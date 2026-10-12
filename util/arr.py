"""Radarr / Sonarr history as the completed-downloads source.

SABnzbd's history is the obvious place to ask what finished, and on a setup
with *arr importing it is empty: Radarr and Sonarr delete the SAB entry as
soon as they have taken the files. The record is not lost, it just moved --
to the app that did the deleting, which keeps its own history and is the
only component that actually knows an import SUCCEEDED rather than merely
finished downloading.

It is also the better source for display. SABnzbd knows
"Dune.Part.Two.2024.2160p.UHD.BluRay.x265-TERMiNAL"; Radarr knows
"Dune: Part Two (2024)".

Both apps expose the same v3 shape:

    GET /api/v3/history?eventType=3&pageSize=20&sortKey=date
        &sortDirection=descending
    X-Api-Key: ...

eventType 3 is downloadFolderImported -- the import completing, not the
download. A grab that downloaded fine and failed to import never produces
one, which is exactly the distinction the panel needs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("plexmonitor.arr")

# History event types shared by both apps.
EVENT_IMPORTED = 3

DEFAULT_TIMEOUT = 12.0
# Library -> poster map lifetime, and how soon a FAILED fetch is retried.
POSTER_TTL = 6 * 3600.0
POSTER_RETRY = 300.0


def _parse_time(value: Any) -> float:
    """ISO-8601 to a unix timestamp; 0 when unparseable."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    # Both apps emit "2026-08-17T22:10:00Z"; fromisoformat wants the offset
    # spelled out until 3.11, and sub-second digits vary in length.
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        offset = ""
        for marker in ("+", "-"):
            if marker in tail:
                tail, _, rest = tail.partition(marker)
                offset = marker + rest
                break
        text = f"{head}.{tail[:6]:0<6}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _format_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit not in ("B", "KB") else \
                f"{size:.0f} {unit}"
        size /= 1024
    return ""


