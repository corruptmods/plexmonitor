"""Requests cog -- requesting (through the request panel), /issue and
"now on Plex" announcements.

Everything goes through Seerr (util/seerr.py), acting
as the Discord user who asked, so Seerr's per-person permissions, quotas
and approval rules apply exactly as they do in the web UI.

Linking: a person's Seerr profile holds their Discord user ID (Seerr ->
profile -> Settings -> Notifications -> Discord). Anyone without one is
told how to add it. Admins can also `/link` a member to a Seerr account
(stored in data/seerr_links.json), and `requests.discord_links` in
config.json still works.

Announcements poll Seerr rather than waiting on its webhook: a webhook sent
while the bot is restarting is simply lost, whereas the next poll still
finds the completed request. Already-announced request ids persist in
data/announced_requests.json, and the first run records the backlog
without posting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from util import linkui, requestui
from util.auth import admin_users, is_admin
from util.jsonfile import data_dir, read_json, write_json
from util import seerr as seerrapi
from util.seerr import SeerrClient, SeerrError

logger = logging.getLogger("plexmonitor.requests")

AUTOCOMPLETE_TIMEOUT = 2.5
ANNOUNCED_KEEP = 500
# Tries per title before an announcement that keeps failing is dropped.
MAX_ATTEMPTS = 3
# One pending ask-an-admin link request per person per this many seconds.
LINK_ASK_GAP = 600.0
# Between one person's tries at naming their account.
LINK_TRY_GAP = 10.0
# The green of a "Now on Plex" post.
ANNOUNCE_COLOUR = 0x23A55A
DEFAULTS = {
    "enabled": True,
    "announce_channel_id": 0,
    "poll_seconds": 120,
    "max_age_hours": 48,
    "discord_links": {},
    "panel_channel_id": 0,
    "watch_party_dms": False,
}
# How a sentence starts, as opposed to a title. Titles can be long ("Harry
# Potter and the Deathly Hallows") and can be questions ("What We Do in the
# Shadows"), so an exact title match always wins over this.
_ASKING = re.compile(
    r"^(what|whats|what's|which|who|when|where|why|how|is|are|was|were|do|"
    r"does|did|can|could|would|should|will|any|anything|something|"
    r"recommend|suggest|find|show me|give me|got|i want|i'd like|"
    r"i'm looking|im looking|looking for|need|more like|like|similar|"
    r"movies|shows|films|series|a (?:movie|show|film|series)|some|tell)\b",
    re.I)
_WORDS = re.compile(r"[^\w\s]+")


def looks_like_question(text: str) -> bool:
    text = text.strip()
    return "?" in text or bool(_ASKING.match(text)) \
        or len(text.split()) >= 7


def _norm(text: str) -> str:
    return " ".join(_WORDS.sub(" ", text.lower()).split())


def matches_title(text: str, result: Dict[str, Any]) -> bool:
    """`text` names this result: its title, optionally with its year."""
    typed = _norm(text)
    title = _norm(result.get("title") or "")
    year = str(result.get("year") or "")
    return bool(title) and typed in (title, f"{title} {year}".strip())
ISSUE_VALUE = re.compile(r"^issue:(\d+):(movie|tv):(\d+)(?::(.*))?$")
# What the issue form offers, and how each reaches Seerr. Every choice is one
# of Seerr's own issue types, so plain Seerr takes them all. Seerr AI also
# fixes most problems by itself from the issue's type and words: a file that
# will not play is replaced, subtitles are fetched or made, a wrong episode
# is swapped, and missing episodes are searched for again -- by Sonarr, then
# Backfill, then (once per title) the admin agent. The wording the bot adds
# is what Seerr AI reads to tell which fix is wanted.
ISSUE_LABELS = {"video": "Won't play, or the picture is wrong",
                "audio": "Sound problem",
                "subtitles": "Subtitles missing or wrong",
                "missing": "Missing episodes",
                "wrong": "Wrong episode",
                "other": "Something else"}
# Only for shows.
ISSUE_TV_ONLY = ("missing", "wrong")
# What happens next, as the reporter is told: with Seerr AI (ISSUE_NEXT),
# and with plain Seerr, where an admin handles every issue (ISSUE_TOLD).
ISSUE_TOLD = "It's in Seerr and the admins have been told."
_ON_IT = ("Seerr is on it, and posts what it did on the issue. An admin "
          "only steps in if it can't sort it out.")
ISSUE_NEXT = {
    "video": "Seerr replaces the file with a fresh copy. " + _ON_IT,
    "audio": "Seerr replaces the file with a fresh copy. " + _ON_IT,
    "subtitles": "Seerr looks for subtitles, and makes them from the audio "
                 "if none exist. " + _ON_IT,
    "missing": "Sonarr searches again and Backfill checks other sources at "
               "the same time; if neither finds it, the AI agent looks "
               "further. " + _ON_IT,
    "wrong": "Seerr swaps in the right episode. " + _ON_IT,
    "other": ISSUE_TOLD,
}
# Seerr issue type, and the words that tell Seerr which fix it is.
ISSUE_SENT = {"video": ("video", ""), "audio": ("audio", ""),
              "subtitles": ("subtitles", ""),
              "missing": ("other", "Missing episodes."),
              "wrong": ("other", "Wrong episode."),
              "other": ("other", "")}


def _parse_time(value: Any) -> float:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()




def explain(exc: SeerrError) -> str:
    """Seerr's refusal in words a person can act on."""
    if exc.status == 0:
        return "Seerr isn't reachable right now — try again in a minute."
    if exc.status == 409:
        return "That's already been requested."
    if exc.status == 202:
        return "Every season is already on Plex or already requested."
    if exc.status in (401, 403):
        return f"Seerr said no: {exc.message}"
    if exc.status == 404:
        return "Seerr couldn't find that title."
    return f"Seerr had a problem ({exc.status}): {exc.message}"


