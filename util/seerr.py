"""Seerr (Overseerr / Jellyseerr lineage) API client.

Every request the bot makes on someone's behalf goes through Seerr rather
than straight to Radarr/Sonarr, for three reasons:

  * Seerr enforces each person's permissions and quotas. The bot
    authenticates with the admin API key and names the person it is acting
    for in X-Api-User, so Seerr applies THEIR limits, not the admin's.
  * Requests show up in Seerr's own history, so Maintainerr's rule that
    removes a request alongside its media keeps working.
  * Availability is Seerr's to decide. It already syncs with Plex, so "is
    this on Plex" and "has this request finished" have one source of truth.

Identity: Seerr stores Discord user IDs on each profile (Settings ->
Notifications -> Discord). Older builds keep one ID as ``discordId``; builds
from mid-2026 keep a list as ``discordIds``. Both shapes are read.

Verified against upstream seerr-team/seerr source (routes/request.ts,
routes/issue.ts, routes/search.ts, routes/user, middleware/auth.ts).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import aiohttp
from yarl import URL

logger = logging.getLogger("plexmonitor.seerr")

DEFAULT_TIMEOUT = 12.0
# Discord user -> Seerr user map lifetime, and the floor between forced
# refreshes (a stranger pressing Request repeatedly must not turn into a
# user-list download per keystroke).
LINK_TTL = 600.0
LINK_FORCE_GAP = 60.0
SETTINGS_TTL = 600.0
SEARCH_TTL = 600.0
SEARCH_CACHE_SIZE = 200
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"

# MediaStatus, from server/constants/media.ts.
STATUS_UNKNOWN = 1
STATUS_PENDING = 2
STATUS_PROCESSING = 3
STATUS_PARTIAL = 4
STATUS_AVAILABLE = 5
STATUS_BLOCKLISTED = 6
STATUS_DELETED = 7

# MediaRequestStatus.
REQUEST_PENDING = 1
REQUEST_APPROVED = 2
REQUEST_DECLINED = 3
REQUEST_FAILED = 4
REQUEST_COMPLETED = 5

# IssueType.
ISSUE_TYPES = {"video": 1, "audio": 2, "subtitles": 3, "other": 4}

STATUS_LABELS = {
    STATUS_PENDING: "Requested",
    STATUS_PROCESSING: "Downloading",
    STATUS_PARTIAL: "Partly on Plex",
    STATUS_AVAILABLE: "On Plex",
    STATUS_BLOCKLISTED: "Blocklisted",
}


class SeerrError(RuntimeError):
    """A request Seerr refused or could not answer.

    `status` is the HTTP status (0 when Seerr was unreachable) and `message`
    is Seerr's own explanation when it gave one -- "Movie Quota exceeded.",
    "Request for this media already exists." -- which is worth showing the
    person verbatim.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Seerr {status or 'unreachable'}: {message}")
        self.status = status
        self.message = message


