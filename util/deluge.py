"""Deluge, over its Web UI's JSON-RPC.

Deluge signs in with its Web UI password alone, into a session kept in a
cookie (handled by hand, since aiohttp's jar refuses cookies from bare IP
addresses) and renewed when Deluge forgets it. The Web UI talks to a
daemon; if it isn't connected to one, it's connected to the first it knows,
as Sonarr does -- also when the daemon restarts under a Web UI that stays
up. Categories are labels from Deluge's Label plugin.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from util.downloadclient import (DownloadClientError, TorrentClient, number,
                                 raise_for)
from util.safehttp import read_json

FIELDS = ["name", "state", "download_payload_rate", "eta",
          "total_wanted", "total_done", "is_finished", "num_seeds",
          "num_peers", "label", "completed_time"]
# Deluge's error codes: not signed in, and an unknown method -- which is
# what every daemon call gets while the Web UI has lost its daemon.
NOT_SIGNED_IN = 1
UNKNOWN_METHOD = 2


def status_word(torrent: Dict[str, Any]) -> Optional[str]:
    """None when it's done downloading, "" while downloading, otherwise
    why it isn't."""
    wanted = number(torrent.get("total_wanted"))
    done = bool(torrent.get("is_finished")) or (
        wanted > 0 and number(torrent.get("total_done")) >= wanted)
    state = str(torrent.get("state") or "")
    # Fully downloaded leaves the queue whatever it's doing now (checking,
    # moving, an error after its files were cleaned up).
    if done or state == "Seeding":
        return None
    if state == "Error":
        return "error"
    if state == "Downloading":
        if not wanted:
            return "getting metadata"
        if not number(torrent.get("num_seeds")) and \
                not number(torrent.get("num_peers")):
            return "stalled, no peers"
        return ""
    return {"Paused": "paused", "Queued": "queued", "Allocating": "queued",
            "Checking": "checking", "Moving": "moving"}.get(state, "")


class DelugeClient(TorrentClient):
    name = "Deluge"

    def __init__(self, url: str, password: str = "", **kwargs: Any) -> None:
        super().__init__(url, "", password, **kwargs)
        self._cookie = ""
        self._login_lock = asyncio.Lock()
        self._request_id = 0

    async def _raw(self, method: str, *params: Any) -> Dict[str, Any]:
        self._request_id += 1
        headers = {"Cookie": self._cookie} if self._cookie else {}
        async with await self.send(
                "POST", f"{self.url}/json", headers=headers,
                json={"method": method, "params": list(params),
                      "id": self._request_id}) as response:
            raise_for(response)
            data = await read_json(response)
            if method == "auth.login":
                cookie = response.cookies.get("_session_id")
                self._cookie = f"_session_id={cookie.value}" if cookie else ""
        if not isinstance(data, dict):
            raise DownloadClientError("not a Deluge answer")
        return data

    async def _sign_in(self) -> None:
        async with self._login_lock:
            if self._cookie:
                return              # signed in while this call waited
            answer = await self._raw("auth.login", self.password)
            if answer.get("result") is not True or not self._cookie:
                self._cookie = ""
                raise DownloadClientError(
                    "it didn't accept the password")
            connected = await self._raw("web.connected")
            if connected.get("result"):
                return
            hosts = (await self._raw("web.get_hosts")).get("result") or []
            if not hosts or not isinstance(hosts[0], list) or not hosts[0]:
                raise DownloadClientError(
                    "its Web UI isn't connected to a Deluge daemon")
            await self._raw("web.connect", hosts[0][0])

    @staticmethod
    def _code(answer: Dict[str, Any]) -> Optional[int]:
        error = answer.get("error")
        return error.get("code") if isinstance(error, dict) else None

    async def _call(self, method: str, *params: Any) -> Any:
        if not self._cookie:
            await self._sign_in()
        used = self._cookie
        answer = await self._raw(method, *params)
        if self._code(answer) in (NOT_SIGNED_IN, UNKNOWN_METHOD):
            # Signed out (Deluge restarted), or its Web UI lost the daemon:
            # sign in again, which reconnects it. Another call may already
            # have done so.
            if self._cookie == used:
                self._cookie = ""
            await self._sign_in()
            answer = await self._raw(method, *params)
        error = answer.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) \
                else error
            raise DownloadClientError(str(message or "error")[:200])
        return answer.get("result")

    async def _rows(self) -> List[Dict[str, Any]]:
        torrents = await self._call("core.get_torrents_status", {}, FIELDS)
        rows = []
        for torrent_hash, torrent in (torrents or {}).items():
            if not isinstance(torrent, dict):
                continue
            wanted = number(torrent.get("total_wanted"))
            rows.append({
                "id": torrent_hash,
                "name": torrent.get("name"),
                "word": status_word(torrent),
                # Worked out rather than Deluge's own "progress", which
                # reads 100 for a torrent in Error.
                "progress": (number(torrent.get("total_done")) / wanted * 100
                             if wanted > 0 else 0.0),
                "size": wanted,
                "eta": torrent.get("eta"),
                "speed": number(torrent.get("download_payload_rate")),
                "category": torrent.get("label"),
                # Deluge 2's completed_time; Deluge 1 has no finish time.
                "finished": number(torrent.get("completed_time")),
            })
        return rows
