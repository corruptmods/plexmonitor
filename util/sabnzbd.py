"""SABnzbd: the download queue and finished jobs, over its API.

One reused aiohttp session. Every method returns plain data in the shape
cogs/downloads.py merges with the other download clients, and never raises:
a client that can't be read reports None (queue) or [] (history).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger("plexmonitor.sabnzbd")

# SABnzbd's queue statuses that mean "not downloading right now", and how
# the dashboard words them.
STATUS_LABELS = {
    "paused": "paused",
    "queued": "queued",
    "fetching": "fetching",
    "grabbing": "fetching",
    "propagating": "waiting to start",
    "checking": "checking",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SabnzbdClient:
    name = "SABnzbd"

    def __init__(self, url: str, api_key: str) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10))
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _api(self, mode: str, **extra: str) -> Optional[Dict[str, Any]]:
        session = await self._get_session()
        params = {"apikey": self.api_key, "output": "json", "mode": mode,
                  **extra}
        try:
            async with session.get(urljoin(self.url + "/", "api"),
                                   params=params) as response:
                if not response.ok:
                    logger.error("SABnzbd returned HTTP %s (%s)",
                                 response.status, mode)
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.error("SABnzbd request failed (%s): %s", mode,
                         type(exc).__name__ if not str(exc) else exc)
            return None
        if isinstance(data, dict) and data.get("status") is False:
            # A bad API key answers 200 with {"status": false, "error": ...}.
            logger.error("SABnzbd rejected the request: %s",
                         data.get("error") or "unknown error")
            return None
        return data if isinstance(data, dict) else None

    async def queue(self) -> Optional[Dict[str, Any]]:
        """Every job in the queue, in SABnzbd's order, or None when the
        queue couldn't be read.

        {"items": [{id, name, progress, timeleft, size, status, client}],
         "speed_bps": float, "disk_free": str|None, "disk_total": str|None}
        """
        data = await self._api("queue")
        if data is None:
            return None
        queue = data.get("queue") or {}
        items = []
        for slot in queue.get("slots") or []:
            status = str(slot.get("status") or "").strip().lower()
            items.append({
                # The id SABnzbd knows a job by; the dashboard watches these
                # leave the queue to notice completions.
                "id": f"sab:{slot.get('nzo_id') or slot.get('filename') or ''}",
                "name": slot.get("filename") or "Unknown",
                "progress": _float(slot.get("percentage")),
                "timeleft": slot.get("timeleft") or "",
                # "mb" is numeric; "size" is already formatted text.
                "size_bytes": _float(slot.get("mb"), -1) * 1024 * 1024,
                "status": STATUS_LABELS.get(status, ""),
                "speed_bps": 0.0,
                "client": self.name,
            })
        return {
            "items": items,
            "speed_bps": _float(queue.get("kbpersec")) * 1024,
            "disk_free": _disk(queue.get("diskspace1")),
            "disk_total": _disk(queue.get("diskspacetotal1")),
        }

    async def history(self, limit: int,
                      max_age_hours: float = 0.0) -> List[Dict[str, Any]]:
        """Recently FINISHED jobs, failures excluded.

        "Completed" alone is not enough to trust: SABnzbd leaves a job
        marked Completed while carrying a fail_message when post-processing
        partly failed (unpack errors, repair that couldn't finish), and
        those produce no watchable file. A job counts as done only when its
        status is Completed, it has no fail_message, and files were written
        (a storage path).
        """
        data = await self._api(
            "history", start="0",
            # Over-fetch: failures are interleaved with successes.
            limit=str(max(1, min(200, limit * 5))))
        if data is None:
            return []
        cutoff = time.time() - max_age_hours * 3600.0 if max_age_hours > 0 \
            else 0.0
        done: List[Dict[str, Any]] = []
        for slot in (data.get("history") or {}).get("slots") or []:
            if str(slot.get("status") or "").lower() != "completed":
                continue
            if str(slot.get("fail_message") or "").strip():
                continue
            if not str(slot.get("storage") or "").strip():
                continue
            completed = _float(slot.get("completed"))
            if cutoff and completed and completed < cutoff:
                continue
            done.append({
                "name": slot.get("name") or slot.get("nzb_name") or "Unknown",
                "category": slot.get("category") or "",
                "size": slot.get("size") or "",
                "bytes": slot.get("bytes") or 0,
                "completed": completed,
                "download_time": slot.get("download_time") or 0,
            })
            if len(done) >= limit:
                break
        return done


def _disk(value: Any) -> Optional[str]:
    """SABnzbd reports disk space in GB."""
    size = _float(value, -1)
    if size < 0:
        return None
    if size >= 1024:
        return f"{size / 1024:.2f} TB"
    return f"{size:.0f} GB"
