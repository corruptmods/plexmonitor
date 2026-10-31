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
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from util.downloadclient import (DownloadClientError, TorrentClient, number,
                                 raise_for)
from util.safehttp import read_text

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


class RtorrentClient(TorrentClient):
    name = "rTorrent"
    basic_auth = True

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, username, password, **kwargs)
        if urlsplit(self.url).path in ("", "/"):
            self.url += "/RPC2"

    async def _call(self, method: str, *params: Any) -> Any:
        body = xmlrpc.client.dumps(tuple(params), method, allow_none=False)
        async with await self.send("POST", self.url, data=body.encode(),
                                   headers={"Content-Type": "text/xml"}
                                   ) as response:
            raise_for(response)
            text = await read_text(response)
        try:
            result, _method = xmlrpc.client.loads(text, use_builtin_types=True)
        except xmlrpc.client.Fault as fault:
            raise DownloadClientError(str(fault.faultString)[:200]) from None
        except Exception as exc:          # not XML-RPC at all
            raise DownloadClientError("not an XML-RPC answer") from exc
        return result[0] if result else None

    async def _rows(self) -> List[Dict[str, Any]]:
        rows = []
        for row in await self._call("d.multicall2", "", "main", *FIELDS) or []:
            if not isinstance(row, list) or len(row) != len(KEYS):
                continue
            torrent = dict(zip(KEYS, row))
            size = number(torrent.get("size"))
            left = number(torrent.get("left"))
            rate = number(torrent.get("rate"))
            rows.append({
                "id": torrent.get("hash") or "",
                "name": torrent.get("name"),
                "word": status_word(torrent),
                "progress": (size - left) / size * 100 if size > 0 else 0.0,
                "size": size,
                "eta": left / rate if rate > 0 else None,
                "speed": rate,
                # ruTorrent keeps labels URL-encoded.
                "category": unquote(str(torrent.get("label") or "")),
                "finished": number(torrent.get("finished")),
            })
        return rows
