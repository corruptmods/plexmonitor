"""Assistant cog -- a private conversation with an AI about the Plex server.

Every conversation is between one person and the bot; nobody else sees it.

  Ask AI        the request panel's button (cogs/requestpanel.py): a popup
                for a question, answered ephemerally (only the asker sees
                it). Titles typed into the panel's Request popup that aren't
                titles come here too. A Reply button on the answer opens a
                box to continue the same conversation.
  @mention      in the assistant channel: the question is taken out of the
                channel (deleted, when the bot has Manage Messages) and the
                conversation continues in the person's DMs.
  DMs           just talk. Every DM is the next turn; "New chat" starts over.

  /recap        Seerr AI's "Catch Me Up": a spoiler-free recap of a series
                up to the next episode the person hasn't watched.

Who answers (config `assistant.engine`):

  seerr   Seerr AI's own AI (/api/v1/ai/chat), asked AS the person: the
          same model, prompt, watch-history context, title lookups and daily
          limit as the AI button in Seerr AI. Needs the person linked to
          Seerr. Plain Seerr has no AI; see SeerrClient.check_ai.
  own     The bot's own model (AI_PROVIDER/AI_API_KEY) with tools over the
          bot's data: Seerr search and status, the person's requests and
          watch history, what is playing, recent imports, library sizes.
  auto    Seerr AI when it is on for that person; the bot's own model for
          anyone not linked, when Seerr's AI is off, or with plain Seerr.
          The default.

Either way it can SUGGEST but never REQUEST. Titles come back as cards with
a Request button the person presses -- filed as them, through Seerr, with
their own quotas. A model mis-hearing "not Dune" as "Dune" costs nothing.

Message Content intent is NOT needed: Discord delivers the text of DMs and
of messages that mention the bot. Conversation memory is a per-person
session held in memory that lapses after SESSION_IDLE of silence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands, ui
from discord.ext import commands

from util import aipager, linkui, requestui
from util.auth import home_guild, home_member
from util.jsonfile import data_dir, read_json, write_json
from util import ai
from util import seerr as seerrapi
from util.seerr import SeerrError

logger = logging.getLogger("plexmonitor.assistant")

DEFAULTS = {
    "enabled": True,
    "engine": "auto",
    "channel_id": 0,
    "daily_limit": 25,
    "history": 8,
    "delete_mentions": True,
    "dms": True,
}
MAX_ANSWER = 2800
# A conversation nobody has touched for this long starts over.
SESSION_IDLE = 30 * 60.0
# How long the "check your DMs" / "can't DM you" notice stays in the channel.
NOTICE_SECONDS = 20.0
SYSTEM = """You are the assistant for a private Plex server, in a private \
one-to-one Discord conversation. People ask what to watch, whether something is on Plex, \
what is playing, what was added lately and how their requests are going.

Rules:
- Facts come from tools. Never guess whether something is on Plex, \
requested or downloading -- look it up.
- To offer a title for requesting, call show_request_cards with the exact \
media_type and tmdb_id from search results. You cannot file requests \
yourself; the person presses the Request button on the card.
- When they mention something they watched ("the last show I watched", \
"more like what I've been watching"), call my_watch_history instead of \
asking them.
- Recommend from your own knowledge freely, but search each pick so the \
card shows real availability. Prefer things NOT already on Plex only if \
they asked for something new.
- Keep replies short and conversational: a few sentences, Discord \
markdown, no headings, no tables. Don't list details the cards already \
show.
- The person talking is {name}. Today is {today}."""
ENGINES = ("auto", "seerr", "own")
REQUEST_STATUS = {seerrapi.REQUEST_PENDING: "pending approval",
                  seerrapi.REQUEST_APPROVED: "approved",
                  seerrapi.REQUEST_DECLINED: "declined",
                  seerrapi.REQUEST_FAILED: "failed",
                  seerrapi.REQUEST_COMPLETED: "completed"}
# How long a server-membership check is trusted.
MEMBER_TTL = 600.0
NO_SEERR_AI = ("This Seerr has no AI features (they come with Seerr AI), and "
               "the bot has no AI model of its own set up.")


@dataclass
class Answer:
    text: str
    picks: List[Dict[str, Any]] = field(default_factory=list)
    footer: str = ""
    # False for refusals ("AI is off", "not linked"): not a conversation
    # turn, so not remembered.
    record: bool = True
    # A "Link my Seerr account" button under the answer.
    offer_link: bool = False
    # Shown instead of an answer card (the link steps).
    view: Optional[ui.LayoutView] = None
    # The first suggestion's full title card (items, accent), from Seerr.
    page: Optional[Tuple[List[ui.Item], int]] = None


# Asking to link their account, in their own words.
LINK_INTENT = re.compile(
    r"\b(?:link|connect)\s+(?:me\b|my\s+(?:seerr\s+|discord\s+|plex\s+)?"
    r"(?:account|profile|seerr|discord)\b|(?:to\s+)?seerr\b)"
    r"|\bhow\s+(?:do|can)\s+i\s+(?:link|connect)\b"
    r"|\b(?:not|isn'?t|am\s*n'?t|'m\s+not)\s+linked\b",
    re.I)
# Discord refuses a Components V2 message whose text passes 4000
# characters; the answer gets whatever the cards leave.
MESSAGE_TEXT = 3900


def _text_of(item: Any) -> int:
    size = len(getattr(item, "content", "") or "")
    for child in getattr(item, "children", None) or []:
        size += _text_of(child)
    return size


def compact_page(pick: Dict[str, Any]) -> Tuple[List[ui.Item], int]:
    """A title card from what the answer already knows, for when Seerr
    can't be asked for the full one."""
    kind = pick["media_type"]
    lines = [f"### {'🎬' if kind == 'movie' else '📺'} "
             f"{discord.utils.escape_markdown(seerrapi.display_title(pick))}",
             "-# " + ("Movie" if kind == "movie" else "TV show")
             + (f" · {pick['status_label']}" if pick.get("status_label")
                else "")]
    if pick.get("overview"):
        lines.append(discord.utils.escape_markdown(
            requestui._clip(pick["overview"], requestui.OVERVIEW_CHARS)))
    text = ui.TextDisplay("\n".join(lines))
    items: List[ui.Item] = [ui.Section(text, accessory=ui.Thumbnail(
        pick["poster_url"])) if pick.get("poster_url") else text]
    buttons: List[ui.Item] = []
    if pick.get("plex_url"):
        buttons.append(ui.Button(label="Watch on Plex", emoji="▶️",
                                 style=discord.ButtonStyle.link,
                                 url=pick["plex_url"]))
    elif pick.get("status") not in (
            seerrapi.STATUS_PENDING, seerrapi.STATUS_PROCESSING,
            seerrapi.STATUS_AVAILABLE, seerrapi.STATUS_BLOCKLISTED):
        buttons.append(requestui.RequestButton(kind, pick["tmdb_id"]))
    if buttons:
        items.append(ui.ActionRow(*buttons))
    return items, requestui.ACCENT["available" if pick.get("on_plex")
                                   else "requestable"]


