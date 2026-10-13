"""SABnzbd: its queue and finished jobs, over its API (apikey in the URL)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from util.downloadclient import DownloadClient, DownloadClientError, number

# Queue statuses that mean "not downloading right now", in the dashboard's
# words.
STATUS_WORDS = {
    "paused": "paused",
    "queued": "queued",
    "fetching": "fetching",
    "grabbing": "fetching",
    "propagating": "waiting to start",
    "checking": "checking",
}


class SabnzbdClient(DownloadClient):
    name = "SABnzbd"

    def __init__(self, url: str, api_key: str, **kwargs: Any) -> None:
        super().__init__(url, **kwargs)
        self.api_key = api_key or ""

    async def _api(self, mode: str, **extra: str) -> Dict[str, Any]:
        session = await self.session()
        params = {"apikey": self.api_key, "output": "json", "mode": mode,
                  **extra}
        async with session.get(urljoin(self.url + "/", "api"),
                               params=params) as response:
            if not response.ok:
                raise DownloadClientError(f"HTTP {response.status}")
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise DownloadClientError("not a SABnzbd answer")
        if data.get("status") is False:
            # A wrong API key answers 200 with {"status": false, "error": ...}.
            raise DownloadClientError(str(data.get("error") or "refused"))
        return data

    async def fetch_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        queue = (await self._api("queue")).get("queue") or {}
        # Paused as a whole, SABnzbd still reports each job's own status
        # ("Downloading"), with a 0:00:00 time left.
        all_paused = queue.get("paused") is True
        items = []
        for slot in queue.get("slots") or []:
            status = str(slot.get("status") or "").strip().lower()
            word = "paused" if all_paused else STATUS_WORDS.get(status, "")
            timeleft = str(slot.get("timeleft") or "")
            items.append(self.item(
                slot.get("nzo_id") or slot.get("filename") or "",
                slot.get("filename"),
                number(slot.get("percentage")),
                # "mb" is a number; "size" is already formatted text.
                number(slot.get("mb"), -1) * 1024 * 1024,
                status=word,
                timeleft="" if word or timeleft == "0:00:00" else timeleft,
                category=slot.get("cat")))
        if items and not all_paused:
            # SABnzbd reports one speed for the whole queue.
            downloading = [i for i in items if not i["status"]]
            if len(downloading) == 1:
                downloading[0]["speed_bps"] = number(queue.get("kbpersec")) * 1024
        return items, {
            "speed_bps": number(queue.get("kbpersec")) * 1024,
            "disk_free": _disk(queue.get("diskspace1")),
            "disk_total": _disk(queue.get("diskspacetotal1")),
        }

    async def fetch_history(self, limit: int) -> List[Dict[str, Any]]:
        """Finished jobs, failures left out. "Completed" alone isn't enough:
        SABnzbd leaves a job Completed with a fail_message when unpacking or
        repair partly failed, which leaves no watchable file. A job counts
        only with status Completed, no fail_message, and a storage path."""
        data = await self._api(
            "history", start="0",
            # Over-fetch: failures are interleaved with successes.
            limit=str(max(1, min(200, limit * 5))))
        done = []
        for slot in (data.get("history") or {}).get("slots") or []:
            if str(slot.get("status") or "").lower() != "completed":
                continue
            if str(slot.get("fail_message") or "").strip():
                continue
            if not str(slot.get("storage") or "").strip():
                continue
            done.append({
                "name": slot.get("name") or slot.get("nzb_name") or "Unknown",
                "category": slot.get("category") or "",
                "size": slot.get("size") or "",
                "bytes": slot.get("bytes") or 0,
                "completed": number(slot.get("completed")),
            })
        return done


def _disk(value: Any) -> Optional[str]:
    """SABnzbd reports disk space in GB."""
    size = number(value, -1)
    if size < 0:
        return None
    if size >= 1024:
        return f"{size / 1024:.2f} TB"
    return f"{size:.0f} GB"
