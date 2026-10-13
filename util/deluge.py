"""Deluge, over its Web UI's JSON-RPC.

Deluge signs in with its Web UI password alone, into a session kept in a
cookie (handled by hand, since aiohttp's jar refuses cookies from bare IP
addresses) and renewed when Deluge forgets it. The Web UI talks to a
daemon; if it isn't connected to one, it's connected to the first it knows,
as Sonarr does. Categories are labels from Deluge's Label plugin.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number)

FIELDS = ["name", "state", "progress", "download_payload_rate", "eta",
          "total_wanted", "total_done", "is_finished", "num_seeds",
          "num_peers", "label", "completed_time"]
# Deluge's "not signed in" error code.
NOT_SIGNED_IN = 1


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


class DelugeClient(DownloadClient):
    name = "Deluge"

    def __init__(self, url: str, password: str = "", **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self.password = password or ""
        self._cookie = ""
        self._login_lock = asyncio.Lock()
        self._request_id = 0

    async def _raw(self, method: str, *params: Any) -> Dict[str, Any]:
        session = await self.session()
        self._request_id += 1
        headers = {"Cookie": self._cookie} if self._cookie else {}
        async with session.post(f"{self.url}/json", headers=headers,
                                json={"method": method, "params": list(params),
                                      "id": self._request_id}) as response:
            if not response.ok:
                raise DownloadClientError(f"HTTP {response.status}")
            data = await response.json(content_type=None)
            if method == "auth.login":
                cookie = response.cookies.get("_session_id")
                self._cookie = f"_session_id={cookie.value}" if cookie else ""
        if not isinstance(data, dict):
            raise DownloadClientError("not a Deluge answer")
        return data

    async def _sign_in(self) -> None:
        async with self._login_lock:
            answer = await self._raw("auth.login", self.password)
            if answer.get("result") is not True or not self._cookie:
                self._cookie = ""
                raise DownloadClientError("it didn't accept the password")
            connected = await self._raw("web.connected")
            if connected.get("result"):
                return
            hosts = (await self._raw("web.get_hosts")).get("result") or []
            if not hosts:
                raise DownloadClientError(
                    "its Web UI isn't connected to a Deluge daemon")
            await self._raw("web.connect", hosts[0][0])

    async def _call(self, method: str, *params: Any) -> Any:
        if not self._cookie:
            await self._sign_in()
        answer = await self._raw(method, *params)
        error = answer.get("error")
        if error and (error or {}).get("code") == NOT_SIGNED_IN:
            self._cookie = ""
            await self._sign_in()
            answer = await self._raw(method, *params)
            error = answer.get("error")
        if error:
            raise DownloadClientError(str((error or {}).get("message")
                                          or "error"))
        return answer.get("result")

    async def _torrents(self) -> Dict[str, Dict[str, Any]]:
        torrents = await self._call("core.get_torrents_status", {}, FIELDS)
        return {h: t for h, t in (torrents or {}).items()
                if isinstance(t, dict)}

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        items = []
        for torrent_hash, torrent in (await self._torrents()).items():
            word = status_word(torrent)
            if word is None:
                continue
            items.append(self.item(
                torrent_hash, torrent.get("name"),
                number(torrent.get("progress")),
                number(torrent.get("total_wanted"), -1),
                status=word,
                timeleft="" if word else eta_text(torrent.get("eta")),
                speed_bps=number(torrent.get("download_payload_rate")),
                category=torrent.get("label")))
        items.sort(key=lambda i: (bool(i["status"]), -i["progress"]))
        return items, {}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        done = []
        for torrent in (await self._torrents()).values():
            if status_word(torrent) is not None:
                continue
            done.append({
                "name": torrent.get("name") or "Unknown",
                "category": torrent.get("label") or "",
                "size": "",
                "bytes": int(number(torrent.get("total_wanted"))),
                # Deluge 2's completed_time; Deluge 1 has no finish time.
                "completed": number(torrent.get("completed_time")),
            })
        return done
