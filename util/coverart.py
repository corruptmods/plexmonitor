"""Album covers from the Cover Art Archive (coverartarchive.org).

Plex matches music to MusicBrainz and keeps the album's ID as a guid
("mbid://<uuid>"). The Cover Art Archive serves covers for those IDs at
public URLs, which is what a remote-artwork card needs: Discord fetches the
image itself, so nothing is uploaded and no credentials are involved.

Plex may hold a release-group ID or a release ID, so both are tried. Answers
-- including "no cover" -- are cached, so a song on repeat costs one lookup.
"""

import asyncio
import logging
import re
import time
from typing import Dict, Iterable, Optional, Tuple

import aiohttp

from util import safehttp

logger = logging.getLogger("plexmonitor.coverart")

BASE = "https://coverartarchive.org"
USER_AGENT = "PlexMonitor (+https://github.com/corruptmods/plexmonitor)"
HIT_TTL = 24 * 3600.0
MISS_TTL = 6 * 3600.0
MAX_ANSWER = 1024 * 1024            # the JSON listing, not the image
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                   r"-[0-9a-f]{12}$")


def mbids(guids: Iterable[str]) -> list:
    """The MusicBrainz IDs among Plex guids ("mbid://<uuid>")."""
    found = []
    for guid in guids or []:
        provider, sep, ident = str(guid or "").partition("://")
        ident = ident.strip().lower()
        if sep and provider.lower() == "mbid" and _MBID.match(ident):
            found.append(ident)
    return found


class CoverArtClient:
    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[Optional[str], float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, headers={"User-Agent": USER_AGENT})
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def front(self, ids: Iterable[str]) -> Optional[str]:
        """A public URL of the front cover for the first of these
        MusicBrainz IDs that has one, or None."""
        for mbid in ids:
            url = await self._front(mbid)
            if url:
                return url
        return None

    async def _front(self, mbid: str) -> Optional[str]:
        cached = self._cache.get(mbid)
        now = time.monotonic()
        if cached and now < cached[1]:
            return cached[0]
        lock = self._locks.setdefault(mbid, asyncio.Lock())
        async with lock:
            cached = self._cache.get(mbid)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
            url, known = None, True
            for kind in ("release-group", "release"):
                try:
                    url = await self._listing(f"{BASE}/{kind}/{mbid}")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError,
                        safehttp.TooBig, ValueError) as exc:
                    # Unreachable is not "no cover": try again sooner.
                    logger.debug("Cover Art Archive lookup failed: %s", exc)
                    known = False
                    break
                if url:
                    break
            ttl = HIT_TTL if url else (MISS_TTL if known else 300.0)
            self._cache[mbid] = (url, time.monotonic() + ttl)
            if len(self._cache) > 2000:
                for key in sorted(self._cache,
                                  key=lambda k: self._cache[k][1])[:500]:
                    self._cache.pop(key, None)
            return url

    async def _listing(self, url: str) -> Optional[str]:
        session = await self._get_session()
        async with await safehttp.request(session, "GET", url) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            data = await safehttp.read_json(response, limit=MAX_ANSWER)
        images = data.get("images") if isinstance(data, dict) else None
        if not isinstance(images, list):
            return None
        chosen = next((i for i in images if isinstance(i, dict)
                       and i.get("front")), None)
        if chosen is None:
            return None
        thumbs = chosen.get("thumbnails") or {}
        link = (thumbs.get("500") or thumbs.get("large") or thumbs.get("250")
                or thumbs.get("small") or chosen.get("image") or "")
        link = str(link)
        if link.startswith("http://"):
            link = "https://" + link[len("http://"):]
        return link if link.startswith("https://") else None