def build_answer(asked: str, text: str, also: str, footer: str,
                 page: Optional[Tuple[List[ui.Item], int]],
                 keys: List[str], index: int,
                 bottom: List[ui.Item]) -> ui.LayoutView:
    """An AI answer: the reply, then one suggested title at a time with
    ◀ n / N ▶, then Reply (or New chat) and any link button."""
    view = requestui.Card()
    card: List[ui.Item] = []
    accent = requestui.ACCENT["requestable"]
    if page is not None:
        card, accent = list(page[0]), page[1]
        nav = aipager.nav_row(keys, index)
        if nav is not None:
            card.append(nav)
    head: List[ui.Item] = []
    if asked:
        head.append(ui.TextDisplay(asked, id=aipager.ASKED_ID))
    tail: List[ui.Item] = []
    if also:
        tail.append(ui.TextDisplay(also, id=aipager.ALSO_ID))
    if footer:
        tail.append(ui.TextDisplay(footer, id=aipager.FOOTER_ID))
    room = MESSAGE_TEXT - sum(_text_of(i) for i in head + tail + card)
    limit = max(200, min(MAX_ANSWER, room))
    body = text if len(text) <= limit else text[: limit - 1] + "…"
    view.add_item(ui.Container(*head, ui.TextDisplay(body,
                                                     id=aipager.TEXT_ID),
                               *tail, accent_colour=0x5865F2))
    if card:
        view.add_item(ui.Container(*card, accent_colour=accent))
    if bottom:
        view.add_item(ui.ActionRow(*bottom))
    # Every button here is a dynamic item (dispatched by custom_id), so the
    # view itself needn't be kept: a stopped view isn't stored per message.
    view.stop()
    return view


class OverLimit(Exception):
    """The bot's own daily allowance for this person is spent."""


