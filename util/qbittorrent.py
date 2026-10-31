"""qBittorrent, over its Web API (v2).

qBittorrent signs a session in with a cookie (SID). It's kept between calls
and renewed only when qBittorrent turns it down. With no username,
qBittorrent is expected to let this address in without signing in (its
"Bypass authentication for clients in whitelisted IP subnets"). The cookie
is handled by hand: aiohttp's cookie jar refuses cookies from bare IP
addresses, which is how most people reach qBittorrent on their LAN.

A refused sign-in isn't retried for an hour: qBittorrent bans an address
after a few failed sign-ins (5 by default), and the bot shares that address
with Sonarr and Radarr.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Tuple

from util.downloadclient import (REFUSED, DownloadClient,
                                 DownloadClientError, eta_text, number)
from util.safehttp import read_json, read_text

# Done downloading (seeding, or stopped after, or being re-checked after).
# qBittorrent 5 renamed paused* to stopped*.
DONE_STATES = frozenset({"uploading", "forcedUP", "stalledUP", "queuedUP",
                         "pausedUP", "stoppedUP", "checkingUP"})
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
    "moving": "moving",
    "error": "error",
    "missingFiles": "missing files",
}
# How long a refused sign-in is believed before trying again.
REFUSED_FOR = 3600.0


def finished(torrent: Dict[str, Any]) -> bool:
    """Done downloading, whatever it's doing now (seeding, re-checking
    after a restart, moving, gone missing while seeding): it would
    otherwise come back at 100% and count as a new completion each time it
    left again."""
    state = str(torrent.get("state") or "")
    if state in DONE_STATES:
        return True
    # Restarted qBittorrent re-reads every torrent first; one that had
    # finished says so with its completion time.
    if state == "checkingResumeData" and number(torrent.get("completion_on")) > 0:
        return True
    return (number(torrent.get("progress")) >= 1
            or number(torrent.get("amount_left"), 1) <= 0)


class QbittorrentClient(DownloadClient):
    name = "qBittorrent"

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, username, password, **kwargs)
        self._cookie = ""
        self._login_lock = asyncio.Lock()
        self._refused_at = float("-inf")

    def session_headers(self) -> Dict[str, str]:
        # qBittorrent refuses requests whose Referer is another site.
        return {"Referer": self.url}

    async def _sign_in(self) -> None:
        """Up to 5.1: 200 "Ok." / "Fails." and a cookie named SID. From
        5.2: 204 with no body / 401, and the cookie is QBT_SID_<port>."""
        async with self._login_lock:
            if self._cookie:
                return              # signed in while this call waited
            if time.monotonic() - self._refused_at < REFUSED_FOR:
                raise DownloadClientError(
                    REFUSED + " (trying again within the hour)")
            async with await self.send(
                    "POST", f"{self.url}/api/v2/auth/login",
                    data={"username": self.username,
                          "password": self.password}) as response:
                text = (await read_text(response, 4096)).strip()[:100]
                cookies = [f"{name}={morsel.value}"
                           for name, morsel in response.cookies.items()
                           if name == "SID" or name.startswith("QBT_SID_")]
                if response.status == 403:
                    self._refused_at = time.monotonic()
                    raise DownloadClientError(
                        "it has temporarily banned this address after failed "
                        "sign-ins")
                accepted = (response.status == 204 or
                            (response.status == 200 and text == "Ok."))
                if not accepted or not cookies:
                    if response.status in (200, 204, 401):
                        self._refused_at = time.monotonic()
                        raise DownloadClientError(REFUSED)
                    raise DownloadClientError(f"HTTP {response.status}")
                self._cookie = "; ".join(cookies)

    async def _get(self, path: str, **params: str) -> Any:
        for attempt in (1, 2):
            if self.username and not self._cookie:
                await self._sign_in()
            cookie = self._cookie
            headers = {"Cookie": cookie} if cookie else {}
            async with await self.send("GET", f"{self.url}/api/v2{path}",
                                       params=params,
                                       headers=headers) as response:
                if response.status == 403 and self.username and attempt == 1:
                    # The session expired (qBittorrent restarted). Another
                    # call may already have signed in again.
                    if self._cookie == cookie:
                        self._cookie = ""
                    continue
                if response.status == 403:
                    raise DownloadClientError(
                        "it wants a sign-in: set QBITTORRENT_USERNAME and "
                        "QBITTORRENT_PASSWORD")
                if not response.ok:
                    raise DownloadClientError(f"HTTP {response.status}")
                return await read_json(response)
        return None

    async def _torrents(self, **params: str) -> List[Dict[str, Any]]:
        data = await self._get("/torrents/info", **params)
        return [t for t in data or [] if isinstance(t, dict)]

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        torrents = [t for t in await self._torrents() if not finished(t)]
        items = []
        for torrent in torrents:
            status = STATE_WORDS.get(str(torrent.get("state") or ""), "")
            items.append(self.item(
                torrent.get("hash") or "", torrent.get("name"),
                number(torrent.get("progress")) * 100,
                number(torrent.get("size"), -1),
                status=status,
                timeleft="" if status else eta_text(torrent.get("eta")),
                speed_bps=number(torrent.get("dlspeed")),
                category=torrent.get("category")))
        # Actually moving first, then furthest along.
        items.sort(key=lambda i: (bool(i["status"]), -i["progress"]))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        """Torrents that finished downloading. One removed after its import
        (no seeding) is gone from here, which is why the dashboard also
        watches the queue and asks Radarr/Sonarr."""
        return [self.history_item(t.get("name"), t.get("category"),
                                  t.get("size"), t.get("completion_on"))
                for t in await self._torrents(filter="completed")
                if str(t.get("state") or "") not in ("error", "missingFiles")]
