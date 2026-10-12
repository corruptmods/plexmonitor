"""Async Tautulli API client.

Tautulli is the preferred source for stream and library data:

  * one cheap JSON call returns every active session, already enriched with
    the transcode decision, stream bitrate and the user's friendly name --
    all things that cost extra round trips through plexapi
  * library counts come from Tautulli's own database, so we never enumerate
    library items just to count them
  * `server_status` is answered from Tautulli's persistent websocket to Plex,
    so it costs nothing and is near-real-time

Field names and units below were verified against Tautulli v2.17.2. Almost
everything Tautulli passes through from Plex's XML arrives as a *string*,
including numbers, so every value is cast defensively.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("plexmonitor.tautulli")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _pretty_resolution(value: str) -> str:
    """Tautulli reports "4k"/"sd" in lowercase; everything else is already fine."""
    lowered = (value or "").strip().lower()
    if lowered in ("4k", "2160", "2160p"):
        return "4K"
    if lowered == "sd":
        return "SD"
    return value


class TautulliError(RuntimeError):
    pass


class TautulliClient:
    """Minimal async client for the handful of commands the dashboard needs."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0,
                 verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        # One session reused for the bot's lifetime -- connection pooling means
        # we are not paying for a TCP + TLS handshake on every dashboard tick.
        async with self._lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(ssl=None if self.verify_ssl else False)
                self._session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, cmd: str, **params: Any) -> Any:
        """Run an API command and return the unwrapped `data` payload."""
        session = await self._get_session()
        query = {"apikey": self.api_key, "cmd": cmd, "out_type": "json", **params}
        url = f"{self.base_url}/api/v2"

        try:
            async with session.get(url, params=query) as response:
                # Tautulli returns 400 with a JSON error body, so read the body
                # before raising on status -- otherwise the useful message is lost.
                text = await response.text()
                if response.status >= 400:
                    raise TautulliError(f"HTTP {response.status} from Tautulli: {text[:200]}")
                try:
                    payload = await response.json(content_type=None)
                except Exception as exc:
                    raise TautulliError(f"Tautulli returned non-JSON: {text[:200]}") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # A total-timeout raises the builtin TimeoutError, which is NOT an
            # aiohttp.ClientError -- catching only the latter let a hung
            # Tautulli escape as an unhandled exception and freeze the
            # dashboard on its last render instead of flipping it to offline.
            raise TautulliError(f"Could not reach Tautulli: {exc or type(exc).__name__}") from exc

        envelope = (payload or {}).get("response") or {}
        if envelope.get("result") != "success":
            raise TautulliError(envelope.get("message") or f"Tautulli rejected cmd={cmd}")
        return envelope.get("data")

    # ---------------------------------------------------------------- activity

    async def get_activity(self) -> Dict[str, Any]:
        """Current streams, normalised into the shape the dashboard renders.

        Note: Tautulli reports `result: "success"` with an empty `data` when
        Plex itself is unreachable, so the presence of a `sessions` key is the
        real health signal -- not the result field.
        """
        data = await self.call("get_activity")
        if not isinstance(data, dict) or "sessions" not in data:
            raise TautulliError("Tautulli returned no session data (is Plex reachable?)")

        sessions = [self._normalise_session(s) for s in (data.get("sessions") or [])]
        return {
            # len(sessions) rather than data["stream_count"]: Tautulli reads
            # stream_count from Plex before filtering out background theme
            # music, so the two can disagree.
            "count": len(sessions),
            "transcode_count": _int(data.get("stream_count_transcode")),
            "direct_play_count": _int(data.get("stream_count_direct_play")),
            "direct_stream_count": _int(data.get("stream_count_direct_stream")),
            "total_bandwidth": _int(data.get("total_bandwidth")),   # kbps
            "lan_bandwidth": _int(data.get("lan_bandwidth")),
            "wan_bandwidth": _int(data.get("wan_bandwidth")),
            "sessions": sessions,
        }

    def _normalise_session(self, s: Dict[str, Any]) -> Dict[str, Any]:
        media_type = _str(s.get("media_type"))
        decision = _str(s.get("transcode_decision")).lower()

        return {
            "media_type": media_type,
            "title": _str(s.get("title")),
            "series": _str(s.get("grandparent_title")),
            "season": _int(s.get("parent_media_index")),
            "episode": _int(s.get("media_index")),
            "year": _str(s.get("year")),
            "full_title": _str(s.get("full_title")),
            "library": _str(s.get("library_name")),

            # friendly_name is Tautulli's per-user display override and
            # already falls back to the plex.tv username when unset.
            "user": _str(s.get("friendly_name")) or _str(s.get("username"), "Unknown"),

            "state": _str(s.get("state"), "playing"),
            "paused": _str(s.get("state")).lower() == "paused",
            "view_offset": _int(s.get("view_offset")),        # ms
            "duration": _int(s.get("stream_duration")) or _int(s.get("duration")),  # ms
            "progress": _int(s.get("progress_percent")),      # 0-100

            "player": _str(s.get("player")) or _str(s.get("product"), "Unknown"),
            "product": _str(s.get("product")),
            "platform": _str(s.get("platform")),

            "decision": decision,                              # transcode|copy|direct play
            "video_decision": _str(s.get("stream_video_decision")),
            "audio_decision": _str(s.get("stream_audio_decision")),
            "subtitle_decision": _str(s.get("stream_subtitle_decision")),
            "transcode_speed": _str(s.get("transcode_speed")),
            "hw_encode": bool(_int(s.get("transcode_hw_encoding"))),

            "resolution": _str(s.get("stream_video_full_resolution")),
            "source_resolution": _str(s.get("video_full_resolution")),
            "video_codec": _str(s.get("video_codec")),
            "stream_video_codec": _str(s.get("stream_video_codec")),
            "audio_codec": _str(s.get("audio_codec")),
            "stream_audio_codec": _str(s.get("stream_audio_codec")),
            "bitrate": _int(s.get("stream_bitrate")),          # kbps
            "bandwidth": _int(s.get("bandwidth")),             # kbps
            "location": _str(s.get("location")),               # lan|wan

            "session_key": _str(s.get("session_key")),
            "rating_key": _str(s.get("rating_key")),
            # Provider IDs -- "tmdb://12345", "tvdb://678", "imdb://tt..." --
            # for the item itself and, for an episode, for its show. Radarr
            # and Sonarr index their libraries by exactly these, so a poster
            # lookup by ID cannot be defeated by "&" vs "and" or a dropped
            # "The", which a title lookup can. Older Tautulli omits them and
            # the list is simply empty.
            "guids": [str(g) for g in (s.get("guids") or []) if g],
            "grandparent_guids": [str(g) for g in
                                  (s.get("grandparent_guids") or []) if g],
            "original_title": _str(s.get("original_title")),
            # Poster path for the card thumbnail: the show's poster for
            # episodes (an episode thumb is a video still, not art), the
            # movie's own poster otherwise.
            "poster": (
                _str(s.get("grandparent_thumb")) or _str(s.get("parent_thumb"))
                if media_type in ("episode", "track") else _str(s.get("thumb"))
            ) or _str(s.get("thumb")),
        }

    @staticmethod
    def describe_playback(session: Dict[str, Any]) -> str:
        """Short human label for how a stream is being delivered.

        Tautulli never exposes Plex's own `transcodeReason` string, so this
        infers the reason from the individual decision fields the way
        Tautulli's own web UI does.
        """
        decision = session.get("decision", "")
        if decision == "transcode":
            reasons = []
            if session.get("subtitle_decision") == "burn":
                reasons.append("subtitles")
            if session.get("video_decision") == "transcode":
                src = _pretty_resolution(session.get("source_resolution", ""))
                out = _pretty_resolution(session.get("resolution", ""))
                if src and out and src != out:
                    reasons.append(f"{src}→{out}")
                else:
                    # Same resolution in and out means the codec is what
                    # forced it -- naming those is more useful than "video".
                    src_codec = session.get("video_codec", "").upper()
                    out_codec = session.get("stream_video_codec", "").upper()
                    reasons.append(
                        f"{src_codec}→{out_codec}"
                        if src_codec and out_codec and src_codec != out_codec
                        else "video"
                    )
            elif session.get("audio_decision") == "transcode":
                src_codec = session.get("audio_codec", "").upper()
                out_codec = session.get("stream_audio_codec", "").upper()
                reasons.append(
                    f"{src_codec}→{out_codec}"
                    if src_codec and out_codec and src_codec != out_codec
                    else "audio"
                )
            label = "Transcode"
            if session.get("hw_encode"):
                label += " (HW)"
            return f"{label} · {', '.join(reasons)}" if reasons else label
        if decision == "copy":
            return "Direct stream"
        return "Direct play"

    async def get_image(self, img_path: str, width: int = 240,
                        height: int = 360) -> Optional[bytes]:
        """Fetch poster art via Tautulli's image proxy.

        Discord's servers cannot reach a LAN-only Tautulli, so poster URLs
        are useless in embeds -- instead the caller attaches these bytes to
        the message and references them as attachment://.
        """
        if not img_path:
            return None
        session = await self._get_session()
        params = {
            "apikey": self.api_key, "cmd": "pms_image_proxy",
            "img": img_path, "width": width, "height": height,
            "img_format": "jpg", "fallback": "poster",
        }
        try:
            async with session.get(f"{self.base_url}/api/v2", params=params) as response:
                if response.status != 200:
                    return None
                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type:
                    return None  # error envelope came back as JSON
                data = await response.read()
                return data if data else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("Poster fetch failed for %s: %s", img_path, exc)
            return None

    # --------------------------------------------------------------- libraries

    async def get_libraries(self) -> List[Dict[str, Any]]:
        """Per-library counts, read from Tautulli's database (no Plex round trip).

        For a TV library: count = shows, parent_count = seasons,
        child_count = episodes. Movie libraries omit or null the latter two.
        """
        # Tautulli slices with filtered[start:start+length], so length=0 returns
        # an EMPTY list and length=-1 drops the last row. Pass a large finite
        # value to lift the DataTables default page size of 25.
        data = await self.call("get_libraries_table", length=1000)
        rows = (data or {}).get("data") if isinstance(data, dict) else data
        libraries = []
        for row in rows or []:
            libraries.append({
                "section_id": _str(row.get("section_id")),
                "name": _str(row.get("section_name")),
                "type": _str(row.get("section_type")),
                "count": _int(row.get("count")),
                "parent_count": _int(row.get("parent_count")),
                "child_count": _int(row.get("child_count")),
                "is_active": bool(_int(row.get("is_active"), 1)),
            })
        return libraries

    # ------------------------------------------------------------------ status

    async def server_status(self) -> Optional[bool]:
        """Whether Tautulli's websocket to Plex is currently connected.

        Returns None when Tautulli has not yet attempted a connection --
        `connected` is null in that case, which is not the same as False.
        """
        data = await self.call("server_status")
        if not isinstance(data, dict):
            return None
        value = data.get("connected")
        return None if value is None else bool(value)

