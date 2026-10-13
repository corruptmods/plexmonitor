"""qBittorrent: the torrents still downloading, and recently finished ones,
over its Web API (v2).

qBittorrent signs a session in with a cookie (SID). It's kept between
calls and renewed only when qBittorrent turns it down, so the dashboard
doesn't sign in on every refresh. With no username set, qBittorrent is
expected to let this address in without signing in (its "Bypass
authentication for clients on localhost / in whitelisted IP subnets").

The cookie is handled by hand rather than by aiohttp's cookie jar, which
refuses cookies from bare IP addresses -- how most people reach
qBittorrent on their LAN.

Every method returns plain data in the shape cogs/downloads.py merges with
the other download clients, and never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

import aiohttp

logger = logging.getLogger("plexmonitor.qbittorrent")

# qBittorrent's "no estimate" for eta.
ETA_UNKNOWN = 8_640_000

# Torrents that are done downloading (seeding or stopped after it). Those
# leave the queue; the rest are shown, with what they're doing when it isn't
# plain downloading. qBittorrent 5 renamed paused* to stopped*.
DONE_STATES = frozenset({"uploading", "forcedUP", "stalledUP", "queuedUP",
                         "pausedUP", "stoppedUP"})
STATE_LABELS = {
    "downloading": "",
    "forcedDL": "",
    "stalledDL": "stalled, no peers",
    "pausedDL": "paused",
    "stoppedDL": "paused",
    "queuedDL": "queued",
    "allocating": "queued",
    "metaDL": "getting metadata",
    "forcedMetaDL": "getting metadata",
    "checkingDL": "checking",
    "checkingResumeData": "checking",
    "checkingUP": "checking",
    "moving": "moving",
    "error": "error",
    "missingFiles": "missing files",
}
# Shown first: actually moving bytes.
ACTIVE_STATES = frozenset({"downloading", "forcedDL"})


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def eta_text(seconds: Any) -> str:
    """qBittorrent's eta in seconds -> SABnzbd's "H:MM:SS" style."""
    value = int(_float(seconds, -1))
    if value <= 0 or value >= ETA_UNKNOWN:
        return ""
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class QbittorrentClient:
    name = "qBittorrent"

    def __init__(self, url: str, username: str = "", password: str = "",
                 categories: Iterable[str] = (),
                 verify_ssl: bool = True) -> None:
        self.url = (url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        # Only torrents in these categories (all when empty), so torrents
        # that have nothing to do with Plex stay off the dashboard.
        self.categories = [c for c in (s.strip() for s in categories) if c]
        self.verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._cookie = ""
        self._warned_at = float("-inf")

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    connector=aiohttp.TCPConnector(ssl=self.verify_ssl or False),
                    # qBittorrent refuses requests whose Referer is another
                    # site (its CSRF protection).
                    headers={"Referer": self.url})
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _sign_in(self, session: aiohttp.ClientSession) -> None:
        async with self._login_lock:
            async with session.post(
                    f"{self.url}/api/v2/auth/login",
                    data={"username": self.username,
                          "password": self.password}) as response:
                text = (await response.text()).strip()
                cookie = response.cookies.get("SID")
                if response.status == 403:
                    raise _Refused("qBittorrent has temporarily banned this "
                                   "address after failed sign-ins")
                if text != "Ok." or cookie is None:
                    raise _Refused("qBittorrent didn't accept the username or "
                                   "password")
                self._cookie = f"SID={cookie.value}"

    async def _get(self, path: str, **params: str) -> Any:
        session = await self._get_session()
        for attempt in (1, 2):
            if self.username and not self._cookie:
                await self._sign_in(session)
            headers = {"Cookie": self._cookie} if self._cookie else {}
            async with session.get(f"{self.url}/api/v2{path}", params=params,
                                   headers=headers) as response:
                if response.status == 403 and self.username and attempt == 1:
                    self._cookie = ""          # session expired: sign in again
                    continue
                if response.status == 403:
                    raise _Refused("qBittorrent refused the request (sign-in "
                                   "needed: set QBITTORRENT_USERNAME and "
                                   "QBITTORRENT_PASSWORD)")
                if not response.ok:
                    raise _Refused(f"qBittorrent returned HTTP {response.status}")
                return await response.json(content_type=None)
        return None

    async def _torrents(self, **params: str) -> Optional[List[Dict[str, Any]]]:
        """Torrents in the configured categories, or None when unreadable."""
        try:
            if not self.categories:
                data = await self._get("/torrents/info", **params)
                return [t for t in data or [] if isinstance(t, dict)]
            found: List[Dict[str, Any]] = []
            for category in self.categories:
                data = await self._get("/torrents/info", category=category,
                                       **params)
                found.extend(t for t in data or [] if isinstance(t, dict))
            return found
        except (_Refused, aiohttp.ClientError, asyncio.TimeoutError,
                ValueError) as exc:
            # Logged at most every 10 minutes: the dashboard asks often.
            if time.monotonic() - self._warned_at > 600:
                self._warned_at = time.monotonic()
                logger.error("qBittorrent request failed: %s",
                             exc if str(exc) else type(exc).__name__)
            return None

    async def queue(self) -> Optional[Dict[str, Any]]:
        """Torrents still downloading, active ones first, or None when
        qBittorrent couldn't be read. Same shape as SabnzbdClient.queue."""
        torrents = await self._torrents()
        if torrents is None:
            return None
        waiting = [t for t in torrents
                   if str(t.get("state") or "") not in DONE_STATES]
        waiting.sort(key=lambda t: (
            str(t.get("state") or "") not in ACTIVE_STATES,
            -_float(t.get("progress"))))
        items = []
        for torrent in waiting:
            state = str(torrent.get("state") or "")
            items.append({
                "id": f"qbit:{str(torrent.get('hash') or '').lower()}",
                "name": torrent.get("name") or "Unknown",
                "progress": max(0.0, min(100.0,
                                         _float(torrent.get("progress")) * 100)),
                "timeleft": eta_text(torrent.get("eta"))
                if state in ACTIVE_STATES else "",
                "size_bytes": _float(torrent.get("size"), -1),
                "status": STATE_LABELS.get(state, ""),
                "speed_bps": _float(torrent.get("dlspeed")),
                "client": self.name,
            })
        return {
            "items": items,
            "speed_bps": sum(i["speed_bps"] for i in items),
            "disk_free": None,
            "disk_total": None,
        }

    async def history(self, limit: int,
                      max_age_hours: float = 0.0) -> List[Dict[str, Any]]:
        """Torrents that finished downloading, newest first. A torrent
        removed after its import (no seeding) is gone from here, which is
        why the dashboard also watches the queue and asks Radarr/Sonarr."""
        torrents = await self._torrents(filter="completed",
                                        sort="completion_on", reverse="true")
        if not torrents:
            return []
        cutoff = time.time() - max_age_hours * 3600.0 if max_age_hours > 0 \
            else 0.0
        done: List[Dict[str, Any]] = []
        for torrent in sorted(torrents, reverse=True,
                              key=lambda t: _float(t.get("completion_on"))):
            if str(torrent.get("state") or "") in ("error", "missingFiles"):
                continue
            completed = _float(torrent.get("completion_on"))
            if completed <= 0 or (cutoff and completed < cutoff):
                continue
            done.append({
                "name": torrent.get("name") or "Unknown",
                "category": torrent.get("category") or "",
                "size": "",
                "bytes": int(_float(torrent.get("size"))),
                "completed": completed,
                "download_time": 0,
            })
            if len(done) >= limit:
                break
        return done


class _Refused(Exception):
    """qBittorrent answered, but not with what was asked for."""
