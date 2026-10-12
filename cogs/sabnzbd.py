"""SABnzbd queue cog.

Changes from the original: a single reused aiohttp session instead of one
per poll, the queue speed surfaced separately, and formatting moved out to
the dashboard so downloads and streams share one progress bar renderer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp
from discord.ext import commands
from dotenv import load_dotenv

from util.jsonfile import data_dir, read_json

if os.getenv("RUNNING_IN_DOCKER", "false").lower() != "true":
    load_dotenv()

logger = logging.getLogger("plexmonitor.sabnzbd")

def _empty() -> Dict[str, Any]:
    """Fresh empty result. A module-level constant copied with dict() would
    share its `downloads` list between every caller."""
    return {"downloads": [], "queued_total": 0, "speed": None,
            "diskspace1": None, "diskspacetotal1": None}


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default

class SABnzbd(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.url = os.getenv("SABNZBD_URL")
        self.api_key = os.getenv("SABNZBD_API_KEY")
        self.enabled = bool(self.url and self.api_key)
        if not self.enabled:
            logger.info("SABNZBD_URL/SABNZBD_API_KEY not set -- download panel disabled")

        self.config_file = os.path.join(data_dir(), "config.json")
        config = self._load_config()
        sab = config.get("sabnzbd") if isinstance(config, dict) else None
        sab = sab if isinstance(sab, dict) else {}
        keywords = sab.get("keywords", [])
        self.keywords: List[str] = ([str(k) for k in keywords]
                                    if isinstance(keywords, list) else [])
        self.max_items: int = _int(sab.get("max_items"), 4)
        self.max_name_length: int = _int(sab.get("max_name_length"), 45)

        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    def _load_config(self) -> Dict[str, Any]:
        config = read_json(self.config_file, {})
        return config if isinstance(config, dict) else {}

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            return self._session

    async def cog_unload(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_sabnzbd_info(self) -> Dict[str, Any]:
        if not self.enabled:
            return _empty()

        session = await self._get_session()
        url = urljoin(self.url.rstrip("/") + "/", "api")
        params = {"apikey": self.api_key, "output": "json", "mode": "queue"}

        try:
            async with session.get(url, params=params) as response:
                if not response.ok:
                    logger.error("SABnzbd returned HTTP %s", response.status)
                    return _empty()
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("SABnzbd request failed: %s", exc)
            return _empty()

        if isinstance(data, dict) and data.get("status") is False:
            # A bad API key answers 200 with {"status": false, "error": ...};
            # without this it rendered as "Queue is empty" and logged nothing.
            logger.error("SABnzbd rejected the request: %s",
                         data.get("error") or "unknown error")
            return _empty()
        queue = (data or {}).get("queue", {})
        slots = queue.get("slots", []) or []
        speed = self._format_speed(queue.get("kbpersec", "0"))

        downloads = [
            {
                "name": slot.get("filename", "Unknown"),
                "progress": self._float(slot.get("percentage")),
                "timeleft": slot.get("timeleft") or "",
                # Use "mb" (a numeric string) rather than "size", which SABnzbd
                # already formats as e.g. "2.3 GB" and cannot be parsed as a float.
                "size": self._format_size(slot.get("mb")),
            }
            for slot in slots[: self.max_items]
        ]

        return {
            "downloads": downloads,
            # EVERY slot, not just the displayed ones, and carrying the id
            # SABnzbd identifies a job by. The dashboard watches this set to
            # notice completions: an nzo_id that was at ~100% and is now gone
            # finished. That inference exists because Sonarr and Radarr
            # delete the history entry the moment they import, so asking
            # SABnzbd what completed returns nothing.
            "slots": [
                {
                    "nzo_id": slot.get("nzo_id") or slot.get("filename") or "",
                    "name": slot.get("filename", "Unknown"),
                    "progress": self._float(slot.get("percentage")),
                    "size": self._format_size(slot.get("mb")),
                }
                for slot in slots
            ],
            "queued_total": len(slots),
            "speed": speed if slots else None,
            "diskspace1": self._format_disk(queue.get("diskspace1")),
            "diskspacetotal1": self._format_disk(queue.get("diskspacetotal1")),
        }

    async def get_history(self, limit: int = 10,
                          max_age_hours: float = 0.0) -> List[Dict[str, Any]]:
        """Recently FINISHED jobs, failures excluded.

        "Completed" alone is not enough to trust. SABnzbd will leave a slot
        marked Completed while carrying a fail_message when post-processing
        partially failed -- unpack errors, repair that could not finish --
        and those produce no watchable file. Three things have to agree
        before a job is reported as done:

          status == "Completed"   SAB's own verdict
          no fail_message         nothing went wrong on the way
          storage path present    files were actually written somewhere

        The third is what catches a job that was deleted from history's
        point of view but still reads as complete.
        """
        if not self.enabled:
            return []

        session = await self._get_session()
        url = urljoin(self.url.rstrip("/") + "/", "api")
        params = {
            "apikey": self.api_key, "output": "json", "mode": "history",
            "start": "0",
            # Over-fetch: failures are interleaved with successes, so asking
            # for exactly `limit` rows can return fewer than `limit` good ones.
            "limit": str(max(1, min(200, limit * 5))),
        }
        try:
            async with session.get(url, params=params) as response:
                if not response.ok:
                    logger.error("SABnzbd history returned HTTP %s",
                                 response.status)
                    return []
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("SABnzbd history request failed: %s", exc)
            return []

        slots = ((data or {}).get("history") or {}).get("slots") or []
        cutoff = 0.0
        if max_age_hours > 0:
            cutoff = time.time() - max_age_hours * 3600.0

        done: List[Dict[str, Any]] = []
        for slot in slots:
            if str(slot.get("status") or "").lower() != "completed":
                continue
            if str(slot.get("fail_message") or "").strip():
                continue
            if not str(slot.get("storage") or "").strip():
                continue
            try:
                completed = float(slot.get("completed") or 0)
            except (TypeError, ValueError):
                completed = 0.0
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

    # ------------------------------------------------------------- formatting

    def clean_name(self, name: str) -> str:
        """Strip release-tag noise and truncate for display.

        Keyword matching is case-insensitive, so a config listing "4K" also
        trims "4k" -- the original was case-sensitive, which is why the
        upstream config had to list both "German" and "GERMAN".
        """
        cleaned = name or ""
        lowered = cleaned.lower()
        # Only cut at a keyword that appears after the title, never at index 0.
        cuts = [
            position
            for keyword in self.keywords
            if (position := lowered.find(keyword.lower())) > 0
        ]
        if cuts:
            cleaned = cleaned[: min(cuts)]
        cleaned = cleaned.replace(".", " ").strip(" -_")

        # Fall back before truncating, so a name that cleans away to nothing
        # still respects max_name_length.
        if not cleaned:
            cleaned = (name or "Unknown").replace(".", " ").strip(" -_") or "Unknown"
        if len(cleaned) > self.max_name_length:
            cleaned = cleaned[: self.max_name_length - 1].rstrip() + "…"
        return cleaned

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _format_size(cls, size: Any) -> str:
        """SABnzbd reports queue slot sizes in MB."""
        value = cls._float(size, -1)
        if value < 0:
            return ""
        units = ["MB", "GB", "TB"]
        index = 0
        while value >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        return f"{value:.2f} {units[index]}"

    @classmethod
    def _format_speed(cls, kbpersec: Any) -> str:
        value = cls._float(kbpersec)
        if value <= 0:
            return ""
        units = ["KB/s", "MB/s", "GB/s"]
        index = 0
        while value >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        return f"{value:.1f} {units[index]}"

    @classmethod
    def _format_disk(cls, value: Any) -> Optional[str]:
        """SABnzbd reports disk space in GB."""
        size = cls._float(value, -1)
        if size < 0:
            return None
        if size >= 1024:
            return f"{size / 1024:.2f} TB"
        return f"{size:.0f} GB"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SABnzbd(bot))