def history_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One Tautulli history row, trimmed to what the model needs."""
    kind = str(row.get("media_type") or "")
    entry: Dict[str, Any] = {"type": kind}
    if kind == "episode":
        entry["show"] = row.get("grandparent_title")
        try:
            entry["episode"] = (f"S{int(row.get('parent_media_index') or 0):02d}"
                                f"E{int(row.get('media_index') or 0):02d}")
        except (TypeError, ValueError):
            pass
        entry["episode_title"] = row.get("title")
    else:
        entry["title"] = row.get("full_title") or row.get("title")
        if row.get("year"):
            entry["year"] = row.get("year")
    try:
        stamp = float(row.get("date") or 0)
        if stamp:
            entry["watched"] = datetime.fromtimestamp(
                stamp, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    entry["finished"] = row.get("watched_status") == 1
    return entry


def _strip_mentions(text: str, bot_id: int) -> str:
    text = re.sub(rf"<@!?{bot_id}>", "", text or "")
    return " ".join(text.split())


class Assistant(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_dir = data_dir()
        self._config_path = os.path.join(self.data_dir, "config.json")
        self._config_mtime = -1.0
        self.config = dict(DEFAULTS)
        self._reload_config()
        try:
            max_tokens = int(os.getenv("AI_MAX_TOKENS", "1024"))
        except ValueError:
            max_tokens = 1024
        self.provider = ai.make_provider(
            os.getenv("AI_PROVIDER", "anthropic"),
            os.getenv("AI_API_KEY", ""), os.getenv("AI_MODEL", ""),
            os.getenv("AI_BASE_URL", ""), max_tokens)
        if self.provider is None:
            logger.info("AI_PROVIDER/AI_API_KEY not set -- the bot's own "
                        "model is off; Seerr's AI answers when it is set up")
        else:
            logger.info("Assistant using %s model %s", self.provider.kind,
                        self.provider.model)
        self.usage_file = os.path.join(self.data_dir, "ai_usage.json")
        usage = read_json(self.usage_file, {})
        self._usage: Dict[str, Any] = usage if isinstance(usage, dict) else {}
        # user id -> {"turns": [(role, text)], "at": monotonic}
        self._sessions: Dict[int, Dict[str, Any]] = {}
        self._busy: set = set()
        self._members: Dict[int, Tuple[bool, float]] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*AI_ITEMS)
        self._probe = asyncio.ensure_future(self._probe_seerr_ai())

    def _warn_other_servers(self) -> None:
        """The assistant answers members of the dashboard's server only, so
        an assistant or request panel channel in another server can't work
        for anyone who isn't also in the dashboard's. Say so once."""
        home = home_guild(self.bot)
        if home is None:
            return
        requests = self.bot.get_cog("Requests")
        places = [("AI_CHANNEL_ID", self._channel_id())]
        if requests is not None:
            places.append(("REQUEST_PANEL_CHANNEL_ID", requests.channel_setting(
                "panel_channel_id", "REQUEST_PANEL_CHANNEL_ID")))
        for name, channel_id in places:
            channel = self.bot.get_channel(channel_id) if channel_id else None
            guild = getattr(channel, "guild", None)
            if guild is not None and guild.id != home.id:
                logger.warning(
                    "%s is in %s, but the dashboard (CHANNEL_ID) is in %s. "
                    "The assistant only answers members of %s.", name,
                    guild, home, home)

    async def _probe_seerr_ai(self) -> None:
        """Find out early whether Seerr has AI, so the request panel only
        offers Ask AI when someone can answer."""
        try:
            await self.bot.wait_until_ready()
            seerr = self._seerr()
            if seerr is not None and self._engine() != "own":
                await seerr.check_ai(force=True)
            self._warn_other_servers()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Seerr AI check failed", exc_info=True)

    async def cog_unload(self) -> None:
        probe = getattr(self, "_probe", None)
        if probe is not None:
            probe.cancel()
        try:
            self.bot.remove_dynamic_items(*AI_ITEMS)
        except Exception:
            logger.debug("remove_dynamic_items failed", exc_info=True)
        if self.provider is not None:
            await self.provider.close()

    # ---------------------------------------------------------------- config

    def _reload_config(self) -> None:
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        raw = read_json(self._config_path, {})
        section = raw.get("assistant") if isinstance(raw, dict) else None
        merged = dict(DEFAULTS)
        if isinstance(section, dict):
            merged.update(section)
        env_channel = os.getenv("AI_CHANNEL_ID", "")
        if env_channel.strip().isdigit():
            merged["channel_id"] = int(env_channel)
        self.config = merged

    def _channel_id(self) -> int:
        try:
            return int(self.config.get("channel_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _limit(self) -> int:
        try:
            return max(0, int(self.config.get("daily_limit", 25)))
        except (TypeError, ValueError):
            return 25

    def _spend(self, user_id: int) -> bool:
        """Count one question against today's allowance. False = over."""
        limit = self._limit()
        today = date.today().isoformat()
        if self._usage.get("date") != today:
            self._usage = {"date": today, "users": {}}
        users = self._usage.setdefault("users", {})
        used = int(users.get(str(user_id), 0))
        if limit and used >= limit:
            return False
        users[str(user_id)] = used + 1
        write_json(self.usage_file, self._usage)
        return True

    def _engine(self) -> str:
        engine = str(self.config.get("engine") or "auto").strip().lower()
        return engine if engine in ENGINES else "auto"

    def _history(self, user_id: int, fresh: bool = False
                 ) -> List[Dict[str, Any]]:
        """This person's conversation so far (oldest first) as
        {ask, reply, offered} turns -- the shape Seerr's chat takes -- or []
        when it lapsed or `fresh` asks for a new one."""
        session = self._sessions.get(user_id)
        if fresh or session is None \
                or time.monotonic() - session["at"] > SESSION_IDLE:
            self._sessions.pop(user_id, None)
            return []
        return [dict(t) for t in session["turns"]]

    def _record(self, user_id: int, question: str, answer: str,
                offered: List[str] = ()) -> None:
        turns = self._history(user_id)
        turns.append({"ask": question, "reply": answer,
                      "offered": list(offered)})
        try:
            depth = max(1, int(self.config.get("history", 8) or 8))
        except (TypeError, ValueError):
            depth = 8
        self._sessions[user_id] = {"turns": turns[-depth:],
                                   "at": time.monotonic()}
        # Forget lapsed sessions so the map cannot grow without bound.
        cutoff = time.monotonic() - SESSION_IDLE
        for uid in [u for u, v in self._sessions.items() if v["at"] < cutoff]:
            self._sessions.pop(uid, None)

    def forget(self, user_id: int) -> None:
        self._sessions.pop(user_id, None)

    # ------------------------------------------------------------------ tools

    def _seerr(self):
        cog = self.bot.get_cog("Requests")
        client = getattr(cog, "seerr", None)
        return client if client is not None and client.enabled else None

    def _dashboard(self):
        """The Dashboard cog, which owns the Tautulli/Plex/*arr clients."""
        return self.bot.get_cog("Dashboard")

    def _tools(self, asker: discord.abc.User, picks: List[Dict[str, Any]]
               ) -> List[ai.Tool]:
        tools: List[ai.Tool] = []
        seerr = self._seerr()
        seen: Dict[str, Dict[str, Any]] = {}

        if seerr is not None:
            async def search_media(query: str, media_type: str = "any"):
                results = await seerr.search(query)
                if media_type in ("movie", "tv"):
                    results = [r for r in results
                               if r["media_type"] == media_type]
                for r in results:
                    seen[f"{r['media_type']}:{r['tmdb_id']}"] = r
                return [{"media_type": r["media_type"],
                         "tmdb_id": r["tmdb_id"], "title": r["title"],
                         "year": r["year"],
                         "status": r["status_label"] or "not requested",
                         "overview": r["overview"][:200]}
                        for r in results[:8]]

            async def media_status(media_type: str, tmdb_id: int):
                details = await seerr.details(media_type, int(tmdb_id))
                summary = seerrapi.summarize({**details,
                                              "mediaType": media_type})
                if summary:
                    seen[f"{media_type}:{int(tmdb_id)}"] = summary
                out = {"title": seerrapi.display_title(summary)
                       if summary else "?",
                       "status": (summary or {}).get("status_label")
                       or "not requested",
                       "requested_by": requestui.requesters(details)}
                if media_type == "tv":
                    state = requestui.season_state(details)
                    out.update(seasons_on_plex=state["have"],
                               seasons_requested=state["asked"],
                               seasons_available_to_request=state["open"])
                return out

            async def show_request_cards(items: List[Dict[str, Any]]):
                added = []
                for item in (items or [])[:4]:
                    kind = str(item.get("media_type") or "")
                    try:
                        tmdb_id = int(item.get("tmdb_id"))
                    except (TypeError, ValueError):
                        continue
                    key = f"{kind}:{tmdb_id}"
                    summary = seen.get(key)
                    if summary is None and kind in ("movie", "tv"):
                        try:
                            details = await seerr.details(kind, tmdb_id)
                            summary = seerrapi.summarize(
                                {**details, "mediaType": kind})
                        except SeerrError:
                            summary = None
                    if summary and all(
                            (p["media_type"], p["tmdb_id"]) != (kind, tmdb_id)
                            for p in picks):
                        picks.append(summary)
                        added.append(seerrapi.display_title(summary))
                return {"cards_shown": added} if added else \
                    "error: none of those ids were found -- search first"

            async def my_requests():
                user = await seerr.user_for_discord(asker.id)  # may raise
                if user is None:
                    return ("This person's Discord account is not linked to "
                            "Seerr, so their requests can't be looked up. "
                            "They can add their Discord ID in Seerr under "
                            "Settings -> Notifications -> Discord.")
                rows = await seerr.requests_for(user["id"], 10)
                out = []
                for row in rows:
                    media = row.get("media") or {}
                    out.append({
                        "tmdb_id": media.get("tmdbId"),
                        "media_type": media.get("mediaType"),
                        "request_status": REQUEST_STATUS.get(
                            row.get("status"), "?"),
                        "media_status": seerrapi.STATUS_LABELS.get(
                            media.get("status"), "not available"),
                        "requested_at": row.get("createdAt")})
                return out

            tools += [
                ai.Tool("search_media",
                        "Search movies and TV shows. Returns tmdb ids and "
                        "whether each is on Plex, requested or neither.",
                        {"type": "object", "properties": {
                            "query": {"type": "string"},
                            "media_type": {"type": "string",
                                           "enum": ["any", "movie", "tv"]}},
                         "required": ["query"]}, search_media),
                ai.Tool("media_status",
                        "Detailed availability for one title, including "
                        "which TV seasons are on Plex or requested.",
                        {"type": "object", "properties": {
                            "media_type": {"type": "string",
                                           "enum": ["movie", "tv"]},
                            "tmdb_id": {"type": "integer"}},
                         "required": ["media_type", "tmdb_id"]},
                        media_status),
                ai.Tool("show_request_cards",
                        "Attach up to 4 title cards under your reply. Each "
                        "shows availability; requestable ones get a Request "
                        "button the person can press.",
                        {"type": "object", "properties": {"items": {
                            "type": "array", "maxItems": 4, "items": {
                                "type": "object", "properties": {
                                    "media_type": {"type": "string",
                                                   "enum": ["movie", "tv"]},
                                    "tmdb_id": {"type": "integer"}},
                                "required": ["media_type", "tmdb_id"]}}},
                         "required": ["items"]}, show_request_cards),
                ai.Tool("my_requests",
                        "The asking person's own recent Seerr requests and "
                        "their status.",
                        {"type": "object", "properties": {}}, my_requests),
            ]

        dashboard = self._dashboard()
        if dashboard is not None:
            async def now_playing():
                activity = await dashboard._fetch_activity()
                if activity is None:
                    return "Plex is offline or unreachable right now."
                mapping = getattr(dashboard, "user_mapping", {}) or {}
                out = []
                for s in activity.get("sessions") or []:
                    out.append({
                        "title": s.get("series") or s.get("title"),
                        "episode": s.get("title") if s.get("series") else None,
                        "user": mapping.get(s.get("user", ""), s.get("user")),
                        "progress_percent": s.get("progress"),
                        "paused": bool(s.get("paused")),
                        "transcoding": s.get("decision") == "transcode"})
                return out or "Nothing is playing."

            async def recently_added():
                clients = [c for c in getattr(dashboard, "_arr", []) or []
                           if c.enabled]
                results = await asyncio.gather(
                    *[c.imported(10) for c in clients],
                    return_exceptions=True)
                items = []
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    items.extend(result)
                items.sort(key=lambda d: float(d.get("completed") or 0),
                           reverse=True)
                return [{"title": d.get("name"),
                         "hours_ago": round((time.time()
                                             - float(d.get("completed")
                                                     or 0)) / 3600, 1)}
                        for d in items[:12]] or \
                    "No recent imports reported by Radarr/Sonarr."

            async def library_stats():
                libraries = await dashboard._fetch_libraries()
                return [{"library": lib.get("name"), "items":
                         lib.get("count"), "episodes_or_tracks":
                         lib.get("child_count") or None}
                        for lib in libraries] or "Library counts unavailable."

            tools += [
                ai.Tool("now_playing", "What is streaming on Plex right now.",
                        {"type": "object", "properties": {}}, now_playing),
                ai.Tool("recently_added",
                        "Titles imported to Plex recently (Radarr/Sonarr).",
                        {"type": "object", "properties": {}},
                        recently_added),
                ai.Tool("library_stats", "How many items each Plex library "
                        "holds.", {"type": "object", "properties": {}},
                        library_stats),
            ]

        tautulli = getattr(dashboard, "tautulli", None)
        if seerr is not None and tautulli is not None:
            async def my_watch_history(limit: int = 15):
                user = await seerr.user_for_discord(asker.id)  # may raise
                if user is None:
                    return ("This person's Discord account is not linked to "
                            "Seerr, so their Plex account is unknown. They "
                            "can add their Discord ID in Seerr under "
                            "Settings -> Notifications -> Discord.")
                if user.get("plex_id"):
                    who = {"user_id": user["plex_id"]}
                elif user.get("plex_username"):
                    who = {"user": user["plex_username"]}
                else:
                    return "Their Seerr account has no Plex account attached."
                data = await tautulli.call(
                    "get_history", length=max(1, min(50, int(limit or 15))),
                    order_column="date", order_dir="desc", **who)
                rows = (data or {}).get("data") if isinstance(data, dict) \
                    else data
                return [history_row(r) for r in rows or []
                        if isinstance(r, dict)] or \
                    "No watch history for this person yet."

            tools.append(ai.Tool(
                "my_watch_history",
                "What the asking person has watched on Plex lately, newest "
                "first. Use it whenever they refer to something they "
                "watched (\"like the last show I watched\").",
                {"type": "object", "properties": {
                    "limit": {"type": "integer", "minimum": 1,
                              "maximum": 50}}},
                my_watch_history))
        return tools

    # ----------------------------------------------------------------- answer

    async def answer(self, asker: discord.abc.User, question: str,
                     history: List[Dict[str, Any]]
                     ) -> Tuple[str, List[Dict[str, Any]]]:
        """The bot's own model, with tools. Returns (text, picks)."""
        picks: List[Dict[str, Any]] = []
        name = getattr(asker, "display_name", None) or asker.name
        convo = self.provider.conversation(SYSTEM.format(
            name=name, today=date.today().strftime("%A %d %B %Y")))
        for turn in history:
            convo.add_user(turn["ask"])
            reply = turn.get("reply") or ""
            if turn.get("offered"):
                reply += "\n(Cards shown: " + ", ".join(turn["offered"]) + ")"
            convo.add_assistant_text(reply or "…")
        convo.add_user(question)
        text = await ai.run(self.provider, convo, self._tools(asker, picks))
        return text or "I don't have an answer for that.", picks

    async def seerr_answer(self, seerr, seerr_user: Dict[str, Any],
                           question: str, history: List[Dict[str, Any]]
                           ) -> Answer:
        """Seerr's own AI chat, asked as this person."""
        data = await seerr.ai_chat(seerr_user["id"], question, history)
        text, titles = seerrapi.ai_answer(data)
        picks: List[Dict[str, Any]] = []
        for title in titles:
            summary = seerrapi.summarize(title)
            if summary is None or any(
                    (p["media_type"], p["tmdb_id"]) ==
                    (summary["media_type"], summary["tmdb_id"])
                    for p in picks):
                continue
            if title.get("_reason"):
                summary["overview"] = title["_reason"]
            picks.append(summary)
        if not text and not picks:
            logger.warning("Seerr AI answered with nothing readable "
                           "(fields: %s)", sorted(data) if isinstance(
                               data, dict) else type(data).__name__)
            text = "Seerr's AI didn't come back with an answer — try " \
                   "asking another way."
        elif not text:
            text = "Here's what I found:"
        return Answer(text, picks, footer="-# Seerr AI · can be wrong")

    def _own_ready(self) -> bool:
        return self.provider is not None and self._engine() != "seerr"

    def _seerr_ready(self) -> bool:
        seerr = self._seerr()
        return (self._engine() != "own" and seerr is not None
                and seerr.has_ai is not False)

    async def _own(self, user: discord.abc.User, question: str,
                   history: List[Dict[str, Any]], note: str = "") -> Answer:
        if not self._spend(user.id):
            raise OverLimit()
        text, picks = await self.answer(user, question, history)
        footer = f"-# AI · {self.provider.model} · can be wrong"
        if note:
            footer = note + "\n" + footer
        return Answer(text, picks, footer=footer)

    async def respond(self, user: discord.abc.User, question: str,
                      history: List[Dict[str, Any]]) -> Answer:
        """Pick who answers (see the module docstring) and ask them."""
        if LINK_INTENT.search(question) and self._seerr() is not None:
            return await self._link_request(user)
        if self._engine() != "own" and self._seerr() is not None:
            await self._seerr().check_ai()
        seerr = self._seerr() if self._seerr_ready() else None
        own = self._own_ready()
        if seerr is None:
            if own:
                return await self._own(user, question, history)
            return Answer("The assistant isn't set up on this bot.",
                          record=False)
        try:
            seerr_user = await seerr.user_for_discord(user.id)
        except SeerrError as exc:
            if own:
                logger.info("Seerr unreachable (%s) -- the bot's own model "
                            "answers", exc)
                return await self._own(user, question, history)
            return Answer("Seerr isn't reachable right now — try again in a "
                          "minute.", record=False)
        if seerr_user is None:
            if own:
                answer = await self._own(
                    user, question, history,
                    note="-# Not linked to Seerr, so this isn't based on "
                         "your own watch history.")
                answer.offer_link = True
                return answer
            return await self._link_answer(
                user, "Seerr's AI answers as you, so it needs to know who "
                      "you are in Seerr first.")
        try:
            return await self.seerr_answer(seerr, seerr_user, question,
                                           history)
        except SeerrError as exc:
            # 404: this Seerr has no AI (it isn't Seerr AI). 403: its AI is
            # off (for everyone, for this person, or no model set up).
            # Unreachable: Seerr is down. In each case the bot's own model
            # can stand in. Anything else -- above all the daily limit -- is
            # Seerr's decision and is shown as it wrote it, never worked
            # around.
            if exc.status == 404:
                seerr.no_ai()
            if own and exc.status in (0, 403, 404):
                logger.info("Seerr AI unavailable for %s (%s) -- the bot's "
                            "own model answers", seerr_user["name"], exc)
                return await self._own(user, question, history)
            if exc.status == 0:
                return Answer("Seerr isn't reachable right now — try again "
                              "in a minute.", record=False)
            if exc.status == 404:
                return Answer(NO_SEERR_AI, record=False)
            logger.info("Seerr AI refused %s: %s", seerr_user["name"], exc)
            return Answer(exc.message or "Seerr's AI couldn't answer that.",
                          record=False)

    async def _link_answer(self, user: discord.abc.User,
                           note: str) -> Answer:
        """The link steps in place of an answer."""
        requests = self.bot.get_cog("Requests")
        if requests is None:
            return Answer(seerrapi.link_help(user.id), record=False)
        return Answer("", record=False,
                      view=await requests.link_prompt(user.id, note))

    async def _link_request(self, user: discord.abc.User) -> Answer:
        """They asked the AI to link their account: do that instead."""
        seerr = self._seerr()
        try:
            linked = await seerr.user_for_discord(user.id)
        except SeerrError:
            return Answer("Seerr isn't reachable right now — try again in a "
                          "minute.", record=False)
        if linked is not None:
            return Answer("You're already linked to Seerr as **"
                          + discord.utils.escape_markdown(linked["name"])
                          + "**.", record=False)
        return await self._link_answer(user, "")

    def _view(self, answer: Answer, asked: str = "",
              button: Optional[ui.Item] = None) -> ui.LayoutView:
        if answer.view is not None:
            return answer.view
        keys = aipager.encode(answer.picks)
        rest = answer.picks[len(keys):]
        also = ""
        if rest:
            names = ", ".join(seerrapi.display_title(p) for p in rest[:12])
            more = f" and {len(rest) - 12} more" if len(rest) > 12 else ""
            also = requestui._clip(f"-# Also: {names}{more}", 600)
        page = answer.page
        if page is None and answer.picks:
            page = compact_page(answer.picks[0])
        bottom = [b for b in (button, linkui.LinkStartButton()
                              if answer.offer_link else None) if b]
        return build_answer(asked, answer.text, also, answer.footer, page,
                            keys, 0, bottom)

    async def _page(self, kind: str, tmdb_id: int,
                    fallback: Optional[Dict[str, Any]] = None):
        """One suggestion's title card, as the request panel shows it."""
        requests = self.bot.get_cog("Requests")
        if requests is not None and requests.seerr.enabled:
            try:
                return await requests.card_parts(kind, tmdb_id)
            except SeerrError as exc:
                logger.info("Title card for %s:%s unavailable: %s", kind,
                            tmdb_id, exc)
        return compact_page(fallback) if fallback else None

    async def turn_page(self, interaction: discord.Interaction,
                        keys: List[str], page: int) -> None:
        """◀/▶ on an answer (after defer): the same answer, another title."""
        kind, tmdb_id = aipager.decode(keys[page])
        parts = await self._page(kind, tmdb_id)
        if parts is None:
            await interaction.followup.send(
                "Seerr isn't reachable right now — try again in a minute.",
                ephemeral=True)
            return
        texts = aipager.texts_by_id(interaction.message)
        present = set(aipager.custom_ids(interaction.message))
        bottom = [item() for cid, item in BOTTOM_BUTTONS if cid in present]
        view = build_answer(texts.get(aipager.ASKED_ID, ""),
                            texts.get(aipager.TEXT_ID, ""),
                            texts.get(aipager.ALSO_ID, ""),
                            texts.get(aipager.FOOTER_ID, ""),
                            parts, keys, page, bottom)
        try:
            await interaction.edit_original_response(view=view)
        except discord.HTTPException as exc:
            logger.warning("Could not turn the suggestion page: %s", exc)
            await interaction.followup.send(
                "Couldn't show that one — try again.", ephemeral=True)

    def _allowed_here(self, channel_id: int) -> bool:
        wanted = self._channel_id()
        return not wanted or channel_id == wanted

    def _ready(self) -> bool:
        self._reload_config()
        return bool(self.config.get("enabled", True)) and (
            self._own_ready() or self._seerr_ready())

    def _limit_text(self) -> str:
        return (f"You've hit today's limit of {self._limit()} questions — "
                "it resets at midnight.")

    @staticmethod
    def _failure(exc: ai.AIError) -> str:
        if exc.status == 429:
            return "The AI service is busy right now — try again shortly."
        if exc.status in (401, 403):
            return "The AI service rejected the bot's API key."
        return "I couldn't reach the AI service just now."

    async def _member(self, user: discord.abc.User,
                      guild: Optional[discord.Guild] = None) -> bool:
        """Only members of the server the dashboard is in may use the
        assistant: a bot can be DMed, or added to other servers, by anyone,
        and answers spend the AI key and can say who is watching what.

        `guild` is where the question came from; asked inside the home
        server, the asker is a member without any lookup. When Discord
        can't say (the server isn't loaded yet right after a reconnect),
        the question is allowed rather than refusing everyone."""
        home = home_guild(self.bot)
        if home is None:
            return True
        if guild is not None and guild.id == home.id:
            return True
        now = time.monotonic()
        seen = self._members.get(user.id)
        if seen is not None and now - seen[1] < MEMBER_TTL:
            return seen[0]
        allowed = await home_member(self.bot, user.id)
        if allowed is None:
            return True
        self._members[user.id] = (allowed, now)
        return allowed

    async def _turn(self, user: discord.abc.User, question: str,
                    fresh: bool) -> Answer:
        """One question in this person's conversation; records the turn."""
        history = self._history(user.id, fresh=fresh)
        result = await self.respond(user, question, history)
        if result.picks and result.page is None:
            first = result.picks[0]
            result.page = await self._page(first["media_type"],
                                           first["tmdb_id"], first)
        if result.record:
            self._record(user.id, question, result.text,
                         [seerrapi.display_title(p) for p in result.picks])
        return result

    # ------------------------------- private answers (request panel, Reply)

    async def private_answer(self, interaction: discord.Interaction,
                             question: str, fresh: bool,
                             deferred: bool = False) -> None:
        """Answer visible only to the asker, with a Reply button. The request
        panel hands over questions, and anything typed that isn't a title."""
        if interaction.user.id in self._busy:
            if deferred:
                await interaction.followup.send(
                    "Still working on your last question.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Still working on your last question.", ephemeral=True)
            return
        if not deferred:
            await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._member(interaction.user, interaction.guild):
            await interaction.followup.send(
                "The assistant is only for members of this bot's server.",
                ephemeral=True)
            return
        self._busy.add(interaction.user.id)
        try:
            result = await self._turn(interaction.user, question, fresh)
            asked = ("-# You asked: " + discord.utils.escape_markdown(
                requestui._clip(question, 200)))
            view = self._view(result, asked=asked, button=ReplyButton())
            await requestui.followup(interaction, view, ephemeral=True)
        except OverLimit:
            await interaction.followup.send(self._limit_text(),
                                            ephemeral=True)
        except ai.AIError as exc:
            logger.warning("AI request failed: %s", exc)
            await interaction.followup.send(self._failure(exc),
                                            ephemeral=True)
        except Exception:
            logger.exception("Assistant answer failed")
            await interaction.followup.send(
                "Something went wrong answering that.", ephemeral=True)
        finally:
            self._busy.discard(interaction.user.id)

    # ----------------------------------------------------- /recap (Seerr)

    async def show_autocomplete(self, interaction: discord.Interaction,
                                current: str):
        cog = self.bot.get_cog("Requests")
        if cog is None or self._seerr() is None:
            return []
        results = [r for r in await cog._search(current)
                   if r["media_type"] == "tv"]
        return [app_commands.Choice(name=cog._choice_name(r),
                                    value=f"tv:{r['tmdb_id']}")
                for r in results[:25]]

    @staticmethod
    def recap_start(seasons: List[Dict[str, Any]],
                    position: Optional[Dict[str, int]],
                    season: Optional[int], episode: Optional[int]
                    ) -> Optional[Tuple[int, int]]:
        """Where the recap stops, as Seerr's own Catch Me Up picks it: what
        the person asked for; else the next episode their history says they
        haven't seen; else the start of the latest season. One past a
        season's last episode means "the whole season". None when there is
        nothing before it to recap."""
        regular = sorted(
            (s for s in seasons
             if int(s.get("seasonNumber") or 0) >= 1
             and int(s.get("episodeCount") or 0) > 0),
            key=lambda s: int(s["seasonNumber"]))
        if not regular:
            return None
        counts = {int(s["seasonNumber"]): int(s["episodeCount"])
                  for s in regular}
        first = int(regular[0]["seasonNumber"])
        if season:
            if season not in counts:
                return None
            start = (season, max(1, min(episode or 1, counts[season] + 1)))
        elif position and position.get("season") in counts:
            start = (position["season"],
                     max(1, min(position["episode"],
                                counts[position["season"]] + 1)))
        else:
            start = (int(regular[-1]["seasonNumber"]), 1)
        if start == (first, 1):
            if counts[first] < 2 or season:
                return None
            start = (first, 2)
        return start

    @app_commands.command(name="recap",
                          description="Catch me up: a spoiler-free recap of "
                                      "a show, up to where you are")
    @app_commands.describe(show="Start typing, then pick from the list",
                           season="Recap everything before this season "
                                  "(default: from your watch history)",
                           episode="…and this episode of it")
    async def recap(self, interaction: discord.Interaction, show: str,
                    season: Optional[app_commands.Range[int, 1, 200]] = None,
                    episode: Optional[app_commands.Range[int, 1, 2000]] = None
                    ) -> None:
        seerr = self._seerr()
        if seerr is None or self._engine() == "own" \
                or not self.config.get("enabled", True) \
                or seerr.has_ai is False:
            await interaction.response.send_message(
                "Recaps come from Seerr AI's Catch Me Up, which isn't set up "
                "here.", ephemeral=True)
            return
        match = re.match(r"^tv:(\d+)$", show.strip())
        if not match:
            await interaction.response.send_message(
                "Pick the show from the list that appears as you type.",
                ephemeral=True)
            return
        if episode and not season:
            await interaction.response.send_message(
                "Give the season too — or leave both out and I'll go by "
                "your watch history.", ephemeral=True)
            return
        tv_id = int(match[1])
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._recap(interaction, seerr, tv_id, season, episode)
        except Exception:
            logger.exception("/recap failed")
            await interaction.followup.send(
                "Something went wrong making that recap.", ephemeral=True)

    async def _recap(self, interaction: discord.Interaction, seerr,
                     tv_id: int, season: Optional[int],
                     episode: Optional[int]) -> None:
        try:
            user = await seerr.user_for_discord(interaction.user.id)
            if user is None:
                requests = self.bot.get_cog("Requests")
                if requests is not None:
                    await requests.send_link_prompt(
                        interaction, note="Recaps follow your own watch "
                                          "history, so link your Seerr "
                                          "account first.")
                else:
                    await interaction.followup.send(
                        seerrapi.link_help(interaction.user.id),
                        ephemeral=True)
                return
            details = await seerr.details("tv", tv_id)
            position = None
            if not season:
                try:
                    position = await seerr.ai_recap_position(user["id"],
                                                             tv_id)
                except SeerrError as exc:
                    if exc.status == 403:
                        raise
                    position = None
            start = self.recap_start(details.get("seasons") or [], position,
                                     season, episode)
            name = str(details.get("name") or "that show")
            if start is None:
                await interaction.followup.send(
                    "There's nothing before that to recap." if season else
                    f"There's nothing to recap yet for {name}.",
                    ephemeral=True)
                return
            text = await seerr.ai_recap(user["id"], tv_id, *start)
        except SeerrError as exc:
            if exc.status == 404 and await seerr.check_ai(force=True) \
                    is False:
                await interaction.followup.send(
                    "Recaps come from Seerr AI's Catch Me Up, which isn't "
                    "set up here.", ephemeral=True)
                return
            await interaction.followup.send(
                "Seerr isn't reachable right now — try again in a minute."
                if exc.status == 0 else
                (exc.message or "Seerr's AI couldn't do that."),
                ephemeral=True)
            return
        s, e = start
        count = next((int(x.get("episodeCount") or 0)
                      for x in details.get("seasons") or []
                      if int(x.get("seasonNumber") or -1) == s), 0)
        upto = (f"the end of season {s}" if e > count
                else f"S{s:02d}E{e:02d}")
        where = "" if season else (" (from your watch history)"
                                   if position else "")
        heading = (f"### Previously on {discord.utils.escape_markdown(name)}"
                   f"\n-# Everything before {upto}{where}")
        body = text or "Seerr's AI didn't come back with a recap."
        view = requestui.Card()
        view.add_item(ui.Container(
            ui.TextDisplay(heading),
            ui.TextDisplay(body if len(body) <= MAX_ANSWER
                           else body[: MAX_ANSWER - 1] + "…"),
            ui.TextDisplay("-# Seerr AI · spoiler-free · can be wrong"),
            accent_colour=0x5865F2))
        await requestui.followup(interaction, view, ephemeral=True)

    recap.autocomplete("show")(show_autocomplete)

    # --------------------------------------------------- @mention and DMs

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None \
                or not self._ready():
            return
        if message.guild is None:
            if self.config.get("dms", True) and message.content.strip() \
                    and await self._member(message.author):
                await self._dm_turn(message.author, message.content.strip(),
                                    fresh=False, channel=message.channel)
            return
        if self.bot.user not in message.mentions \
                or not self._allowed_here(message.channel.id):
            return
        if not await self._member(message.author, message.guild):
            return
        # A voice channel's text chat belongs to whoever is in the call
        # (a watch party, say): a mention there isn't a question for the
        # AI, unless that chat is the configured assistant channel.
        if getattr(message.channel, "type", None) in (
                discord.ChannelType.voice, discord.ChannelType.stage_voice) \
                and message.channel.id != self._channel_id():
            return
        question = _strip_mentions(message.content, self.bot.user.id) \
            or "Hi!"
        await self._take_private(message, question)

    async def _take_private(self, message: discord.Message,
                            question: str) -> None:
        """Move a channel @mention into the person's DMs."""
        me = message.guild.me if message.guild else None
        can_delete = bool(me and message.channel.permissions_for(me)
                          .manage_messages)
        if self.config.get("delete_mentions", True) and can_delete:
            try:
                await message.delete()
            except discord.HTTPException as exc:
                logger.info("Could not remove an assistant mention: %s", exc)
        try:
            dm = await message.author.create_dm()
            delivered = await self._dm_turn(message.author, question,
                                            fresh=True, channel=dm)
        except discord.HTTPException:
            delivered = False
        if delivered:
            notice = (f"{message.author.mention} answered you in DMs "
                      "\N{ENVELOPE}")
        else:
            notice = (f"{message.author.mention} I can't DM you — allow "
                      "direct messages from this server, or use **Ask AI** in "
                      "the request panel for a private answer.")
        try:
            await message.channel.send(
                notice, delete_after=NOTICE_SECONDS,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author], everyone=False, roles=False))
        except discord.HTTPException:
            pass

    async def _dm_turn(self, user: discord.abc.User, question: str,
                       fresh: bool, channel) -> bool:
        """One conversational turn in a DM. False when the DM can't be sent
        (the person blocks DMs from server members)."""
        if user.id in self._busy:
            return True
        self._busy.add(user.id)
        try:
            async with channel.typing():
                result = await self._turn(user, question, fresh)
            view = self._view(result, button=NewChatButton())
            try:
                await channel.send(
                    view=view, allowed_mentions=discord.AllowedMentions.none())
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                # The card was refused (too long, a bad poster URL...): the
                # same thing as plain text and classic buttons.
                logger.warning("DM answer card refused (%s) -- sending it "
                               "as plain text", exc)
                content, classic = requestui.plain_version(view)
                await channel.send(content=content, view=classic,
                                   allowed_mentions=discord
                                   .AllowedMentions.none())
            return True
        except discord.Forbidden:
            return False
        except OverLimit:
            await channel.send(self._limit_text())
            return True
        except ai.AIError as exc:
            logger.warning("AI request failed: %s", exc)
            await channel.send(self._failure(exc))
            return True
        except Exception:
            logger.exception("Assistant DM reply failed")
            await channel.send("Something went wrong answering that.")
            return True
        finally:
            self._busy.discard(user.id)


