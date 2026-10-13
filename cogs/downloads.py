"""Downloads cog -- every download client the dashboard reads, as one.

The clients Seerr AI supports: SABnzbd (SABNZBD_URL, SABNZBD_API_KEY) and
qBittorrent (QBITTORRENT_URL, plus QBITTORRENT_USERNAME/PASSWORD unless
qBittorrent lets this address in without signing in). Any, both or neither.

The dashboard asks this cog, never a client:

  get_info()     the queue: the first few jobs for display, every job's id
                 (to notice completions), the total count, combined speed
  get_history()  recently finished jobs, newest first, from every client
  clean_name()   a release name trimmed for display

Display settings live in the `downloads` section of config.json (the older
`sabnzbd` section is still read when there is no `downloads` one).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

from discord.ext import commands
from dotenv import load_dotenv

from util.jsonfile import data_dir, read_json
from util.qbittorrent import QbittorrentClient
from util.sabnzbd import SabnzbdClient

if os.getenv("RUNNING_IN_DOCKER", "false").lower() != "true":
    load_dotenv()

logger = logging.getLogger("plexmonitor.downloads")


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _scaled(value: float, units: List[str], decimals: int) -> str:
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.{decimals}f} {units[index]}"


def format_size(size_bytes: float) -> str:
    """Bytes -> "2.31 GB" (MB at least, like SABnzbd shows them)."""
    if size_bytes is None or size_bytes < 0:
        return ""
    return _scaled(size_bytes / (1024 * 1024), ["MB", "GB", "TB"], 2)


def format_speed(bytes_per_second: float) -> str:
    """Bytes per second -> "12.4 MB/s"; "" when idle."""
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return _scaled(bytes_per_second / 1024, ["KB/s", "MB/s", "GB/s"], 1)


def _empty() -> Dict[str, Any]:
    return {"downloads": [], "queued_total": 0, "speed": None,
            "diskspace1": None, "diskspacetotal1": None}


class Downloads(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clients: List[Any] = []
        if os.getenv("SABNZBD_URL") and os.getenv("SABNZBD_API_KEY"):
            self.clients.append(SabnzbdClient(os.environ["SABNZBD_URL"],
                                              os.environ["SABNZBD_API_KEY"]))
        if os.getenv("QBITTORRENT_URL"):
            self.clients.append(QbittorrentClient(
                os.environ["QBITTORRENT_URL"],
                os.getenv("QBITTORRENT_USERNAME", ""),
                os.getenv("QBITTORRENT_PASSWORD", ""),
                categories=os.getenv("QBITTORRENT_CATEGORIES", "").split(","),
                verify_ssl=os.getenv("QBITTORRENT_VERIFY_SSL", "true")
                .lower() != "false"))
        self.enabled = bool(self.clients)
        if self.enabled:
            logger.info("Download clients: %s",
                        ", ".join(c.name for c in self.clients))
        else:
            logger.info("No download client set (SABnzbd or qBittorrent) -- "
                        "download panels disabled")

        config = read_json(os.path.join(data_dir(), "config.json"), {})
        config = config if isinstance(config, dict) else {}
        section = config.get("downloads")
        if not isinstance(section, dict):
            section = config.get("sabnzbd")
        section = section if isinstance(section, dict) else {}
        keywords = section.get("keywords", [])
        self.keywords: List[str] = ([str(k) for k in keywords]
                                    if isinstance(keywords, list) else [])
        self.max_items: int = _int(section.get("max_items"), 4)
        self.max_name_length: int = _int(section.get("max_name_length"), 45)

    async def cog_unload(self) -> None:
        for client in self.clients:
            try:
                await client.close()
            except Exception:
                logger.debug("Closing %s failed", client.name, exc_info=True)

    @property
    def names(self) -> List[str]:
        return [c.name for c in self.clients]

    async def get_info(self) -> Dict[str, Any]:
        """Every client's queue as one.

        `slots` lists every job (not just the displayed ones) with its id;
        the dashboard notices a completion when an id that was at ~100%
        disappears. When any client couldn't be read, `slots` is left out:
        its jobs would otherwise look like they all just left the queue.
        """
        if not self.enabled:
            return _empty()
        results = await asyncio.gather(*(c.queue() for c in self.clients),
                                       return_exceptions=True)
        items: List[Dict[str, Any]] = []
        speed = 0.0
        disk_free = disk_total = None
        complete = True
        for client, result in zip(self.clients, results):
            if isinstance(result, BaseException) or result is None:
                if isinstance(result, BaseException):
                    logger.warning("%s queue failed: %s", client.name, result)
                complete = False
                continue
            items.extend(result["items"])
            speed += result.get("speed_bps") or 0.0
            disk_free = disk_free or result.get("disk_free")
            disk_total = disk_total or result.get("disk_total")
        # Jobs actually moving first, each client's own order kept.
        shown = sorted(items, key=lambda i: bool(i.get("status")))
        info = {
            "downloads": [{
                "name": item["name"],
                "progress": item["progress"],
                "timeleft": item.get("timeleft") or "",
                "size": format_size(item.get("size_bytes", -1)),
                "status": item.get("status") or "",
                "speed": format_speed(item.get("speed_bps") or 0),
                "client": item.get("client") or "",
            } for item in shown[: self.max_items]],
            "queued_total": len(items),
            "speed": format_speed(speed) if items else None,
            "diskspace1": disk_free,
            "diskspacetotal1": disk_total,
        }
        if complete:
            info["slots"] = [{
                "id": item["id"],
                "name": item["name"],
                "progress": item["progress"],
                "size": format_size(item.get("size_bytes", -1)),
            } for item in items]
        return info

    async def get_history(self, limit: int = 10,
                          max_age_hours: float = 0.0) -> List[Dict[str, Any]]:
        """Recently finished jobs from every client, newest first."""
        if not self.enabled:
            return []
        results = await asyncio.gather(
            *(c.history(limit, max_age_hours) for c in self.clients),
            return_exceptions=True)
        done: List[Dict[str, Any]] = []
        for client, result in zip(self.clients, results):
            if isinstance(result, BaseException):
                logger.warning("%s history failed: %s", client.name, result)
                continue
            done.extend(result)
        for item in done:
            if not item.get("size") and item.get("bytes"):
                item["size"] = format_size(float(item["bytes"]))
        done.sort(key=lambda d: float(d.get("completed") or 0), reverse=True)
        return done[:limit]

    def clean_name(self, name: str) -> str:
        """Strip release-tag noise and truncate for display. Keywords match
        case-insensitively, and only after the start of the title."""
        cleaned = name or ""
        lowered = cleaned.lower()
        cuts = [position for keyword in self.keywords
                if (position := lowered.find(keyword.lower())) > 0]
        if cuts:
            cleaned = cleaned[: min(cuts)]
        cleaned = cleaned.replace(".", " ").strip(" -_")
        # Fall back before truncating, so a name that cleans away to nothing
        # still respects max_name_length.
        if not cleaned:
            cleaned = (name or "Unknown").replace(".", " ").strip(" -_") \
                or "Unknown"
        if len(cleaned) > self.max_name_length:
            cleaned = cleaned[: self.max_name_length - 1].rstrip() + "…"
        return cleaned


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Downloads(bot))
