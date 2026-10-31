"""Downloads cog -- every download client the dashboard reads, as one.

The clients Seerr AI supports, any number of them at once:

  SABnzbd       SABNZBD_URL, SABNZBD_API_KEY
  NZBGet        NZBGET_URL, NZBGET_USERNAME, NZBGET_PASSWORD
  qBittorrent   QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD
  Transmission  TRANSMISSION_URL, TRANSMISSION_USERNAME, TRANSMISSION_PASSWORD
  Deluge        DELUGE_URL, DELUGE_PASSWORD
  rTorrent      RTORRENT_URL, RTORRENT_USERNAME, RTORRENT_PASSWORD

DOWNLOAD_CATEGORIES (comma-separated) limits every client to those
categories (labels, for the torrent clients that call them that), and
DOWNLOAD_VERIFY_SSL=false accepts self-signed certificates.

The dashboard asks this cog, never a client:

  get_info()     the queue: the first few jobs for display, every job's id
                 and time left (to notice completions), the jobs that
                 finished, the total count, combined speed, and which
                 clients couldn't be read
  get_history()  recently finished jobs, newest first, from every client
  clean_name()   a release name trimmed for display

Display settings live in the `downloads` section of config.json (the older
`sabnzbd` section is still read when there is no `downloads` one).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List

from discord.ext import commands
from dotenv import load_dotenv

from util.deluge import DelugeClient
from util.jsonfile import data_dir, read_json
from util.nzbget import NzbgetClient
from util.qbittorrent import QbittorrentClient
from util.rtorrent import RtorrentClient
from util.sabnzbd import SabnzbdClient
from util.transmission import TransmissionClient

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
            "diskspace1": None, "diskspacetotal1": None, "unreachable": []}


# What a release name says after its title: the year, or SxxEyy. Release
# tags (1080p, WEB-DL...) are only looked for after it, so a title that
# happens to contain a tag word ("The English", "Godless") stays whole.
_TITLE_END = re.compile(
    r"(?<![a-z0-9])(?:(?:19|20)\d{2}|s\d{1,2}(?:e\d{1,3})+|s\d{1,2})"
    r"(?![a-z0-9])")


def _cut_at(lowered: str, keyword: str, start: int) -> int:
    """Where a release tag starts (as a whole word, not inside one), at or
    after `start`; -1 when it doesn't appear."""
    match = re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower())
                       + r"(?![a-z0-9])").search(lowered, start)
    return match.start() if match else -1


def _tags_before(lowered: str, end: int, keywords: List[str]) -> int:
    """Where an unbroken run of release tags ending at `end` starts (only
    separators between them), or `end` when there is none."""
    cut = end
    while True:
        head = lowered[:cut].rstrip(" ._-[]()")
        found = next((len(head) - len(k) for k in keywords
                      if head.endswith(k) and (len(head) == len(k)
                                               or not head[-len(k) - 1]
                                               .isalnum())), None)
        if found is None or found <= 0:
            return cut
        cut = found


def _off(value: str) -> bool:
    return value.strip().lower() in ("false", "0", "no", "off")


class Downloads(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clients: List[Any] = self.configured_clients()
        self.enabled = bool(self.clients)
        if self.enabled:
            logger.info("Download clients: %s",
                        ", ".join(c.name for c in self.clients))
        else:
            logger.info("No download client set -- download panels "
                        "disabled")

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

    @staticmethod
    def configured_clients() -> List[Any]:
        env = os.getenv
        shared = {
            "categories": env("DOWNLOAD_CATEGORIES", "").split(","),
            "verify_ssl": not _off(env("DOWNLOAD_VERIFY_SSL", "true")),
        }
        clients: List[Any] = []
        if env("SABNZBD_URL") and env("SABNZBD_API_KEY"):
            clients.append(SabnzbdClient(env("SABNZBD_URL"),
                                         env("SABNZBD_API_KEY"), **shared))
        if env("NZBGET_URL"):
            clients.append(NzbgetClient(env("NZBGET_URL"),
                                        env("NZBGET_USERNAME", ""),
                                        env("NZBGET_PASSWORD", ""), **shared))
        if env("QBITTORRENT_URL"):
            clients.append(QbittorrentClient(
                env("QBITTORRENT_URL"), env("QBITTORRENT_USERNAME", ""),
                env("QBITTORRENT_PASSWORD", ""), **shared))
        if env("TRANSMISSION_URL"):
            clients.append(TransmissionClient(
                env("TRANSMISSION_URL"), env("TRANSMISSION_USERNAME", ""),
                env("TRANSMISSION_PASSWORD", ""), **shared))
        if env("DELUGE_URL"):
            clients.append(DelugeClient(env("DELUGE_URL"),
                                        env("DELUGE_PASSWORD", ""), **shared))
        if env("RTORRENT_URL"):
            clients.append(RtorrentClient(
                env("RTORRENT_URL"), env("RTORRENT_USERNAME", ""),
                env("RTORRENT_PASSWORD", ""), **shared))
        return clients

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

        `slots` lists every job (not just the displayed ones) with its id
        ("<client>:<id>"); the dashboard notices a completion when an id
        that was at ~100% disappears. `unreachable` names the clients that
        couldn't be read: their jobs aren't in `slots`, and mustn't look
        like they all just left the queue.
        """
        if not self.enabled:
            return _empty()
        results = await asyncio.gather(*(c.queue() for c in self.clients),
                                       return_exceptions=True)
        items: List[Dict[str, Any]] = []
        finished: List[str] = []
        speed = 0.0
        disk_free = disk_total = None
        unreachable: List[str] = []
        for client, result in zip(self.clients, results):
            if isinstance(result, BaseException) or result is None:
                if isinstance(result, BaseException):
                    logger.warning("%s queue failed: %s", client.name, result)
                unreachable.append(client.name)
                continue
            items.extend(result["items"])
            finished.extend(result.get("finished") or [])
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
            "unreachable": unreachable,
            "slots": [{
                "id": item["id"],
                "name": item["name"],
                "progress": item["progress"],
                "size": format_size(item.get("size_bytes", -1)),
                "status": item.get("status") or "",
                "eta_s": item.get("eta_s"),
            } for item in items],
            # Jobs the clients still hold that are done downloading.
            "finished": finished,
        }
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
        case-insensitively as whole words, after the title: after its year
        or SxxEyy when the name has one, otherwise after its first word."""
        cleaned = name or ""
        lowered = cleaned.lower()
        keywords = [k.strip().lower() for k in self.keywords if k.strip()]
        title_end = _TITLE_END.search(lowered, 1)
        start = title_end.end() if title_end else 1
        cuts = [position for keyword in keywords
                if (position := _cut_at(lowered, keyword, start)) > 0]
        if not cuts and title_end:
            # Tags placed before the year ("Title.1080p.2019"): only a run of
            # them right up against it, with a tag like 1080p in it, so a
            # title word that happens to be a keyword stays ("The English
            # Patient 1996", "The English S01E01").
            cut = _tags_before(lowered, title_end.start(), keywords)
            if 0 < cut < title_end.start() and any(
                    ch.isdigit() for ch in lowered[cut:title_end.start()]):
                cuts = [cut]
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
