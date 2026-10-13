"""Transmission, over its RPC.

Transmission guards its RPC with a session id: the first call is refused
with 409 and the id to use, which is kept and renewed whenever Transmission
refuses it again. A username and password, when set, go with every call
(HTTP basic auth). TRANSMISSION_URL is its web address
(http://host:9091); the RPC path is worked out from it (see rpc_url). Categories are Transmission's labels (3.0 and
newer).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number)

FIELDS = ["hashString", "name", "status", "percentDone", "rateDownload",
          "eta", "sizeWhenDone", "leftUntilDone", "error",
          "peersSendingToUs", "webseedsSendingToUs",
          "metadataPercentComplete", "labels", "doneDate"]
# Transmission's torrent status numbers.
STOPPED, CHECK_WAIT, CHECKING, DOWNLOAD_WAIT, DOWNLOADING, SEED_WAIT, \
    SEEDING = range(7)


def status_word(torrent: Dict[str, Any]) -> Optional[str]:
    """None when it's done downloading (it leaves the queue), "" while
    downloading, otherwise why it isn't."""
    size = number(torrent.get("sizeWhenDone"))
    left = number(torrent.get("leftUntilDone"))
    done = size > 0 and left <= 0
    status = int(number(torrent.get("status"), -1))
    # Fully downloaded leaves the queue whatever it's doing now (checking,
    # an error after its files were cleaned up), so it can't come back at
    # 100% and count as a new completion.
    if done:
        return None
    if int(number(torrent.get("error"))) == 3:     # a local error
        return "error"
    if number(torrent.get("metadataPercentComplete"), 1) < 1:
        return "getting metadata"
    if status == STOPPED:
        return "paused"
    if status in (CHECK_WAIT, CHECKING):
        return "checking"
    if status == DOWNLOAD_WAIT:
        return "queued"
    if status == DOWNLOADING:
        sources = number(torrent.get("peersSendingToUs")) + \
            number(torrent.get("webseedsSendingToUs"))
        return "" if sources else "stalled, no peers"
    return ""


def rpc_url(url: str) -> str:
    """The RPC address from whatever people paste: the bare address, the
    URL base Sonarr shows (/transmission), the web page
    (/transmission/web/), or the RPC address itself."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/web"):
        path = path[: -len("/web")]
    if not path:
        path = "/transmission"
    if not path.endswith("/rpc"):
        path += "/rpc"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class TransmissionClient(DownloadClient):
    name = "Transmission"

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self.url = rpc_url(self.url)
        self.username = username or ""
        self.password = password or ""
        self._session_id = ""

    def session_auth(self) -> Optional[aiohttp.BasicAuth]:
        return (aiohttp.BasicAuth(self.username, self.password)
                if self.username else None)

    async def _call(self, method: str, **arguments: Any) -> Dict[str, Any]:
        session = await self.session()
        for attempt in (1, 2):
            headers = ({"X-Transmission-Session-Id": self._session_id}
                       if self._session_id else {})
            async with session.post(self.url, headers=headers,
                                    json={"method": method,
                                          "arguments": arguments}) as response:
                if response.status == 409 and attempt == 1:
                    self._session_id = response.headers.get(
                        "X-Transmission-Session-Id", "")
                    continue
                if response.status == 401:
                    raise DownloadClientError(
                        "it didn't accept the username or password")
                if not response.ok:
                    raise DownloadClientError(f"HTTP {response.status}")
                data = await response.json(content_type=None)
            if not isinstance(data, dict) or data.get("result") != "success":
                raise DownloadClientError(
                    str((data or {}).get("result") or "no answer"))
            return data.get("arguments") or {}
        raise DownloadClientError("it kept refusing its own session id")

    async def _torrents(self) -> List[Dict[str, Any]]:
        answer = await self._call("torrent-get", fields=FIELDS)
        return [t for t in answer.get("torrents") or [] if isinstance(t, dict)]

    @staticmethod
    def _category(torrent: Dict[str, Any]) -> str:
        labels = torrent.get("labels") or []
        return str(labels[0]) if labels else ""

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        items = []
        for torrent in await self._torrents():
            word = status_word(torrent)
            if word is None:
                continue
            items.append(self.item(
                torrent.get("hashString") or "", torrent.get("name"),
                number(torrent.get("percentDone")) * 100,
                number(torrent.get("sizeWhenDone"), -1),
                status=word,
                timeleft="" if word else eta_text(torrent.get("eta")),
                speed_bps=number(torrent.get("rateDownload")),
                category=self._category(torrent)))
        items.sort(key=lambda i: (bool(i["status"]), -i["progress"]))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        done = []
        for torrent in await self._torrents():
            if status_word(torrent) is not None:
                continue
            done.append({
                "name": torrent.get("name") or "Unknown",
                "category": self._category(torrent),
                "size": "",
                "bytes": int(number(torrent.get("sizeWhenDone"))),
                "completed": number(torrent.get("doneDate")),
            })
        return done
