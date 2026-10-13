"""rTorrent, over XML-RPC.

rTorrent is reached through whatever web server exposes its XML-RPC
(ruTorrent's, usually at /RPC2), with that server's basic auth when it has
one. RTORRENT_URL is that full address, e.g. http://host/RPC2; "/RPC2" is
added when the address has no path. Categories are the label ruTorrent
shows (custom1).
"""

from __future__ import annotations

import re
import xmlrpc.client
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import aiohttp

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number)

FIELDS = ["d.hash=", "d.name=", "d.state=", "d.is_open=", "d.is_active=",
          "d.complete=", "d.size_bytes=", "d.left_bytes=", "d.down.rate=",
          "d.is_hash_checking=", "d.peers_connected=", "d.custom1=",
          "d.timestamp.finished=", "d.message="]
KEYS = ["hash", "name", "state", "is_open", "is_active", "complete", "size",
        "left", "rate", "checking", "peers", "label", "finished", "message"]


def status_word(torrent: Dict[str, Any]) -> Optional[str]:
    """None when it's done downloading, "" while downloading, otherwise
    why it isn't."""
    size = number(torrent.get("size"))
    done = bool(number(torrent.get("complete"))) or (
        size > 0 and number(torrent.get("left")) <= 0)
    # Fully downloaded leaves the queue whatever it's doing now (a
    # recheck), so it can't come back at 100% and count as a new completion.
    if done:
        return None
    if number(torrent.get("checking")):
        return "checking"
    # A magnet link is a stand-in named <hash>.meta until its file list is in.
    if re.fullmatch(r"[0-9a-fA-F]{40}\.meta", str(torrent.get("name") or "")):
        return "getting metadata"
    if not number(torrent.get("state")):
        # Closed by rTorrent on an error, or stopped by someone.
        return "error" if str(torrent.get("message") or "").strip() \
            else "paused"
    if not number(torrent.get("is_open")) or not number(torrent.get("is_active")):
        return "paused"
    if not number(torrent.get("peers")):
        return "stalled, no peers"
    return ""


class RtorrentClient(DownloadClient):
    name = "rTorrent"

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        if urlsplit(self.url).path in ("", "/"):
            self.url += "/RPC2"
        self.username = username or ""
        self.password = password or ""

    def session_auth(self) -> Optional[aiohttp.BasicAuth]:
        return (aiohttp.BasicAuth(self.username, self.password)
                if self.username else None)

    async def _call(self, method: str, *params: Any) -> Any:
        session = await self.session()
        body = xmlrpc.client.dumps(tuple(params), method, allow_none=False)
        async with session.post(self.url, data=body.encode(),
                                headers={"Content-Type": "text/xml"}) as response:
            if response.status == 401:
                raise DownloadClientError(
                    "it didn't accept the username or password")
            if not response.ok:
                raise DownloadClientError(f"HTTP {response.status}")
            text = await response.text()
        try:
            result, _method = xmlrpc.client.loads(text, use_builtin_types=True)
        except xmlrpc.client.Fault as fault:
            raise DownloadClientError(fault.faultString) from None
        except Exception as exc:          # not XML-RPC at all
            raise DownloadClientError("not an XML-RPC answer") from exc
        return result[0] if result else None

    async def _torrents(self) -> List[Dict[str, Any]]:
        rows = await self._call("d.multicall2", "", "main", *FIELDS)
        torrents = []
        for row in rows or []:
            if isinstance(row, list) and len(row) == len(KEYS):
                torrent = dict(zip(KEYS, row))
                # ruTorrent keeps labels URL-encoded.
                torrent["label"] = unquote(str(torrent.get("label") or ""))
                torrents.append(torrent)
        return torrents

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        items = []
        for torrent in await self._torrents():
            word = status_word(torrent)
            if word is None:
                continue
            size = number(torrent.get("size"))
            left = number(torrent.get("left"))
            rate = number(torrent.get("rate"))
            items.append(self.item(
                torrent.get("hash") or "", torrent.get("name"),
                (size - left) / size * 100 if size > 0 else 0.0,
                size if size > 0 else -1,
                status=word,
                timeleft="" if word or rate <= 0 else eta_text(left / rate),
                speed_bps=rate,
                category=torrent.get("label")))
        items.sort(key=lambda i: (bool(i["status"]), -i["progress"]))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        done = []
        for torrent in await self._torrents():
            if status_word(torrent) is not None:
                continue
            done.append({
                "name": torrent.get("name") or "Unknown",
                "category": torrent.get("label") or "",
                "size": "",
                "bytes": int(number(torrent.get("size"))),
                "completed": number(torrent.get("finished")),
            })
        return done