def poster_url(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if text.startswith("http"):
        return text
    return TMDB_IMAGE + (text if text.startswith("/") else "/" + text)


def _year(value: Any) -> str:
    text = str(value or "")
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""


def summarize(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A search result or details payload in the one shape the bot uses.

    People and collections return None: neither can be requested.
    """
    if not isinstance(item, dict):
        return None
    kind = item.get("mediaType")
    if kind is None:
        # Details payloads carry no mediaType; tell them apart by field.
        kind = "tv" if ("firstAirDate" in item or "numberOfSeasons" in item
                        or "name" in item and "title" not in item) else "movie"
    if kind not in ("movie", "tv"):
        return None
    try:
        tmdb_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    info = item.get("mediaInfo") or {}
    try:
        status = int(info.get("status") or STATUS_UNKNOWN)
    except (TypeError, ValueError):
        status = STATUS_UNKNOWN
    title = (item.get("title") if kind == "movie" else item.get("name")) \
        or item.get("originalTitle") or item.get("originalName") or "Unknown"
    year = _year(item.get("releaseDate") if kind == "movie"
                 else item.get("firstAirDate"))
    return {
        "media_type": kind,
        "tmdb_id": tmdb_id,
        "title": str(title),
        "year": year,
        "overview": str(item.get("overview") or ""),
        "poster_url": poster_url(item.get("posterPath")),
        "status": status,
        "status_label": STATUS_LABELS.get(status, ""),
        "on_plex": status in (STATUS_AVAILABLE, STATUS_PARTIAL),
        # Seerr's own media row id. Issues are filed against THIS, not the
        # TMDB id, and it only exists once Seerr knows the title.
        "media_id": info.get("id"),
        "plex_url": plex_web_url(info.get("mediaUrl")),
    }


_BARE_PLEX = re.compile(r"^(https?://[^/?#]+)/?(#!.*)$")


def plex_web_url(url: Any) -> str:
    """Seerr's link to a title in Plex Web, made to open.

    Seerr builds it from Settings → Plex → Web App URL, and a server
    address put there with no path ("https://plex.example.com") gives
    "https://plex.example.com#!/server/…". That is the Plex server's root,
    which answers 401 Unauthorized to a browser: Plex Web lives at /web.
    Such a link is pointed at /web/index.html; any other is left alone.
    """
    text = str(url or "").strip()
    match = _BARE_PLEX.match(text)
    if match and "app.plex.tv" not in match[1]:
        return f"{match[1]}/web/index.html{match[2]}"
    return text


# How long Seerr's Application URL is trusted before it is read again.
APP_URL_TTL = 600.0


def display_title(summary: Dict[str, Any]) -> str:
    year = summary.get("year")
    return f"{summary['title']} ({year})" if year else str(summary["title"])


def link_help(discord_id: int) -> str:
    """What to tell someone Seerr doesn't know yet."""
    return ("Your Discord account isn't linked to Seerr yet, so I can't "
            "act as you.\n"
            "In Seerr, open **your profile → Settings → Notifications → "
            f"Discord** and add your Discord user ID: `{discord_id}`. "
            "Then try again — or ask an admin to `/link` you.")


def discord_ids_from(settings: Any) -> List[int]:
    """Every Discord user ID stored on a Seerr profile, in either shape."""
    if not isinstance(settings, dict):
        return []
    raw: List[Any] = []
    many = settings.get("discordIds")
    if isinstance(many, list):
        raw.extend(many)
    elif isinstance(many, str) and many.strip():
        raw.extend(part for part in many.replace(",", " ").split())
    one = settings.get("discordId")
    if one:
        raw.append(one)
    out: List[int] = []
    for value in raw:
        text = str(value).strip()
        if text.isdigit() and 15 <= len(text) <= 21 and int(text) not in out:
            out.append(int(text))
    return out


class SeerrClient:
    def __init__(self, url: str, api_key: str, public_url: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 link_overrides: Optional[Dict[Any, Any]] = None) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.public_url = (public_url or "").rstrip("/")
        self.enabled = bool(self.url and self.api_key)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._links: Dict[int, Dict[str, Any]] = {}
        self._links_at: Optional[float] = None
        self._links_forced_at = float("-inf")
        self._links_lock = asyncio.Lock()
        self._users: List[Dict[str, Any]] = []
        self._public: Optional[Dict[str, Any]] = None
        self._public_at = 0.0
        self._app_url: Optional[str] = None
        self._app_url_at = 0.0
        # Whether this Seerr has the AI routes (Seerr AI does; plain Seerr's
        # API validator answers 404 for them). None until checked.
        self.has_ai: Optional[bool] = None
        self._claim_warned: Dict[int, set] = {}
        # Discord IDs saved on more than one Seerr profile (see _load_links).
        self.contested: set = set()
        self._ai_checked_at = float("-inf")
        self._search_cache: Dict[str, tuple] = {}
        self.set_overrides(link_overrides)

    def set_overrides(self, overrides: Optional[Dict[Any, Any]]) -> None:
        """Manual Discord -> Seerr user links from config, for anyone whose
        Seerr profile has no Discord ID (or a Seerr build without the field)."""
        clean: Dict[int, int] = {}
        for key, value in (overrides or {}).items():
            try:
                clean[int(str(key).strip())] = int(value)
            except (TypeError, ValueError):
                logger.warning("Ignoring discord_links entry %r -> %r "
                               "(both must be numbers)", key, value)
        self._overrides = clean

    # ------------------------------------------------------------ transport

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self.timeout)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, *,
                    query: Optional[Dict[str, Any]] = None,
                    body: Optional[Dict[str, Any]] = None,
                    user_id: Optional[int] = None,
                    timeout: Optional[float] = None) -> Any:
        """One API call. Returns parsed JSON; raises SeerrError otherwise.

        The query string is encoded by hand, percent-encoding everything the
        way the Seerr web UI's encodeURIComponent does. Letting the HTTP
        library choose meant a space could arrive as "+", which Seerr passes
        to TMDB literally.
        """
        if not self.enabled:
            raise SeerrError(0, "Seerr is not configured")
        target = f"{self.url}/api/v1{path}"
        if query:
            target += "?" + "&".join(
                f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
                for k, v in query.items() if v is not None)
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if user_id is not None:
            headers["X-Api-User"] = str(int(user_id))
        kwargs: Dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            session = await self._get_session()
            async with session.request(method, URL(target, encoded=True),
                                       **kwargs) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = None
                if 200 <= response.status < 300 and response.status != 202:
                    return data
                message = ""
                if isinstance(data, dict):
                    message = str(data.get("message") or data.get("error")
                                  or "")
                raise SeerrError(response.status,
                                 message or f"HTTP {response.status}")
        except SeerrError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise SeerrError(0, f"could not reach Seerr "
                                f"({exc or type(exc).__name__})") from exc

    # -------------------------------------------------------------- lookups

    async def search(self, query: str, timeout: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
        """Movies and shows matching `query`, Seerr's status attached.

        Cached briefly: autocomplete asks on every keystroke, and "dune",
        "dune ", "dune p" resolving the same prefix three times is waste.
        """
        text = " ".join(str(query or "").split())
        if len(text) < 2:
            return []
        key = text.lower()
        hit = self._search_cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < SEARCH_TTL:
            return hit[1]
        data = await self._call("GET", "/search",
                                query={"query": text, "page": 1},
                                timeout=timeout)
        results = [s for s in (summarize(r) for r in
                               (data or {}).get("results") or []) if s]
        if len(self._search_cache) >= SEARCH_CACHE_SIZE:
            self._search_cache.pop(next(iter(self._search_cache)))
        self._search_cache[key] = (now, results)
        return results

    def forget_search(self) -> None:
        """Statuses in cached results go stale the moment someone requests."""
        self._search_cache.clear()

    async def details(self, media_type: str, tmdb_id: int) -> Dict[str, Any]:
        """Full movie/TV payload -- seasons, mediaInfo, requests and all."""
        if media_type not in ("movie", "tv"):
            raise SeerrError(400, f"unknown media type {media_type!r}")
        data = await self._call("GET", f"/{media_type}/{int(tmdb_id)}")
        return data if isinstance(data, dict) else {}

    async def public_settings(self) -> Dict[str, Any]:
        now = time.monotonic()
        if self._public is not None and now - self._public_at < SETTINGS_TTL:
            return self._public
        try:
            data = await self._call("GET", "/settings/public")
            self._public = data if isinstance(data, dict) else {}
        except SeerrError as exc:
            logger.warning("Seerr public settings unavailable: %s", exc)
            self._public = self._public or {}
        self._public_at = now
        return self._public

    async def partial_requests(self) -> bool:
        return bool((await self.public_settings()).get(
            "partialRequestsEnabled", True))

    async def app_url(self) -> str:
        """Where people open Seerr in a browser, for links in messages.

        SEERR_PUBLIC_URL wins; then Seerr's own Application URL setting;
        then the address the bot talks to, which may be LAN-only.
        """
        if self.public_url:
            return self.public_url
        # Read again now and then: a corrected Application URL should not
        # wait for the bot to restart.
        if self._app_url is None or \
                time.monotonic() - self._app_url_at > APP_URL_TTL:
            self._app_url_at = time.monotonic()
            try:
                main = await self._call("GET", "/settings/main")
                self._app_url = str((main or {}).get("applicationUrl")
                                    or "").rstrip("/")
            except SeerrError:
                # Keep what was read before rather than fall back to a LAN
                # address for a blip.
                self._app_url = self._app_url or ""
        return self._app_url or self.url

    async def media_link(self, media_type: str, tmdb_id: int) -> str:
        return f"{await self.app_url()}/{media_type}/{int(tmdb_id)}"

    # ------------------------------------------------------------- identity

    async def _all_users(self) -> List[Dict[str, Any]]:
        users: List[Dict[str, Any]] = []
        skip = 0
        while True:
            page = await self._call("GET", "/user",
                                    query={"take": 100, "skip": skip})
            rows = (page or {}).get("results") or []
            users.extend(r for r in rows if isinstance(r, dict))
            info = (page or {}).get("pageInfo") or {}
            skip += len(rows)
            if not rows or skip >= int(info.get("results") or 0) \
                    or skip >= 5000:
                return users

    async def _settings_for(self, user_id: int) -> Dict[str, Any]:
        try:
            data = await self._call(
                "GET", f"/user/{int(user_id)}/settings/notifications")
            return data if isinstance(data, dict) else {}
        except SeerrError as exc:
            logger.debug("Notification settings for Seerr user %s: %s",
                         user_id, exc)
            return {}

    async def _load_links(self) -> Dict[int, Dict[str, Any]]:
        users = await self._all_users()
        by_id = {int(u["id"]): u for u in users if u.get("id") is not None}
        self._users = [self._user_view(u) for u in by_id.values()]
        links: Dict[int, Dict[str, Any]] = {}
        # Admin-scoped user lists carry each profile's settings inline; if
        # this build strips them, fall back to one small call per user.
        inline = any(isinstance(u.get("settings"), dict) for u in users)
        if inline:
            pairs = [(u, (u.get("settings") or {})) for u in users]
        else:
            gate = asyncio.Semaphore(4)

            async def fetch(user):
                async with gate:
                    return user, await self._settings_for(int(user["id"]))

            pairs = await asyncio.gather(*(fetch(u) for u in by_id.values()))
        claims: Dict[int, set] = {}
        for user, settings in pairs:
            for discord_id in discord_ids_from(settings):
                claims.setdefault(discord_id, set()).add(int(user["id"]))
                links[discord_id] = self._user_view(user)
        # Anyone can type any Discord ID into their own Seerr profile. When
        # two accounts claim the same one, neither is trusted: the person is
        # treated as unlinked until an admin uses /link.
        contested = set()
        for discord_id, owners in claims.items():
            if len(owners) > 1 and discord_id not in self._overrides:
                links.pop(discord_id, None)
                contested.add(discord_id)
                if self._claim_warned.get(discord_id) != owners:
                    self._claim_warned[discord_id] = set(owners)
                    logger.warning(
                        "Discord ID %s is claimed by several Seerr users "
                        "(%s) -- treating it as unlinked; a bot admin can "
                        "/link the right one", discord_id,
                        ", ".join(str(o) for o in sorted(owners)))
        self.contested = contested
        for discord_id, seerr_id in self._overrides.items():
            user = by_id.get(seerr_id)
            if user is None:
                logger.warning("discord_links points %s at Seerr user %s, "
                               "who does not exist", discord_id, seerr_id)
                continue
            links[discord_id] = self._user_view(user)
        return links

    @staticmethod
    def _user_view(user: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": int(user["id"]),
            "name": str(user.get("displayName") or user.get("username")
                        or user.get("plexUsername") or f"User {user['id']}"),
            "permissions": int(user.get("permissions") or 0),
            # The Plex account behind this Seerr user, for looking up their
            # own watch history in Tautulli. plexId is only in admin-scoped
            # user lists -- which is what the API key gets.
            "plex_id": user.get("plexId"),
            "plex_username": user.get("plexUsername"),
        }

    async def discord_links(self, force: bool = False
                            ) -> Dict[int, Dict[str, Any]]:
        async with self._links_lock:
            now = time.monotonic()
            fresh = (self._links_at is not None
                     and now - self._links_at < LINK_TTL)
            can_force = now - self._links_forced_at >= LINK_FORCE_GAP
            if fresh and not (force and can_force):
                return self._links
            if force:
                self._links_forced_at = now
            try:
                self._links = await self._load_links()
                self._links_at = now
                logger.info("Seerr: %d Discord account(s) linked to Seerr "
                            "users%s", len(self._links),
                            "" if self._links else
                            " -- add Discord IDs in Seerr (profile -> "
                            "Settings -> Notifications) or use /link")
            except SeerrError as exc:
                logger.warning("Could not read Seerr users: %s", exc)
                # Keep serving the last good map; retry in a minute.
                self._links_at = (now - LINK_TTL + 60.0
                                  if self._links_at is not None else None)
            return self._links

    async def user_for_discord(self, discord_id: int
                               ) -> Optional[Dict[str, Any]]:
        links = await self.discord_links()
        user = links.get(int(discord_id))
        if user is None:
            # Someone may have just added their ID in Seerr.
            links = await self.discord_links(force=True)
            user = links.get(int(discord_id))
        if user is None and self._links_at is None:
            # Never managed to read the user list: "not linked" would be a
            # lie that sends people off to fix a setting that is fine.
            raise SeerrError(0, "could not read Seerr's user list")
        return user

    async def users(self, fresh: bool = False) -> List[Dict[str, Any]]:
        """Every Seerr user, as `_user_view` shapes them (for linking).

        Unlike discord_links() this raises SeerrError when the list can't
        be read, so "no such account" is never said when Seerr is down.
        `fresh` re-reads it (someone may have just been added).
        """
        if fresh or self._links_at is None:
            async with self._links_lock:
                now = time.monotonic()
                # A fresh read pages through every user (and may fetch each
                # one's settings), so it happens at most once a minute
                # however often people ask.
                if self._links_at is None \
                        or now - self._links_forced_at >= LINK_FORCE_GAP:
                    self._links_forced_at = now
                    self._links = await self._load_links()
                    self._links_at = now
        return list(self._users)

    def invalidate_links(self) -> None:
        """Re-read the Discord -> Seerr map on next use (after /link)."""
        self._links_at = None
        self._links_forced_at = float("-inf")

    async def discord_for_user(self, seerr_id: int) -> List[int]:
        links = await self.discord_links()
        return [d for d, u in links.items() if u["id"] == int(seerr_id)]

    # --------------------------------------------------------------- writes

    async def create_request(self, user_id: int, media_type: str,
                             tmdb_id: int,
                             seasons: Optional[Iterable[int]] = None
                             ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"mediaType": media_type,
                                "mediaId": int(tmdb_id), "is4k": False}
        if media_type == "tv":
            chosen = sorted({int(s) for s in (seasons or [])})
            body["seasons"] = chosen or "all"
        data = await self._call("POST", "/request", body=body,
                                user_id=user_id)
        self.forget_search()
        return data if isinstance(data, dict) else {}

    async def create_issue(self, user_id: int, media_id: int,
                           issue_type: int, message: str,
                           season: int = 0, episode: int = 0
                           ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"issueType": int(issue_type),
                                "message": str(message or "")[:2000],
                                "mediaId": int(media_id)}
        if season:
            body["problemSeason"] = int(season)
        if episode:
            body["problemEpisode"] = int(episode)
        data = await self._call("POST", "/issue", body=body, user_id=user_id)
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- reads

    async def completed_requests(self, take: int = 50) -> List[Dict[str, Any]]:
        """Most recently changed COMPLETED requests, newest first.

        Seerr marks a request completed when its media becomes available --
        and ALSO when the media is deleted (Maintainerr cleanups), so callers
        must check the media status before announcing anything.
        """
        data = await self._call("GET", "/request", query={
            "take": max(1, min(100, int(take))), "skip": 0,
            "filter": "completed", "sort": "modified",
            "sortDirection": "desc"})
        return [r for r in (data or {}).get("results") or []
                if isinstance(r, dict)]

    async def requests_for(self, user_id: int, take: int = 10
                           ) -> List[Dict[str, Any]]:
        data = await self._call("GET", "/request", query={
            "take": max(1, min(50, int(take))), "skip": 0,
            "filter": "all", "sort": "added", "requestedBy": int(user_id)})
        return [r for r in (data or {}).get("results") or []
                if isinstance(r, dict)]

    # ------------------------------------------------------------------- AI
    #
    # Seerr AI's own AI (github.com/corruptmods/seerrai, server/routes/ai.ts).
    # Plain Seerr has none of these routes; see check_ai. Called AS the person
    # (X-Api-User), so Seerr uses its own model and settings, reads their
    # watch history from Tautulli, applies their daily limit and feature
    # switches, and resolves every title it names through TMDB with their
    # view of its status. Errors carry a message Seerr wrote to be shown.

    async def check_ai(self, force: bool = False) -> Optional[bool]:
        """Whether this Seerr has the AI routes, re-checked every
        AI_CHECK_SECONDS. Any answer but 404 means the route exists (a 403
        only means its AI is switched off right now). None while unknown,
        e.g. Seerr is unreachable and has never answered."""
        if not self.enabled:
            self.has_ai = False
            return False
        if not force and time.monotonic() - self._ai_checked_at \
                < AI_CHECK_SECONDS:
            return self.has_ai
        self._ai_checked_at = time.monotonic()
        try:
            await self._call("GET", "/ai/status")
            found = True
        except SeerrError as exc:
            # Unreachable or a server error (a proxy's 502 while Seerr
            # restarts) says nothing either way.
            found = (None if exc.status == 0 or exc.status >= 500
                     else exc.status != 404)
        if found is not None and found != self.has_ai:
            logger.info("Seerr %s the AI features (Seerr AI)",
                        "has" if found else "doesn't have")
        if found is not None:
            self.has_ai = found
        return self.has_ai

    def no_ai(self) -> None:
        """An AI route just answered 404: this Seerr has no AI."""
        self.has_ai = False
        self._ai_checked_at = time.monotonic()

    async def ai_chat(self, user_id: int, message: str,
                      turns: Iterable[Dict[str, Any]] = ()
                      ) -> Dict[str, Any]:
        """One chat turn. `turns` are earlier ones, oldest first, as
        {ask, reply, offered: ["Title (year)", ...]}; trimmed here to the
        limits the route validates, so a long Discord message is cut rather
        than refused."""
        clean = []
        for turn in list(turns)[-AI_TURNS:]:
            clean.append({
                "ask": str(turn.get("ask") or "")[:AI_ASK],
                "reply": str(turn.get("reply") or "")[:AI_REPLY],
                "offered": [str(o)[:AI_OFFERED_CHARS]
                            for o in (turn.get("offered") or [])
                            ][:AI_OFFERED]})
        data = await self._call(
            "POST", "/ai/chat", user_id=user_id, timeout=AI_TIMEOUT,
            body={"message": " ".join(str(message).split())[:AI_ASK],
                  "turns": clean})
        return data if isinstance(data, dict) else {"text": data}

    async def ai_recap_position(self, user_id: int, tv_id: int
                                ) -> Optional[Dict[str, int]]:
        """The next episode this person has not seen, from their history."""
        data = await self._call("GET", f"/ai/recap/{int(tv_id)}/position",
                                user_id=user_id, timeout=AI_TIMEOUT)
        position = (data or {}).get("position") if isinstance(data, dict) \
            else None
        if isinstance(position, dict) and position.get("season") \
                and position.get("episode"):
            return {"season": int(position["season"]),
                    "episode": int(position["episode"])}
        return None

    async def ai_recap(self, user_id: int, tv_id: int, season: int,
                       episode: int) -> str:
        """A spoiler-free recap of everything before season/episode."""
        data = await self._call("GET", f"/ai/recap/{int(tv_id)}",
                                user_id=user_id, timeout=AI_TIMEOUT,
                                query={"season": int(season),
                                       "episode": int(episode)})
        return str((data or {}).get("text") or "").strip() \
            if isinstance(data, dict) else ""


# How often to re-check whether Seerr has the AI routes.
AI_CHECK_SECONDS = 600.0
# Limits /ai/chat validates (routes/ai.ts chatBody).
AI_ASK = 500
AI_REPLY = 400
AI_TURNS = 10
AI_OFFERED = 10
AI_OFFERED_CHARS = 200
# A model answering, plus TMDB lookups for every title it names.
AI_TIMEOUT = 120.0
_TEXT_KEYS = ("text", "reply", "answer", "message", "content")
_LIST_KEYS = ("results", "titles", "items", "picks", "found")


def ai_answer(data: Any) -> tuple:
    """(text, titles) from a Seerr AI answer.

    Titles are Seerr MovieResult/TvResult objects, as its search returns
    them, which `summarize` reads. Tolerant of the answer's exact field
    names: a pick-style {reason, result} entry works too, and the reason is
    kept as the title's note.
    """
    if isinstance(data, str):
        return data.strip(), []
    if not isinstance(data, dict):
        return "", []
    text = next((data[k].strip() for k in _TEXT_KEYS
                 if isinstance(data.get(k), str) and data[k].strip()), "")
    titles: List[Dict[str, Any]] = []
    for key in _LIST_KEYS:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            inner = row.get("result") if isinstance(row.get("result"),
                                                    dict) else row
            if inner.get("mediaType") in ("movie", "tv") \
                    and inner.get("id") is not None:
                if inner is not row and row.get("reason"):
                    inner = {**inner, "_reason": str(row["reason"])}
                titles.append(inner)
        if titles:
            break
    return text, titles
