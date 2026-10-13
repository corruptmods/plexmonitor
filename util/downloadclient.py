"""What every download client has in common.

PlexMonitor reads the download clients Seerr AI supports: SABnzbd and
NZBGet for Usenet, qBittorrent, Transmission, Deluge and rTorrent for
torrents. Each lives in its own module and only fills in two methods:

  fetch_queue()    the jobs not finished downloading, as items (below),
                   plus any extras (SABnzbd's disk space)
  fetch_history()  jobs that finished downloading, newest first

This base turns those into what cogs/downloads.py merges: it keeps one
aiohttp session, filters by category, never raises (a client that can't be
read reports None for its queue and [] for its history), and logs a failing
client at most every ten minutes instead of on every refresh.

A queue item:
  {"id", "name", "progress" (0-100), "timeleft" ("H:MM:SS" or ""),
   "size_bytes", "status" ("" while downloading, else a short word or two
   saying why not), "speed_bps", "category", "client"}

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

logger = logging.getLogger("plexmonitor.downloads")

# How long a client's failure stays quiet in the log after being reported.
WARN_EVERY = 600.0
TIMEOUT = 10.0


class DownloadClientError(Exception):
    """The client answered, but not with what was asked for (a refused
    sign-in, an error in its reply)."""


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def eta_text(seconds: Any) -> str:
    """Seconds left -> SABnzbd's "H:MM:SS" style; "" when unknown."""
    value = int(number(seconds, -1))
    # Clients use huge numbers (qBittorrent's 8640000) for "no estimate".
    if value <= 0 or value >= 100 * 86400:
        return ""
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class DownloadClient:
    name = "Download client"

    def __init__(self, url: str, categories: Iterable[str] = (),
                 verify_ssl: bool = True) -> None:
        self.url = (url or "").rstrip("/")
        # Only jobs in these categories (labels, for some torrent clients);
        # every job when empty.
        self.categories = {c.strip().lower() for c in categories if c.strip()}
        self.verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._warned_at = float("-inf")

    # ------------------------------------------------------------- plumbing

    def session_headers(self) -> Dict[str, str]:
        return {}

    def session_auth(self) -> Optional[aiohttp.BasicAuth]:
        return None

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                    connector=aiohttp.TCPConnector(
                        ssl=None if self.verify_ssl else False),
                    headers=self.session_headers(),
                    auth=self.session_auth(),
                    # Sign-in cookies are sent by hand where a client needs
                    # one: aiohttp's jar refuses cookies from bare IP
                    # addresses, which is how most clients are reached.
                    cookie_jar=aiohttp.DummyCookieJar())
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _failed(self, what: str, exc: BaseException) -> None:
        if time.monotonic() - self._warned_at < WARN_EVERY:
            return
        self._warned_at = time.monotonic()
        detail = str(exc) or type(exc).__name__
        logger.error("%s %s failed: %s", self.name, what, detail)

    def _wanted(self, category: Any) -> bool:
        return not self.categories or \
            str(category or "").strip().lower() in self.categories

    def item(self, ident: str, name: Any, progress: float, size_bytes: float,
             status: str = "", timeleft: str = "", speed_bps: float = 0.0,
             category: Any = "") -> Dict[str, Any]:
        return {
            "id": f"{self.name.lower()}:{str(ident).lower()}",
            "name": str(name or "Unknown"),
            "progress": max(0.0, min(100.0, number(progress))),
            "timeleft": timeleft,
            "size_bytes": number(size_bytes, -1),
            "status": status,
            "speed_bps": max(0.0, number(speed_bps)),
            "category": str(category or ""),
            "client": self.name,
        }

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
        cutoff = time.time() - max_age_hours * 3600.0 if max_age_hours > 0 \
            else 0.0
        kept = [d for d in done
                if self._wanted(d.get("category"))
                and number(d.get("completed")) > 0
                and not (cutoff and number(d.get("completed")) < cutoff)]
        kept.sort(key=lambda d: number(d.get("completed")), reverse=True)
        return kept[:limit]