class AskModal(ui.Modal, title="Reply to the assistant"):
    question = ui.TextInput(label="Your reply", style=discord.TextStyle.paragraph,
                            max_length=1000)

    def __init__(self, cog: Assistant) -> None:
        super().__init__(timeout=900)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.private_answer(interaction, str(self.question.value),
                                      fresh=False)


class ReplyButton(ui.DynamicItem[ui.Button], template=r"plexmon:ai:reply"):
    """Continue an ephemeral AI conversation. Only the asker can see the
    message it sits on, so only they can press it."""

    def __init__(self) -> None:
        super().__init__(ui.Button(label="Reply", emoji="💬",
                                   style=discord.ButtonStyle.primary,
                                   custom_id="plexmon:ai:reply"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Assistant")
        if cog is None or not cog._ready():
            await interaction.response.send_message(
                "The assistant is switched off right now.", ephemeral=True)
            return
        await interaction.response.send_modal(AskModal(cog))


class NewChatButton(ui.DynamicItem[ui.Button], template=r"plexmon:ai:new"):
    """Start the DM conversation over."""

    def __init__(self) -> None:
        super().__init__(ui.Button(label="New chat", emoji="🧹",
                                   style=discord.ButtonStyle.secondary,
                                   custom_id="plexmon:ai:new"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Assistant")
        if cog is not None:
            cog.forget(interaction.user.id)
        await interaction.response.send_message(
            "Fresh start — what's next?")


AI_ITEMS = (ReplyButton, NewChatButton, aipager.PageButton)
# Buttons under an answer, rebuilt when a page turns: (custom_id, maker).
BOTTOM_BUTTONS = (("plexmon:ai:reply", ReplyButton),
                  ("plexmon:ai:new", NewChatButton),
                  ("plexmon:link:start", linkui.LinkStartButton))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Assistant(bot))