class IssueModal(ui.Modal):
    """Issue report form. The problem type uses Discord's radio group when
    the client supports modals with it, falling back to a select."""

    def __init__(self, cog: "Requests", media_id: int, kind: str,
                 title: str, radio: bool = True) -> None:
        super().__init__(title=requestui._clip(f"Report: {title}", 45),
                         timeout=600)
        self.cog = cog
        self.media_id = media_id
        self.kind = kind
        offered = {value: label for value, label in ISSUE_LABELS.items()
                   if kind == "tv" or value not in ISSUE_TV_ONLY}
        if radio and hasattr(ui, "RadioGroup"):
            self.problem = ui.RadioGroup(options=[
                discord.RadioGroupOption(label=label, value=value)
                for value, label in offered.items()])
        else:
            self.problem = ui.Select(options=[
                discord.SelectOption(label=label, value=value)
                for value, label in offered.items()],
                placeholder="What's wrong?")
        self.add_item(ui.Label(text="What's wrong?", component=self.problem))
        self.season = self.episode = None
        if kind == "tv":
            self.season = ui.TextInput(required=False, max_length=3,
                                       placeholder="e.g. 2")
            self.episode = ui.TextInput(required=False, max_length=4,
                                        placeholder="e.g. 5")
            self.add_item(ui.Label(text="Season (optional)",
                                   component=self.season))
            self.add_item(ui.Label(text="Episode (optional)",
                                   component=self.episode))
        self.details = ui.TextInput(style=discord.TextStyle.paragraph,
                                    max_length=1000, required=False,
                                    placeholder=(
                                        "Optional: audio drifts out of sync "
                                        "about 40 minutes in"))
        self.add_item(ui.Label(text="Anything else? (optional)",
                               component=self.details))

    def _problem_value(self) -> str:
        value = getattr(self.problem, "value", None)
        if value is None:
            values = getattr(self.problem, "values", None) or []
            value = values[0] if values else None
        return str(value or "other")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def number(field) -> int:
            text = str(getattr(field, "value", "") or "").strip()
            return int(text) if text.isdigit() else 0
        await self.cog.file_issue(
            interaction, self.media_id, self._problem_value(),
            str(self.details.value or ""), number(self.season),
            number(self.episode))

    async def on_error(self, interaction, error) -> None:
        logger.exception("Issue form failed", exc_info=error)
        await requestui.sorry(interaction,
                              "Something went wrong filing that — try again.")


