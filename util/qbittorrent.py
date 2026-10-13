"""qBittorrent, over its Web API (v2).

qBittorrent signs a session in with a cookie (SID). It's kept between calls
and renewed only when qBittorrent turns it down. With no username,
qBittorrent is expected to let this address in without signing in (its
"Bypass authentication for clients in whitelisted IP subnets"). The cookie
is handled by hand: aiohttp's cookie jar refuses cookies from bare IP
addresses, which is how most people reach qBittorrent on their LAN.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number)

# Done downloading (seeding, or stopped after). qBittorrent 5 renamed
# paused* to stopped*.
DONE_STATES = frozenset({"uploading", "forcedUP", "stalledUP", "queuedUP",
                         "pausedUP", "stoppedUP"})
STATE_WORDS = {
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


class QbittorrentClient(DownloadClient):
    name = "qBittorrent"

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self.username = username or ""
        self.password = password or ""
        self._cookie = ""
        self._login_lock = asyncio.Lock()

    def session_headers(self) -> Dict[str, str]:
        # qBittorrent refuses requests whose Referer is another site.
        return {"Referer": self.url}

    async def _sign_in(self) -> None:
        """Up to 5.1: 200 "Ok." / "Fails." and a cookie named SID. From
        5.2: 204 with no body / 401, and the cookie is QBT_SID_<port>."""
        session = await self.session()
        async with self._login_lock:
            async with session.post(
                    f"{self.url}/api/v2/auth/login",
                    data={"username": self.username,
                          "password": self.password}) as response:
                text = (await response.text()).strip()
                cookies = [f"{name}={morsel.value}"
                           for name, morsel in response.cookies.items()
                           if name == "SID" or name.startswith("QBT_SID_")]
                if response.status == 403:
                    raise DownloadClientError(
                        "it has temporarily banned this address after failed "
                        "sign-ins")
                accepted = (response.status == 204 or
                            (response.status == 200 and text == "Ok."))
                if not accepted or not cookies:
                    raise DownloadClientError(
                        "it didn't accept the username or password")
                self._cookie = "; ".join(cookies)

    async def _get(self, path: str, **params: str) -> Any:
        session = await self.session()
        for attempt in (1, 2):
            if self.username and not self._cookie:
                await self._sign_in()
            headers = {"Cookie": self._cookie} if self._cookie else {}
            async with session.get(f"{self.url}/api/v2{path}", params=params,
                                   headers=headers) as response:
                if response.status == 403 and self.username and attempt == 1:
                    self._cookie = ""          # session expired: sign in again
                    continue
                if response.status == 403:
                    raise DownloadClientError(
                        "it wants a sign-in: set QBITTORRENT_USERNAME and "
                        "QBITTORRENT_PASSWORD")
                if not response.ok:
                    raise DownloadClientError(f"HTTP {response.status}")
                return await response.json(content_type=None)
        return None

    async def _torrents(self, **params: str) -> List[Dict[str, Any]]:
        data = await self._get("/torrents/info", **params)
        return [t for t in data or [] if isinstance(t, dict)]

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        # Fully downloaded leaves the queue whatever it's doing now
        # (rechecking after a restart, moving, gone missing while seeding):
        # it would otherwise come back at 100% and count as a new
        # completion each time it left again.
        torrents = [t for t in await self._torrents()
                    if str(t.get("state") or "") not in DONE_STATES
                    and number(t.get("progress")) < 1
                    and number(t.get("amount_left"), 1) > 0]
        # Actually moving first, then furthest along.
        torrents.sort(key=lambda t: (STATE_WORDS.get(str(t.get("state")), "")
                                     != "", -number(t.get("progress"))))
        items = []
        for torrent in torrents:
            state = str(torrent.get("state") or "")
            status = STATE_WORDS.get(state, "")
            items.append(self.item(
                torrent.get("hash") or "", torrent.get("name"),
                number(torrent.get("progress")) * 100,
                number(torrent.get("size"), -1),
                status=status,
                timeleft="" if status else eta_text(torrent.get("eta")),
                speed_bps=number(torrent.get("dlspeed")),
                category=torrent.get("category")))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        """Torrents that finished downloading. One removed after its import
        (no seeding) is gone from here, which is why the dashboard also
        watches the queue and asks Radarr/Sonarr."""
        done = []
        for torrent in await self._torrents(filter="completed"):
            if str(torrent.get("state") or "") in ("error", "missingFiles"):
                continue
            done.append({
                "name": torrent.get("name") or "Unknown",
                "category": torrent.get("category") or "",
                "size": "",
                "bytes": int(number(torrent.get("size"))),
                "completed": number(torrent.get("completion_on")),
            })
        return done
