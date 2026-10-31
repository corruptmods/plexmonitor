"""NZBGet, over its JSON-RPC (HTTP basic auth: its ControlUsername and
ControlPassword)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from util.downloadclient import (DownloadClient, DownloadClientError,
                                 eta_text, number, raise_for)
from util.safehttp import read_json

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
    basic_auth = True

    async def _call(self, method: str, *params: Any) -> Any:
        async with await self.send("POST", f"{self.url}/jsonrpc",
                                   json={"method": method,
                                         "params": list(params),
                                         "id": 1}) as response:
            raise_for(response)
            data = await read_json(response)
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
            state = str(group.get("Status") or "")
            # Repair files NZBGet holds back are only fetched if needed. A
            # job paused by hand has ALL its files paused: nothing to leave
            # out, or it would read as fully downloaded.
            held = 0.0 if state == "PAUSED" else \
                number(group.get("PausedSizeMB"))
            size = max(0.0, number(group.get("FileSizeMB")) - held) * MB
            left = max(0.0, number(group.get("RemainingSizeMB")) - held) * MB
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
            done.append(self.history_item(
                entry.get("Name") or entry.get("NZBName"),
                entry.get("Category"), number(entry.get("FileSizeMB")) * MB,
                entry.get("HistoryTime")))
        return done
