"""NZBGet, over its JSON-RPC (HTTP basic auth: its ControlUsername and
ControlPassword)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number)

MB = 1024 * 1024

# After downloading, while still in NZBGet's queue.
AFTER_DOWNLOAD = {
    "PP_QUEUED": "waiting to unpack",
    "LOADING_PARS": "verifying",
    "VERIFYING_SOURCES": "verifying",
    "VERIFYING_REPAIRED": "verifying",
    "REPAIRING": "repairing",
    "RENAMING": "unpacking",
    "UNPACKING": "unpacking",
    "MOVING": "moving",
    "EXECUTING_SCRIPT": "running its script",
    "PP_FINISHED": "finishing",
}
QUEUE_WORDS = {
    "QUEUED": "queued",
    "PAUSED": "paused",
    "FETCHING": "fetching",
    "DOWNLOADING": "",
}


class NzbgetClient(DownloadClient):
    name = "NZBGet"

    def __init__(self, url: str, username: str = "", password: str = "",
                 **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self.username = username or ""
        self.password = password or ""

    def session_auth(self) -> Optional[aiohttp.BasicAuth]:
        return (aiohttp.BasicAuth(self.username, self.password)
                if self.username else None)

    async def _call(self, method: str, *params: Any) -> Any:
        session = await self.session()
        async with session.post(f"{self.url}/jsonrpc",
                                json={"method": method, "params": list(params),
                                      "id": 1}) as response:
            if response.status == 401:
                raise DownloadClientError(
                    "it didn't accept the username or password")
            if not response.ok:
                raise DownloadClientError(f"HTTP {response.status}")
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise DownloadClientError("not an NZBGet answer")
        if data.get("error"):
            raise DownloadClientError(
                str((data["error"] or {}).get("message") or "error"))
        return data.get("result")

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        groups = await self._call("listgroups", 0) or []
        status = await self._call("status") or {}
        paused_all = bool(status.get("DownloadPaused"))
        rate = number(status.get("DownloadRate"))
        downloading = [g for g in groups if g.get("Status") == "DOWNLOADING"]
        items = []
        for group in groups:
            # Repair files NZBGet holds back are only fetched if needed.
            held = number(group.get("PausedSizeMB"))
            size = max(0.0, number(group.get("FileSizeMB")) - held) * MB
            left = max(0.0, number(group.get("RemainingSizeMB")) - held) * MB
            state = str(group.get("Status") or "")
            if state in AFTER_DOWNLOAD:
                status_word, left = AFTER_DOWNLOAD[state], 0.0
            elif paused_all and state != "PAUSED":
                status_word = "paused"
            else:
                status_word = QUEUE_WORDS.get(state, "queued")
            # NZBGet reports one speed: it's this job's only when it's the
            # one downloading.
            sole = not status_word and len(downloading) == 1
            items.append(self.item(
                group.get("NZBID") or "",
                group.get("NZBName") or group.get("NZBNicename"),
                (size - left) / size * 100 if size else 0.0,
                size,
                status=status_word,
                timeleft=eta_text(left / rate) if sole and rate > 0 else "",
                speed_bps=rate if sole else 0.0,
                category=group.get("Category")))
        return items, {"speed_bps": rate if items else 0.0}

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        done = []
        for entry in await self._call("history", False) or []:
            status = str(entry.get("Status") or "")
            # A failing script leaves the download itself fine.
            if not (status.startswith("SUCCESS/") or status == "WARNING/SCRIPT"):
                continue
            done.append({
                "name": entry.get("Name") or entry.get("NZBName") or "Unknown",
                "category": entry.get("Category") or "",
                "size": "",
                "bytes": int(number(entry.get("FileSizeMB")) * MB),
                "completed": number(entry.get("HistoryTime")),
            })
        return done