class ReportPick(ui.View):
    """Picks the title a report is about, then opens its issue form. The
    form is a popup, which only a press can open, so even a single match
    is one button."""

    def __init__(self, cog: "Requests", picks: List[Dict[str, Any]]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.picks = {str(r["media_id"]): r for r in picks}
        if len(picks) == 1:
            button = ui.Button(label="Report a problem", emoji="🛠️",
                               style=discord.ButtonStyle.primary)
            button.callback = self._chosen(picks[0])
            self.add_item(button)
            return
        select = ui.Select(placeholder="Pick the title", options=[
            discord.SelectOption(
                label=requestui._clip(seerrapi.display_title(r), 100),
                emoji="🎬" if r["media_type"] == "movie" else "📺",
                value=str(r["media_id"]))
            for r in picks])

        async def picked(interaction: discord.Interaction) -> None:
            await self._chosen(self.picks[select.values[0]])(interaction)
        select.callback = picked
        self.add_item(select)

    def _chosen(self, pick: Dict[str, Any]):
        async def open_form(interaction: discord.Interaction) -> None:
            await self.cog._open_issue_form(
                interaction, int(pick["media_id"]), pick["media_type"],
                seerrapi.display_title(pick))
        return open_form


class Requests(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_dir = data_dir()
        self._config_path = os.path.join(self.data_dir, "config.json")
        self._links_file = os.path.join(self.data_dir, "seerr_links.json")
        self._config_mtime = -1.0
        self.config = dict(DEFAULTS)
        self.seerr = SeerrClient(os.getenv("SEERR_URL", ""),
                                 os.getenv("SEERR_API_KEY", ""),
                                 public_url=os.getenv("SEERR_PUBLIC_URL", ""))
        self._reload_config()
        self.state_file = os.path.join(self.data_dir,
                                       "announced_requests.json")
        stored = read_json(self.state_file, None)
        self._seeded = isinstance(stored, dict) and bool(stored.get("seeded"))
        self._announced: List[int] = [
            int(i) for i in (stored or {}).get("ids", [])
            if str(i).isdigit()] if isinstance(stored, dict) else []
        self._warned_channel = False
        self._failures: Dict[str, int] = {}
        self._link_asks: Dict[int, float] = {}
        self._link_tries: Dict[int, float] = {}
        self._users_task = None
        # Links made with /link apply even without a data/config.json.
        self._apply_links()
        if not self.seerr.enabled:
            logger.info("SEERR_URL/SEERR_API_KEY not set -- requests, /issue "
                        "and announcements are disabled")

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
        section = raw.get("requests") if isinstance(raw, dict) else None
        merged = dict(DEFAULTS)
        if isinstance(section, dict):
            merged.update(section)
        self.config = merged
        self._apply_links()

    def _apply_links(self) -> None:
        """Manual links: config.json's `discord_links`, then /link's file."""
        links: Dict[Any, Any] = {}
        configured = self.config.get("discord_links")
        if isinstance(configured, dict):
            links.update(configured)
        stored = read_json(self._links_file, {})
        if isinstance(stored, dict):
            links.update(stored)
        self.seerr.set_overrides(links)

    def _int(self, key: str, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(self.config.get(key)
                                          or DEFAULTS[key])))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    async def cog_load(self) -> None:
        for item in requestui.DYNAMIC_ITEMS + linkui.LINK_ITEMS:
            self.bot.add_dynamic_items(item)
        if self.seerr.enabled:
            self.announce_loop.change_interval(
                seconds=self._int("poll_seconds", 30, 3600))
            self.announce_loop.start()

    async def cog_unload(self) -> None:
        self.announce_loop.cancel()
        try:
            self.bot.remove_dynamic_items(*requestui.DYNAMIC_ITEMS,
                                          *linkui.LINK_ITEMS)
        except Exception:
            logger.debug("remove_dynamic_items failed", exc_info=True)
        await self.seerr.close()

    async def _ready(self, interaction: discord.Interaction) -> bool:
        if not self.seerr.enabled or not self.config.get("enabled", True):
            await interaction.response.send_message(
                "Requests aren't set up on this bot.", ephemeral=True)
            return False
        return True

    # ------------------------------------------------------------ autocomplete

    async def _search(self, text: str) -> List[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(
                self.seerr.search(text, timeout=AUTOCOMPLETE_TIMEOUT),
                AUTOCOMPLETE_TIMEOUT + 0.3)
        except (SeerrError, asyncio.TimeoutError):
            return []

    @staticmethod
    def _choice_name(result: Dict[str, Any]) -> str:
        icon = "🎬" if result["media_type"] == "movie" else "📺"
        name = f"{icon} {seerrapi.display_title(result)}"
        if result.get("status_label"):
            name += f" · {result['status_label']}"
        return requestui._clip(name, 100)

    def _assistant(self):
        """The Assistant cog when it can answer, else None."""
        cog = self.bot.get_cog("Assistant")
        return cog if cog is not None and cog._ready() else None

    async def issue_autocomplete(self, interaction: discord.Interaction,
                                 current: str):
        if not self.seerr.enabled:
            return []
        results = [r for r in await self._search(current)
                   if r.get("on_plex") and r.get("media_id")]
        choices = []
        for r in results[:25]:
            # The title rides along in the value so /issue can open its form
            # without a network call -- a modal must be the FIRST response,
            # inside Discord's three seconds.
            value = f"issue:{r['media_id']}:{r['media_type']}:{r['tmdb_id']}:"
            value += seerrapi.display_title(r)[:100 - len(value)]
            choices.append(app_commands.Choice(name=self._choice_name(r),
                                               value=value))
        return choices

    # ------------------------------------------------ routing typed titles

    async def route(self, interaction: discord.Interaction,
                    title: str) -> None:
        """What the request panel's popup does with what was typed: a clear
        title shows its card, several matches give a picker, and anything
        else -- a sentence, a typo, nothing found -- goes to the AI. The
        interaction must not have been answered yet."""
        text = " ".join(title.split())
        assistant = self._assistant()
        if not self.seerr.enabled or not self.config.get("enabled", True):
            if assistant is not None:
                await assistant.private_answer(interaction, text, fresh=True)
                return
            await self._ready(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await self.seerr.search(text)
        except SeerrError as exc:
            if assistant is not None and assistant._own_ready():
                await assistant.private_answer(interaction, text, fresh=True,
                                               deferred=True)
                return
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        exact = [r for r in results if matches_title(text, r)]
        if len(exact) == 1 or (len(results) == 1
                               and not looks_like_question(text)):
            pick = exact[0] if exact else results[0]
            await self.show_card(interaction, pick["media_type"],
                                 pick["tmdb_id"], deferred=True)
            return
        # Not a title: a sentence, a typo, or nothing matched. The AI
        # answers, privately; it looks titles up itself and offers cards.
        # Several exact matches (a film and a series of the same name) are
        # still titles: those get the picker below.
        if assistant is not None and not exact:
            await assistant.private_answer(interaction, text, fresh=True,
                                           deferred=True)
            return
        if not results:
            await interaction.followup.send(
                f"Nothing matched “{discord.utils.escape_markdown(text)}”. "
                "Try the original title, or add the year.", ephemeral=True)
            return
        await requestui.followup(interaction, requestui.picker(
            (exact or results)[:25], text))

    async def report(self, interaction: discord.Interaction,
                     title: str) -> None:
        """What the panel's Report popup does with what was typed: find it
        on Plex and open the issue form for it. Only what is on Plex can be
        reported; something that never arrived is requested instead. The
        interaction must not have been answered yet."""
        if not await self._ready(interaction):
            return
        text = " ".join(title.split())
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await self.seerr.search(text)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        def on_plex(found):
            return [r for r in found
                    if r.get("on_plex") and r.get("media_id")]
        # What was typed names a title: only that one, or none if it is not
        # on Plex -- never some other title that merely came up.
        exact = [r for r in results if matches_title(text, r)]
        picks = on_plex(exact if exact else results)[:25]
        if not picks:
            await interaction.followup.send(
                f"I can't find “{discord.utils.escape_markdown(text)}” on "
                "Plex. If it never arrived, press **Request** instead; if "
                "it's there under another name, try that.", ephemeral=True)
            return
        await interaction.followup.send(
            "Which one has the problem?" if len(picks) > 1 else
            f"Report a problem with **"
            f"{discord.utils.escape_markdown(seerrapi.display_title(picks[0]))}"
            "**?", view=ReportPick(self, picks), ephemeral=True)

    async def card_for(self, kind: str, tmdb_id: int,
                       note: str = "") -> ui.LayoutView:
        children, accent = await self.card_parts(kind, tmdb_id, note)
        view = requestui.Card()
        view.add_item(ui.Container(*children, accent_colour=accent))
        return view

    async def card_parts(self, kind: str, tmdb_id: int, note: str = ""):
        """A title card's contents and accent, fresh from Seerr: poster,
        facts, status, Request / season picks, Watch on Plex, Open in
        Seerr. Raises SeerrError."""
        details, partial, public = await asyncio.gather(
            self.seerr.details(kind, tmdb_id),
            self.seerr.partial_requests(),
            self.seerr.public_settings())
        link = await self.seerr.media_link(kind, tmdb_id)
        return requestui.media_items(
            details, kind, partial_requests=partial,
            specials=bool(public.get("enableSpecialEpisodes", False)),
            seerr_link=link, note=note)

    @requestui.answers_errors("Showing a title")
    async def show_card(self, interaction: discord.Interaction, kind: str,
                        tmdb_id: int, deferred: bool = False,
                        edit: bool = False) -> None:
        if not deferred:
            if edit:
                await interaction.response.defer()
            else:
                await interaction.response.defer(ephemeral=True,
                                                 thinking=True)
        try:
            view = await self.card_for(kind, tmdb_id)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        if edit:
            await requestui.edit_original(interaction, view)
        else:
            await requestui.followup(interaction, view)

    @requestui.answers_errors("A request")
    async def handle_request(self, interaction: discord.Interaction,
                             kind: str, tmdb_id: int,
                             seasons: Sequence[int]) -> None:
        """A Request button or season pick, from any message."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user = await self.seerr.user_for_discord(interaction.user.id)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        if user is None:
            await self.send_link_prompt(
                interaction, note="I can't request as you until your "
                                  "Discord is linked to Seerr.")
            return
        try:
            request = await self.seerr.create_request(user["id"], kind,
                                                      tmdb_id, seasons)
        except SeerrError as exc:
            logger.info("Request %s:%s by %s refused: %s", kind, tmdb_id,
                        user["name"], exc)
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        status = request.get("status")
        chosen = [s.get("seasonNumber") for s in request.get("seasons") or []
                  if s.get("seasonNumber") is not None]
        what = requestui.season_label(chosen) if kind == "tv" else ""
        logger.info("Requested %s:%s%s for %s (Seerr user %s), status %s",
                    kind, tmdb_id, f" [{what}]" if what else "",
                    interaction.user, user["id"], status)
        channel = self._announce_channel()
        where = (f" I'll post in {channel.mention} when it's ready."
                 if channel is not None else "")
        if status == seerrapi.REQUEST_PENDING:
            note = "Requested — it's waiting for approval."
        else:
            note = "Requested — it's on its way." + where
        if what:
            note = note.replace("Requested", f"Requested {what}", 1)
        try:
            view = await self.card_for(kind, tmdb_id, note=note)
            await requestui.followup(interaction, view)
        except SeerrError:
            await interaction.followup.send(f"✅ {note}", ephemeral=True)

    # ----------------------------------------------------------------- /issue

    @app_commands.command(name="issue",
                          description="Report a problem with something on "
                                      "Plex")
    @app_commands.describe(title="Start typing, then pick from the list")
    @app_commands.guild_only()
    async def issue_command(self, interaction: discord.Interaction,
                            title: str) -> None:
        if not await self._ready(interaction):
            return
        match = ISSUE_VALUE.match(title.strip())
        if not match:
            await interaction.response.send_message(
                "Pick the title from the list that appears as you type — "
                "that's how I know which copy on Plex you mean.",
                ephemeral=True)
            return
        media_id, kind = int(match[1]), match[2]
        name = match[4] or "this title"
        await self._open_issue_form(interaction, media_id, kind, name)

    issue_command.autocomplete("title")(issue_autocomplete)

    async def _open_issue_form(self, interaction, media_id, kind, name):
        try:
            await interaction.response.send_modal(
                IssueModal(self, media_id, kind, name, radio=True))
        except discord.HTTPException as exc:
            # 50035 = the form itself was refused (a radio group Discord
            # will not take in a modal); the interaction is still unanswered
            # then, so retry with a select. Anything else -- an expired or
            # already-answered interaction -- cannot be retried.
            if getattr(exc, "code", None) != 50035:
                logger.warning("Could not open the issue form: %s", exc)
                return
            logger.info("Radio-group modal rejected (%s) — using a select",
                        exc)
            await interaction.response.send_modal(
                IssueModal(self, media_id, kind, name, radio=False))

    @requestui.answers_errors("Filing an issue")
    async def file_issue(self, interaction: discord.Interaction,
                         media_id: int, problem: str, message: str,
                         season: int, episode: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user = await self.seerr.user_for_discord(interaction.user.id)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        if user is None:
            await self.send_link_prompt(
                interaction, note="I can't report issues as you until your "
                                  "Discord is linked to Seerr.")
            return
        kind, said = ISSUE_SENT.get(problem, ISSUE_SENT["other"])
        issue_type = seerrapi.ISSUE_TYPES.get(kind, 4)
        message = " ".join(part for part in (said, message.strip()) if part)
        try:
            issue = await self.seerr.create_issue(
                user["id"], media_id, issue_type, message or "(no details)",
                season, episode)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        logger.info("Issue %s filed by %s (Seerr user %s)", issue.get("id"),
                    interaction.user, user["id"])
        # Filed: from here on, a hiccup must not read as "try again", which
        # would file it twice.
        try:
            link = f"{await self.seerr.app_url()}/issues/{issue.get('id')}"
            view = requestui.Card()
            buttons = [ui.Button(label="View in Seerr", url=link,
                                 style=discord.ButtonStyle.link)] \
                if link.startswith("http") else []
            fixes_itself = await self.seerr.check_ai() is True
            body: List[ui.Item] = [ui.TextDisplay(
                "### 🛠️ Problem reported\n" + (ISSUE_NEXT.get(
                    problem, ISSUE_TOLD) if fixes_itself else ISSUE_TOLD))]
            if buttons:
                body.append(ui.ActionRow(*buttons))
            view.add_item(ui.Container(*body, accent_colour=0xF0B232))
            await requestui.followup(interaction, view)
        except Exception:
            logger.exception("Issue %s was filed, but the confirmation "
                             "failed", issue.get("id"))
            await requestui.sorry(
                interaction, f"🛠️ Reported (issue #{issue.get('id')}).")

    # ------------------------------------------------------------ /link, /unlink

    async def seerr_user_autocomplete(self, interaction: discord.Interaction,
                                      current: str):
        if not self.seerr.enabled or not await is_admin(interaction.client,
                                                        interaction.user):
            return []
        # Shielded: loading every user can outlast one keystroke's
        # deadline, and cancelling it on each keystroke would mean it
        # never finishes.
        if self._users_task is None or self._users_task.done():
            self._users_task = asyncio.ensure_future(self.seerr.users())
        try:
            users = await asyncio.wait_for(asyncio.shield(self._users_task),
                                           AUTOCOMPLETE_TIMEOUT)
        except (SeerrError, asyncio.TimeoutError):
            return []
        text = current.strip().lower()
        choices = []
        for user in sorted(users, key=lambda u: u["name"].lower()):
            label = user["name"]
            if user.get("plex_username") and \
                    user["plex_username"].lower() != label.lower():
                label += f" (Plex: {user['plex_username']})"
            if text and text not in label.lower():
                continue
            choices.append(app_commands.Choice(
                name=requestui._clip(label, 100), value=str(user["id"])))
        return choices[:25]

    def _stored_links(self) -> Dict[str, Any]:
        stored = read_json(self._links_file, {})
        return stored if isinstance(stored, dict) else {}

    def _write_links(self, change) -> None:
        """Change data/seerr_links.json. Raises OSError when it can't be
        saved, so nobody is told "linked" when nothing changed."""
        stored = self._stored_links()
        change(stored)
        if not write_json(self._links_file, stored):
            raise OSError(f"could not write {self._links_file}")
        self._apply_links()
        self.seerr.invalidate_links()

    async def _link(self, discord_id: int, seerr_id: int) -> Dict[str, Any]:
        """Store a link. Returns the Seerr user; raises SeerrError, or
        LookupError when that account doesn't exist."""
        users = {u["id"]: u for u in await self.seerr.users(fresh=True)}
        target = users.get(int(seerr_id))
        if target is None:
            raise LookupError(seerr_id)
        self._write_links(lambda links: links.__setitem__(
            str(int(discord_id)), target["id"]))
        return target

    @app_commands.command(name="link",
                          description="Link a member's Discord account to "
                                      "their Seerr account (bot admins)")
    @app_commands.describe(member="Who to link",
                           seerr_user="Their Seerr account")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def link_command(self, interaction: discord.Interaction,
                           member: discord.Member, seerr_user: str) -> None:
        # Linking someone lets them act as that Seerr account, so this is
        # for the bot's admins (DISCORD_AUTHORIZED_USERS, or its owner),
        # not whoever happens to manage some server the bot is in.
        if not await is_admin(interaction.client, interaction.user):
            await interaction.response.send_message(
                "Only the bot's admins can link accounts.", ephemeral=True)
            return
        if not await self._ready(interaction):
            return
        if not seerr_user.strip().isdigit():
            await interaction.response.send_message(
                "Pick their Seerr account from the list that appears as you "
                "type.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target = await self._link(member.id, int(seerr_user))
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        except LookupError:
            await interaction.followup.send(
                "That Seerr account doesn't exist (any more?).",
                ephemeral=True)
            return
        except Exception:
            logger.exception("/link failed")
            await interaction.followup.send(
                "Something went wrong saving that link.", ephemeral=True)
            return
        logger.info("%s linked %s (%s) to Seerr user %s (%s)",
                    interaction.user, member, member.id, target["id"],
                    target["name"])
        await interaction.followup.send(
            f"Linked {member.mention} to Seerr account **"
            f"{discord.utils.escape_markdown(target['name'])}**. Their "
            "requests, issues and AI answers now go through that account.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    link_command.autocomplete("seerr_user")(seerr_user_autocomplete)

    @app_commands.command(name="unlink",
                          description="Remove a link made with /link "
                                      "(bot admins)")
    @app_commands.describe(member="Who to unlink")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def unlink_command(self, interaction: discord.Interaction,
                             member: discord.Member) -> None:
        if not await is_admin(interaction.client, interaction.user):
            await interaction.response.send_message(
                "Only the bot's admins can unlink accounts.", ephemeral=True)
            return
        stored = read_json(self._links_file, {})
        if not isinstance(stored, dict) or str(member.id) not in stored:
            note = ("They weren't linked with /link." +
                    (" A link in config.json or their Seerr profile still "
                     "applies." if str(member.id) in {
                         str(k) for k in (self.config.get("discord_links")
                                          or {})} else ""))
            await interaction.response.send_message(note, ephemeral=True)
            return
        try:
            self._write_links(lambda links: links.pop(str(member.id), None))
        except OSError as exc:
            logger.warning("/unlink could not save: %s", exc)
            await interaction.response.send_message(
                "Something went wrong saving that.", ephemeral=True)
            return
        logger.info("%s unlinked %s (%s)", interaction.user, member,
                    member.id)
        await interaction.response.send_message(
            f"Unlinked {member.mention}. If their Seerr profile holds their "
            "Discord ID, that link still applies.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------- linking, self-service

    async def link_prompt(self, discord_id: int,
                          note: Optional[str] = None) -> ui.LayoutView:
        """How someone links themselves (see util/linkui.py)."""
        base = await self.seerr.app_url() if self.seerr.enabled else ""
        url = f"{base}/profile/settings/notifications/discord" \
            if base.startswith("http") else ""
        return linkui.prompt_view(discord_id, url, note)

    async def send_link_prompt(self, interaction: discord.Interaction,
                               note: Optional[str] = None) -> None:
        """After a defer: the link steps, only for that person."""
        await requestui.followup(
            interaction, await self.link_prompt(interaction.user.id, note),
            ephemeral=True)

    @requestui.answers_errors("Starting a link")
    async def start_link(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user = await self.seerr.user_for_discord(interaction.user.id)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        if user is not None:
            await interaction.followup.send(
                "You're already linked to Seerr as **"
                f"{discord.utils.escape_markdown(user['name'])}**.",
                ephemeral=True)
            return
        await self.send_link_prompt(interaction)

    @requestui.answers_errors("Checking a link")
    async def check_link(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        # user_for_discord re-reads Seerr's users when this person isn't
        # found, at most once a minute however often anyone presses.
        try:
            user = await self.seerr.user_for_discord(interaction.user.id)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        if user is None and interaction.user.id in self.seerr.contested:
            await interaction.followup.send(
                "Your Discord ID is saved on more than one Seerr account, so "
                "I can't tell which is yours. Remove it from the one that "
                "isn't, or ask an admin to link you.", ephemeral=True)
            return
        if user is None:
            await interaction.followup.send(
                "I don't see it yet. In Seerr, make sure your Discord ID "
                f"`{interaction.user.id}` is saved under **Settings → "
                "Notifications → Discord** on your own profile, then check "
                "again in a minute — or ask an admin instead.",
                ephemeral=True)
            return
        logger.info("%s (%s) linked themselves to Seerr user %s via their "
                    "Seerr profile", interaction.user, interaction.user.id,
                    user["id"])
        await interaction.followup.send(
            "✅ Linked! You're **"
            f"{discord.utils.escape_markdown(user['name'])}** in Seerr now — "
            "requests and AI answers use your own account.", ephemeral=True)

    @staticmethod
    def _matching_accounts(users: List[Dict[str, Any]], name: str
                           ) -> List[Dict[str, Any]]:
        wanted = " ".join(name.split()).lower()
        return [u for u in users if wanted and wanted in {
            str(u.get("name") or "").lower(),
            str(u.get("plex_username") or "").lower()}]

    @requestui.answers_errors("A link request")
    async def ask_admin_link(self, interaction: discord.Interaction,
                             name: str) -> None:
        """Someone names their Seerr account; the admins approve or not."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        requester = interaction.user
        last = self._link_asks.get(requester.id)
        if last is not None and time.monotonic() - last < LINK_ASK_GAP:
            await interaction.followup.send(
                "Your request is already with the admins — hang tight.",
                ephemeral=True)
            return
        tried = self._link_tries.get(requester.id)
        if tried is not None and time.monotonic() - tried < LINK_TRY_GAP:
            await interaction.followup.send(
                "Give it a few seconds before trying another name.",
                ephemeral=True)
            return
        self._link_tries[requester.id] = time.monotonic()
        try:
            users = await self.seerr.users(fresh=True)
        except SeerrError as exc:
            await interaction.followup.send(explain(exc), ephemeral=True)
            return
        found = self._matching_accounts(users, name)
        if len(found) != 1:
            await interaction.followup.send(
                ("Several Seerr accounts go by that name — ask an admin to "
                 "`/link` you." if found else
                 f"There's no Seerr account called “"
                 f"{discord.utils.escape_markdown(name.strip())}”. Use the "
                 "name Seerr shows on your profile, or your Plex username."),
                ephemeral=True)
            return
        target = found[0]
        already = await self.seerr.discord_for_user(target["id"])
        who = f"{requester.mention} (`{requester}`, ID `{requester.id}`)"
        account = discord.utils.escape_markdown(target["name"])
        plex = target.get("plex_username")
        if plex and plex.lower() != target["name"].lower():
            account += f" (Plex: {discord.utils.escape_markdown(plex)})"
        lines = ["### 🔗 Link request",
                 f"{who} wants to be linked to the Seerr account "
                 f"**{account}**.",
                 "-# Approving lets them request and use the AI as that "
                 "account."]
        if already:
            lines.append("⚠️ That account is already linked to "
                         + ", ".join(f"<@{d}>" for d in already) + ".")
        text = "\n".join(lines)
        view = requestui.Card()
        view.add_item(ui.Container(
            ui.TextDisplay(text),
            ui.ActionRow(
                linkui.LinkDecisionButton("ok", requester.id, target["id"]),
                linkui.LinkDecisionButton("no", requester.id, target["id"])),
            accent_colour=linkui.ACCENT))
        delivered = 0
        for admin in await admin_users(self.bot):
            try:
                await admin.send(view=view,
                                 allowed_mentions=discord.AllowedMentions.none())
                delivered += 1
            except discord.HTTPException as exc:
                logger.info("Could not DM admin %s a link request: %s",
                            admin, exc)
        if not delivered:
            await interaction.followup.send(
                "I couldn't reach an admin just now. Ask one to `/link` you, "
                "or add your Discord ID in Seerr yourself.", ephemeral=True)
            return
        self._link_asks[requester.id] = time.monotonic()
        logger.info("%s (%s) asked to be linked to Seerr user %s",
                    requester, requester.id, target["id"])
        await interaction.followup.send(
            "Sent to the admins. I'll DM you when they've approved it.",
            ephemeral=True)

    @requestui.answers_errors("A link decision")
    async def decide_link(self, interaction: discord.Interaction,
                          discord_id: int, seerr_id: int,
                          approve: bool) -> None:
        if not await is_admin(interaction.client, interaction.user):
            await interaction.response.send_message(
                "Only the bot's admins can approve links.", ephemeral=True)
            return
        await interaction.response.defer()
        name = f"Seerr user {seerr_id}"
        current = str(self._stored_links().get(str(int(discord_id))) or "")
        if current == str(int(seerr_id)):
            # Another admin already approved this one: close this copy of
            # the request too, without telling the member twice.
            self._link_asks.pop(discord_id, None)
            done = requestui.Card()
            done.add_item(ui.Container(
                ui.TextDisplay(f"✅ <@{discord_id}> is already linked to "
                               f"Seerr user {seerr_id}."),
                accent_colour=linkui.ACCENT))
            try:
                await interaction.edit_original_response(view=done)
            except discord.HTTPException as exc:
                logger.info("Could not update the link request: %s", exc)
            return
        if approve:
            try:
                target = await self._link(discord_id, seerr_id)
                name = target["name"]
            except SeerrError as exc:
                await interaction.followup.send(explain(exc), ephemeral=True)
                return
            except LookupError:
                await interaction.followup.send(
                    "That Seerr account doesn't exist any more.",
                    ephemeral=True)
                return
            except OSError as exc:
                logger.error("Approving a link failed: %s", exc)
                await interaction.followup.send(
                    "I couldn't save that link — check that the bot can "
                    "write to its data folder.", ephemeral=True)
                return
            except Exception:
                logger.exception("Approving a link failed")
                await interaction.followup.send(
                    "Something went wrong saving that link.", ephemeral=True)
                return
            logger.info("%s approved linking %s to Seerr user %s",
                        interaction.user, discord_id, seerr_id)
        else:
            logger.info("%s declined linking %s to Seerr user %s",
                        interaction.user, discord_id, seerr_id)
        self._link_asks.pop(discord_id, None)
        verdict = (f"✅ <@{discord_id}> linked to **"
                   f"{discord.utils.escape_markdown(name)}** by "
                   f"{interaction.user.mention}." if approve else
                   f"✖️ Declined by {interaction.user.mention}.")
        done = requestui.Card()
        done.add_item(ui.Container(ui.TextDisplay(verdict),
                                   accent_colour=linkui.ACCENT))
        try:
            await interaction.edit_original_response(view=done)
        except discord.HTTPException as exc:
            logger.info("Could not update the link request: %s", exc)
        member = self.bot.get_user(discord_id)
        try:
            member = member or await self.bot.fetch_user(discord_id)
            await member.send(
                f"✅ You're linked to Seerr as **"
                f"{discord.utils.escape_markdown(name)}**. Requests and AI "
                "answers now use your own account." if approve else
                "An admin didn't approve that Seerr link. Ask them about it, "
                "or add your Discord ID in Seerr yourself (profile → "
                "Settings → Notifications → Discord).")
        except (discord.HTTPException, AttributeError) as exc:
            logger.info("Could not tell %s about their link: %s",
                        discord_id, exc)

    # ---------------------------------------------------------- announcements

    def channel_setting(self, key: str, env: str) -> int:
        """A channel id from .env (wins) or the config.json section."""
        for value in (os.getenv(env, ""), self.config.get(key)):
            text = str(value or "").strip()
            if text.isdigit() and int(text):
                return int(text)
        return 0

    def _announce_channel(self):
        channel_id = self.channel_setting("announce_channel_id",
                                          "ANNOUNCE_CHANNEL_ID")
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None and not self._warned_channel:
            self._warned_channel = True
            logger.warning("requests.announce_channel_id %s not found",
                           channel_id)
        return channel

    def _save_state(self) -> None:
        self._announced = self._announced[-ANNOUNCED_KEEP:]
        write_json(self.state_file, {"seeded": True, "ids": self._announced})

    @staticmethod
    def _landed(request: Dict[str, Any]) -> bool:
        """Completed AND on Plex. Seerr also completes a request when its
        media is DELETED (a Maintainerr cleanup), which is not news."""
        if request.get("status") != seerrapi.REQUEST_COMPLETED:
            return False
        media = request.get("media") or {}
        status = media.get("status4k" if request.get("is4k") else "status")
        return status in (seerrapi.STATUS_AVAILABLE, seerrapi.STATUS_PARTIAL)

    async def poll_announcements(self) -> int:
        """One pass. Returns how many announcements were posted."""
        self._reload_config()
        if not self.config.get("enabled", True):
            return 0
        requests = await self.seerr.completed_requests(50)
        if not self._seeded:
            self._announced = [int(r["id"]) for r in requests
                               if r.get("id") is not None]
            self._seeded = True
            self._save_state()
            logger.info("Announcements armed -- %d already-completed "
                        "request(s) recorded without posting",
                        len(self._announced))
            return 0
        channel = self._announce_channel()
        if channel is None:
            return 0
        max_age = self._int("max_age_hours", 1, 24 * 30) * 3600
        now = time.time()
        known = set(self._announced)
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for request in requests:
            rid = request.get("id")
            if rid is None or int(rid) in known:
                continue
            if not self._landed(request):
                media = request.get("media") or {}
                status = media.get("status4k" if request.get("is4k")
                                   else "status")
                if request.get("status") == seerrapi.REQUEST_COMPLETED \
                        and status == seerrapi.STATUS_DELETED:
                    # Completed by a deletion. Remember it, or a later
                    # re-download would resurrect this old request as news.
                    self._announced.append(int(rid))
                continue
            changed = _parse_time(request.get("updatedAt"))
            if changed and now - changed > max_age:
                self._announced.append(int(rid))   # too old to be news
                continue
            media = request.get("media") or {}
            key = (media.get("mediaType") or request.get("type"),
                   media.get("tmdbId"))
            groups.setdefault(key, []).append(request)
        posted = 0
        for (kind, tmdb_id), batch in groups.items():
            key = f"{kind}:{tmdb_id}"
            try:
                if await self._announce(channel, kind, tmdb_id, batch):
                    posted += 1
            except Exception:
                self._failures[key] = self._failures.get(key, 0) + 1
                if self._failures[key] < MAX_ATTEMPTS:
                    logger.exception("Announcement for %s failed (attempt "
                                     "%d/%d)", key, self._failures[key],
                                     MAX_ATTEMPTS)
                    continue
                logger.error("Giving up announcing %s after %d attempts",
                             key, MAX_ATTEMPTS)
            self._failures.pop(key, None)
            self._announced.extend(int(r["id"]) for r in batch)
        if groups or len(self._announced) != len(known):
            self._save_state()
        if posted:
            # Search results carry each title's status: let /issue and the
            # request panel see what just landed straight away.
            self.seerr.forget_search()
        return posted

    async def _announce(self, channel, kind: str, tmdb_id: Any,
                        batch: List[Dict[str, Any]]) -> bool:
        if kind not in ("movie", "tv") or not tmdb_id:
            return False
        details = await self.seerr.details(kind, int(tmdb_id))
        summary = seerrapi.summarize({**details, "mediaType": kind})
        if summary is None:
            return False
        requesters: List[int] = []
        names: List[str] = []
        seasons: List[int] = []
        for request in batch:
            who = request.get("requestedBy") or {}
            ids = (await self.seerr.discord_for_user(int(who["id"]))
                   if who.get("id") is not None else [])
            requesters.extend(i for i in ids if i not in requesters)
            name = (await self._server_name(channel, ids[0]) if ids else "") \
                or str(who.get("displayName") or "")
            if name and name not in names:
                names.append(name)
        for request in batch:
            seasons.extend(int(s["seasonNumber"])
                           for s in request.get("seasons") or []
                           if s.get("seasonNumber") is not None)
        # A classic embed rather than a Components V2 card: an embed's
        # thumbnail keeps the poster's own shape (tall), where a V2
        # section's thumbnail is always cropped to a square.
        embed = discord.Embed(title=seerrapi.display_title(summary)[:256],
                              colour=ANNOUNCE_COLOUR)
        embed.set_author(name=requestui.ANNOUNCE_MARK)
        lines = []
        if kind == "tv" and seasons:
            lines.append(f"*{requestui.season_label(sorted(set(seasons)))}*")
        if summary["overview"]:
            lines.append(requestui._esc(requestui._clip(summary["overview"],
                                                        300)))
        if lines:
            embed.description = "\n".join(lines)
        if summary["poster_url"]:
            embed.set_thumbnail(url=summary["poster_url"])
        if names:
            embed.set_footer(text=requestui._clip(
                "Requested by " + ", ".join(names), 2048))
        plex_url = summary.get("plex_url") or next(
            (seerrapi.plex_web_url((r.get("media") or {}).get("mediaUrl"))
             for r in batch if (r.get("media") or {}).get("mediaUrl")), "")
        view = ui.View(timeout=None)
        if plex_url.startswith("http"):
            view.add_item(ui.Button(label="Watch on Plex", emoji="▶️",
                                    url=plex_url,
                                    style=discord.ButtonStyle.link))
        link = await self.seerr.media_link(kind, int(tmdb_id))
        if link.startswith("http"):
            view.add_item(ui.Button(label="Open in Seerr", url=link,
                                    style=discord.ButtonStyle.link))
        await channel.send(embed=embed,
                           view=view if view.children else discord.utils.MISSING,
                           allowed_mentions=discord.AllowedMentions.none())
        logger.info("Announced %s (%s) to #%s", summary["title"],
                    ", ".join(str(r.get("id")) for r in batch),
                    getattr(channel, "name", "?"))
        if self._watch_party_dms():
            await self._dm_requesters(requesters, kind, int(tmdb_id),
                                      summary, plex_url)
        return True

    @staticmethod
    async def _server_name(channel, user_id: int) -> str:
        """The name a requester goes by in this server, as plain text (no
        ping). Empty when they've left or Discord won't say."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return ""
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return ""
        return member.display_name

    def _watch_party_dms(self) -> bool:
        """WATCH_PARTY_DMS in .env (wins) or requests.watch_party_dms."""
        env = os.getenv("WATCH_PARTY_DMS", "").strip().lower()
        if env:
            return env in ("1", "true", "yes", "on")
        return bool(self.config.get("watch_party_dms", False))

    async def _dm_requesters(self, requesters: List[int], kind: str,
                             tmdb_id: int, summary: Dict[str, Any],
                             plex_url: str) -> None:
        """DM each requester a one-press Start watch party button.

        Only for a companion watch party bot signed in with this same bot
        token (WATCH_PARTY_DMS): the button's custom_id,
        `reqgo:<discord id>:<movie|tv>:<tmdb id>:x`, is that bot's to
        answer. It should check the presser is the person named in it.
        """
        title = seerrapi.display_title(summary)
        for user_id in requesters:
            buttons: List[ui.Item] = [ui.Button(
                label="Start watch party", emoji="▶️",
                style=discord.ButtonStyle.primary,
                custom_id=f"reqgo:{int(user_id)}:{kind}:{int(tmdb_id)}:x")]
            if plex_url.startswith("http"):
                buttons.append(ui.Button(label="Watch on Plex", url=plex_url,
                                         style=discord.ButtonStyle.link))
            view = ui.View(timeout=None)
            for button in buttons:
                view.add_item(button)
            # The button belongs to the companion bot; stopped views aren't
            # kept in discord.py's view store.
            view.stop()
            try:
                user = self.bot.get_user(user_id) \
                    or await self.bot.fetch_user(user_id)
                await user.send(
                    f"🍿 **{requestui._esc(title)}** is on Plex now — you "
                    "asked for it.", view=view)
            except discord.HTTPException as exc:
                logger.info("Could not DM %s about %s: %s", user_id, title,
                            exc)

    @tasks.loop(seconds=120)
    async def announce_loop(self) -> None:
        try:
            await self.poll_announcements()
        except SeerrError as exc:
            logger.warning("Announcement poll failed: %s", exc)
        except Exception:
            logger.exception("Announcement poll crashed -- retrying next "
                             "time")
        wanted = self._int("poll_seconds", 30, 3600)
        if self.announce_loop.seconds != wanted:
            self.announce_loop.change_interval(seconds=wanted)

    @announce_loop.before_loop
    async def _before_announce(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Requests(bot))
