"""What every download client has in common.

PlexMonitor reads the download clients Seerr AI supports: SABnzbd and
NZBGet for Usenet, qBittorrent, Transmission, Deluge and rTorrent for
torrents. Each lives in its own module and only fills in two methods:

  fetch_queue()    the jobs not finished downloading, as items (below),
                   plus any extras (SABnzbd's disk space)
  fetch_history()  jobs that finished downloading, newest first

(Transmission, Deluge and rTorrent fill in one, _rows(), through
TorrentClient below: one list of torrents answers both.)

This base turns those into what cogs/downloads.py merges: it keeps one
aiohttp session, filters by category, never raises (a client that can't be
read reports None for its queue and [] for its history), and logs a failing
client at most every ten minutes instead of on every refresh.

A queue item:
  {"id" ("<client name, lowercased>:<its own id>"), "name", "progress"
   (0-100), "timeleft" ("H:MM:SS" or ""), "size_bytes" (-1 when unknown),
   "status" ("" while downloading, else a short word or two saying why
   not), "speed_bps", "category", "client"}

A history item:
  {"name", "category", "size" (text, may be ""), "bytes", "completed"
   (epoch seconds)}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp

from util import safehttp

logger = logging.getLogger("plexmonitor.downloads")

# How long a client's failure stays quiet in the log after being reported.
WARN_EVERY = 600.0
TIMEOUT = 10.0
REFUSED = "it didn't accept the username or password"


class DownloadClientError(Exception):
    """The client answered, but not with what was asked for (a refused
    sign-in, an error in its reply)."""


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    # NaN and infinity aren't amounts of anything.
    return result if result == result and abs(result) != float("inf") \
        else default


def eta_text(seconds: Any) -> str:
    """Seconds left -> SABnzbd's "H:MM:SS" style; "" when unknown."""
    value = int(number(seconds, -1))
    # Clients use huge numbers (qBittorrent's 8640000) for "no estimate".
    if value <= 0 or value >= 100 * 86400:
        return ""
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def with_scheme(url: str) -> str:
    """An address as people type it: "192.168.1.5:8080" means http."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


def raise_for(response: aiohttp.ClientResponse) -> None:
    """A refused sign-in or an HTTP error, in words for the log."""
    if response.status == 401:
        raise DownloadClientError(REFUSED)
    if not response.ok:
        raise DownloadClientError(f"HTTP {response.status}")


class DownloadClient:
    name = "Download client"
    # Clients that take HTTP basic auth (NZBGet, Transmission, rTorrent's
    # web server) send the username and password with every call.
    basic_auth = False

    def __init__(self, url: str, username: str = "", password: str = "",
                 categories: Iterable[str] = (),
                 verify_ssl: bool = True) -> None:
        self.url = with_scheme(url)
        self.username = username or ""
        self.password = password or ""
        # Only jobs in these categories (labels, for some torrent clients);
        # every job when empty.
        self.categories = {c.strip().lower() for c in categories if c.strip()}
        self.verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._warned_at: Dict[str, float] = {}

    # ------------------------------------------------------------- plumbing

    def session_headers(self) -> Dict[str, str]:
        return {}

    def login(self) -> Optional[aiohttp.BasicAuth]:
        """Basic auth for clients that take it, sent with each call (never
        set on the session, which aiohttp would carry to another host)."""
        if self.basic_auth and self.username:
            return aiohttp.BasicAuth(self.username, self.password)
        return None

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                    connector=aiohttp.TCPConnector(
                        ssl=None if self.verify_ssl else False),
                    headers=self.session_headers(),
                    # Sign-in cookies are sent by hand where a client needs
                    # one: aiohttp's jar refuses cookies from bare IP
                    # addresses, which is how most clients are reached.
                    cookie_jar=aiohttp.DummyCookieJar())
            return self._session

    async def send(self, method: str, url: str,
                   **kwargs: Any) -> aiohttp.ClientResponse:
        """One call to the client, its login with it; redirects followed
        only within the same host (see util/safehttp.py). Use as
        `async with await self.send(...) as response:`."""
        auth = self.login()
        if auth is not None:
            kwargs.setdefault("auth", auth)
        return await safehttp.request(await self.session(), method, url,
                                      **kwargs)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _failed(self, what: str, exc: BaseException) -> None:
        if time.monotonic() - self._warned_at.get(what, float("-inf")) \
                < WARN_EVERY:
            return
        self._warned_at[what] = time.monotonic()
        # One line, and not a client's whole error page.
        detail = " ".join((str(exc) or type(exc).__name__).split())[:300]
        logger.error("%s %s failed: %s", self.name, what, detail)

    def _worked(self, what: str) -> None:
        if self._warned_at.pop(what, None) is not None:
            logger.info("%s %s works again", self.name, what)

    def _wanted(self, category: Any) -> bool:
        return not self.categories or \
            str(category or "").strip().lower() in self.categories

    def item(self, ident: str, name: Any, progress: float, size_bytes: float,
             status: str = "", timeleft: str = "", speed_bps: float = 0.0,
             category: Any = "") -> Dict[str, Any]:
        size = number(size_bytes, -1)
        return {
            "id": f"{self.name.lower()}:{str(ident).lower()}",
            "name": str(name or "Unknown"),
            "progress": max(0.0, min(100.0, number(progress))),
            "timeleft": timeleft,
            # 0 is what clients report before they know (a magnet still
            # fetching its file list): unknown, not empty.
            "size_bytes": size if size > 0 else -1,
            "status": status,
            "speed_bps": max(0.0, number(speed_bps)),
            "category": str(category or ""),
            "client": self.name,
        }

    @staticmethod
    def history_item(name: Any, category: Any, size_bytes: Any,
                     completed: Any, size: str = "") -> Dict[str, Any]:
        return {"name": str(name or "Unknown"),
                "category": str(category or ""),
                "size": size,
                "bytes": max(0, int(number(size_bytes))),
                "completed": number(completed)}

    # -------------------------------------------------------- what to fill

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        raise NotImplementedError

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        return []

    # --------------------------------------------------------- what's read

    async def queue(self) -> Optional[Dict[str, Any]]:
        """Jobs not finished downloading (moving ones first, the client's
        own order kept otherwise), or None when the client can't be read."""
        try:
            items, extras = await self.fetch_queue()
        except Exception as exc:     # an odd answer must never stop a refresh
            self._failed("queue", exc)
            return None
        self._worked("queue")
        items = [i for i in items if self._wanted(i.get("category"))]
        items.sort(key=lambda i: bool(i.get("status")))
        return {
            "items": items,
            # The client's own total when it reports one (SABnzbd and
            # NZBGet give one speed for the whole queue).
            "speed_bps": (number(extras["speed_bps"]) if "speed_bps" in extras
                          else sum(i["speed_bps"] for i in items)),
            "disk_free": extras.get("disk_free"),
            "disk_total": extras.get("disk_total"),
        }

    async def history(self, limit: int,
                      max_age_hours: float = 0.0) -> List[Dict[str, Any]]:
        """Jobs that finished downloading, newest first ([] when the client
        can't be read)."""
        try:
            done = await self.fetch_history(max(1, limit))
        except Exception as exc:     # an odd answer must never stop a refresh
            self._failed("history", exc)
            return []
        self._worked("history")
        cutoff = time.time() - max_age_hours * 3600.0 if max_age_hours > 0 \
            else 0.0
        kept = [d for d in done
                if self._wanted(d.get("category"))
                and number(d.get("completed")) > 0
                and not (cutoff and number(d.get("completed")) < cutoff)]
        kept.sort(key=lambda d: number(d.get("completed")), reverse=True)
        return kept[:limit]


class TorrentClient(DownloadClient):
    """A torrent client whose one list of torrents answers both the queue
    (not finished downloading) and the history (finished). Subclasses
    read that list in _rows()."""

    async def _rows(self) -> List[Dict[str, Any]]:
        """Every torrent, as {"id", "name", "word" (None when it's done
        downloading, "" while downloading, otherwise why it isn't),
        "progress" (0-100), "size" (bytes), "eta" (seconds, or None),
        "speed" (bytes/s), "category", "finished" (epoch seconds, 0 when
        unknown)}."""
        raise NotImplementedError

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        items = [self.item(row["id"], row["name"], row["progress"],
                           row["size"], status=row["word"],
                           timeleft="" if row["word"] else eta_text(row["eta"]),
                           speed_bps=row["speed"], category=row["category"])
                 for row in await self._rows() if row["word"] is not None]
        items.sort(key=lambda i: (bool(i["status"]), -i["progress"]))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        return [self.history_item(row["name"], row["category"], row["size"],
                                  row["finished"])
                for row in await self._rows() if row["word"] is None]