class ArrClient:
    """One Radarr or Sonarr instance."""

    def __init__(self, url: str, api_key: str, kind: str = "radarr",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.kind = kind
        self.enabled = bool(self.url and self.api_key)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._poster_cache: Optional[Dict[str, str]] = None
        self._poster_cache_at: Optional[float] = None
        self._poster_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self.timeout)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _norm(title: Any) -> str:
        """Letters and digits only, lowercased -- matching key."""
        return "".join(ch for ch in str(title or "").lower() if ch.isalnum())

    @classmethod
    def _title_variants(cls, title: Any) -> List[str]:
        """Every spelling of a title the other side might be using.

        The base key strips punctuation, which already covers most
        differences -- but "&" and "and" are different LETTERS, a leading
        "The" is dropped by some sources and kept by others, and a series
        title from Plex can carry its own "(2018)" suffix. Each variant is a
        separate key so any of them matches.
        """
        text = str(title or "").strip()
        if not text:
            return []
        forms = {text}
        forms.add(text.replace("&", " and "))
        forms.add(text.replace(" and ", " & "))
        # A trailing "(YYYY)" is a disambiguator, not part of the name.
        import re as _re
        bare = _re.sub(r"\s*\(\d{4}\)\s*$", "", text)
        forms.add(bare)
        forms.add(bare.replace("&", " and "))
        out = set()
        for form in forms:
            key = cls._norm(form)
            if key:
                out.add(key)
                if key.startswith("the") and len(key) > 6:
                    out.add(key[3:])
        return sorted(out)

    async def poster_map(self) -> Dict[str, str]:
        """normalised title -> public poster URL, cached for hours.

        The point of these URLs is what they are NOT: attachments. A poster
        uploaded to Discord lives on the message, and every PATCH against a
        message carrying attachments is metered against the shared limit
        that has been throttling this bot. A TMDB URL in the embed is
        fetched by Discord itself -- the message stays text-only and its
        edits stay cheap. Radarr and Sonarr both expose these as
        images[].remoteUrl on their library records.
        """
        if not self.enabled:
            return {}
        # Single-flight: every card on a page asks at once, and before this
        # each one fired its own library download.
        async with self._poster_lock:
            now = time.monotonic()
            if (self._poster_cache_at is not None
                    and now - self._poster_cache_at < POSTER_TTL):
                return self._poster_cache or {}
            records = await self._fetch_library()
            if records is None:
                # Remember the failure for a few minutes. Not caching it made
                # every card retry the network: with the host not answering,
                # ten cards x two apps x a 12s timeout held the render lock
                # for minutes and froze the whole dashboard.
                self._poster_cache_at = now - POSTER_TTL + POSTER_RETRY
                return self._poster_cache or {}
            return self._index_posters(records, now)

    async def _fetch_library(self) -> Optional[List[Any]]:
        """Raw library records, or None on any failure. Never raises."""
        path = "/api/v3/movie" if self.kind == "radarr" else "/api/v3/series"
        try:
            session = await self._get_session()
            async with session.get(self.url + path,
                                   headers={"X-Api-Key": self.api_key}
                                   ) as response:
                if not response.ok:
                    logger.warning("%s library returned HTTP %s",
                                   self.kind, response.status)
                    return None
                records = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("%s library request failed: %s", self.kind, exc)
            return None
        except Exception as exc:                        # malformed JSON, etc.
            logger.warning("%s library unreadable: %s", self.kind, exc)
            return None
        return records if isinstance(records, list) else []

    def _index_posters(self, records, now: float) -> Dict[str, str]:
        """Build and cache the lookup map from raw library records."""
        out: Dict[str, str] = {}
        for record in (records or []):
            if not isinstance(record, dict):
                continue
            url = next((i.get("remoteUrl") for i in record.get("images") or []
                        if i.get("coverType") == "poster"
                        and i.get("remoteUrl")), None)
            title = record.get("title")
            if not (title and url):
                continue
            year = record.get("year")
            # Provider IDs first: Radarr carries tmdbId (and imdbId), Sonarr
            # carries tvdbId (and often tmdbId/imdbId). These are the keys
            # Tautulli reports for a playing item, and they are immune to
            # every way two systems can spell the same title differently.
            for field, prefix in (("tmdbId", "tmdb"), ("tvdbId", "tvdb"),
                                  ("imdbId", "imdb")):
                ident = record.get(field)
                if ident:
                    out[f"id:{prefix}:{ident}"] = url
            # Then every title spelling worth trying.
            for variant in self._title_variants(title):
                out.setdefault(variant, url)
                if year:
                    # Year-qualified key wins, so two films sharing a title
                    # ("Dune") resolve to the right poster.
                    out[f"{variant}{year}"] = url
        self._poster_cache = out
        self._poster_cache_at = now
        logger.info("%s poster map: %d titles", self.kind, len(out))
        return out

    def _title(self, record: Dict[str, Any]) -> str:
        """Human title, falling back to the release name.

        includeMovie / includeSeries are requested, but a record can still
        arrive without them (the movie was deleted since, an older API
        build ignores the flag), and a release name is far better than
        "Unknown".
        """
        if self.kind == "sonarr":
            series = (record.get("series") or {}).get("title")
            episode = record.get("episode") or {}
            if series:
                season = episode.get("seasonNumber")
                number = episode.get("episodeNumber")
                if season is not None and number is not None:
                    return f"{series} S{int(season):02d}E{int(number):02d}"
                return str(series)
        else:
            movie = record.get("movie") or {}
            title = movie.get("title")
            if title:
                year = movie.get("year")
                return f"{title} ({year})" if year else str(title)
        return str(record.get("sourceTitle") or "Unknown")

    async def imported(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Recent successful imports, newest first. Never raises."""
        if not self.enabled:
            return []
        params = {
            "eventType": str(EVENT_IMPORTED),
            "pageSize": str(max(1, min(100, limit))),
            "page": "1",
            "sortKey": "date",
            "sortDirection": "descending",
        }
        params["includeSeries" if self.kind == "sonarr"
               else "includeMovie"] = "true"
        if self.kind == "sonarr":
            params["includeEpisode"] = "true"

        try:
            session = await self._get_session()
            async with session.get(
                    f"{self.url}/api/v3/history",
                    params=params,
                    headers={"X-Api-Key": self.api_key}) as response:
                if not response.ok:
                    logger.warning("%s history returned HTTP %s",
                                   self.kind, response.status)
                    return []
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("%s history request failed: %s", self.kind, exc)
            return []
        except Exception as exc:                       # malformed JSON, etc.
            logger.warning("%s history unreadable: %s", self.kind, exc)
            return []

        records = data.get("records") if isinstance(data, dict) else data
        out: List[Dict[str, Any]] = []
        for record in (records or []):
            if not isinstance(record, dict):
                continue
            # The API filters by eventType, but older builds have ignored
            # the parameter -- so check rather than trust.
            event = record.get("eventType")
            if event not in (EVENT_IMPORTED, "downloadFolderImported"):
                continue
            out.append({
                "name": self._title(record),
                "size": _format_size((record.get("data") or {}).get("size")),
                "completed": _parse_time(record.get("date")),
                "source": self.kind,
            })
            if len(out) >= limit:
                break
        return out
