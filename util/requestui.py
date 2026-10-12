"""Components V2 cards for requesting media, shared by the request panel
and the AI.

The buttons and selects are DynamicItems: their custom_id carries what to
request ("plexmon:req:tv:1399:1,2"), so a press works on any message --
an ephemeral request card, an AI answer, a card posted days ago -- and
survives restarts. Whoever presses is who the request is filed for; the
handler looks their Seerr account up by Discord ID.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord import ui

from util import seerr as seerrapi

logger = logging.getLogger("plexmonitor.requestui")

ACCENT = {
    "available": 0x23A55A,
    "partial": 0x3BA55C,
    "requested": 0xF0B232,
    "requestable": 0x5865F2,
    "blocked": 0xF23F43,
}
OVERVIEW_CHARS = 320


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(text: Any) -> str:
    return discord.utils.escape_markdown(str(text or ""))


def season_label(numbers: Sequence[int]) -> str:
    nums = sorted({int(n) for n in numbers})
    if not nums:
        return ""
    if len(nums) == 1:
        return "Specials" if nums[0] == 0 else f"Season {nums[0]}"
    if nums == list(range(nums[0], nums[-1] + 1)):
        return f"Seasons {nums[0]}–{nums[-1]}"
    return "Seasons " + ", ".join(str(n) for n in nums)


def season_state(details: Dict[str, Any], specials: bool = False
                 ) -> Dict[str, Any]:
    """What a TV request can still ask for, mirroring Seerr's own rules in
    MediaRequest.createRequest: a season is taken when Seerr already has
    it (any status but UNKNOWN/DELETED) or an active request covers it."""
    info = details.get("mediaInfo") or {}
    seasons = []
    for season in details.get("seasons") or []:
        try:
            number = int(season.get("seasonNumber"))
            episodes = int(season.get("episodeCount") or 0)
        except (TypeError, ValueError):
            continue
        if episodes <= 0 or (number == 0 and not specials):
            continue
        seasons.append(number)
    have = set()
    for season in info.get("seasons") or []:
        status = season.get("status")
        if status not in (None, seerrapi.STATUS_UNKNOWN,
                          seerrapi.STATUS_DELETED):
            have.add(int(season.get("seasonNumber", -1)))
    asked = set()
    for request in info.get("requests") or []:
        if request.get("is4k"):
            continue
        if request.get("status") in (seerrapi.REQUEST_DECLINED,
                                     seerrapi.REQUEST_COMPLETED):
            continue
        for season in request.get("seasons") or []:
            asked.add(int(season.get("seasonNumber", -1)))
    open_ = [n for n in seasons if n not in have and n not in asked]
    return {"all": seasons, "have": sorted(have & set(seasons)),
            "asked": sorted(asked & set(seasons)), "open": open_}


def requesters(details: Dict[str, Any]) -> List[str]:
    info = details.get("mediaInfo") or {}
    names = []
    for request in info.get("requests") or []:
        if request.get("status") in (seerrapi.REQUEST_DECLINED,):
            continue
        who = (request.get("requestedBy") or {}).get("displayName")
        if who and who not in names:
            names.append(str(who))
    return names


# ------------------------------------------------------------ interactive

class RequestButton(ui.DynamicItem[ui.Button],
                    template=r"plexmon:req:(?P<kind>movie|tv):(?P<tmdb>\d+)"
                             r"(?::(?P<seasons>[\d,]+))?"):
    def __init__(self, kind: str, tmdb_id: int,
                 seasons: Optional[Sequence[int]] = None,
                 label: str = "Request",
                 style: discord.ButtonStyle = discord.ButtonStyle.primary
                 ) -> None:
        suffix = (":" + ",".join(str(int(s)) for s in seasons)) \
            if seasons else ""
        custom_id = f"plexmon:req:{kind}:{int(tmdb_id)}{suffix}"
        if len(custom_id) > 100:
            # Never truncate: a clipped season list would request the
            # wrong seasons. Too many to encode means "all remaining".
            custom_id, seasons = f"plexmon:req:{kind}:{int(tmdb_id)}", None
        super().__init__(ui.Button(
            label=_clip(label, 80), style=style, emoji="➕", custom_id=custom_id))
        self.kind = kind
        self.tmdb_id = int(tmdb_id)
        self.seasons = [int(s) for s in seasons] if seasons else []
        self.label = _clip(label, 80)
        self.style = style

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        seasons = [int(s) for s in (match["seasons"] or "").split(",") if s]
        return cls(match["kind"], int(match["tmdb"]), seasons)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Requests")
        if cog is None:
            await interaction.response.send_message(
                "Requests are switched off right now.", ephemeral=True)
            return
        await cog.handle_request(interaction, self.kind, self.tmdb_id,
                                 self.seasons)


class SeasonSelect(ui.DynamicItem[ui.Select],
                   template=r"plexmon:seasons:(?P<tmdb>\d+)"):
    def __init__(self, tmdb_id: int,
                 open_seasons: Sequence[int] = ()) -> None:
        options = [discord.SelectOption(label=season_label([n]),
                                        value=str(n))
                   for n in list(open_seasons)[:25]] or [
            discord.SelectOption(label="Season", value="1")]
        super().__init__(ui.Select(
            custom_id=f"plexmon:seasons:{int(tmdb_id)}",
            placeholder="…or pick seasons", min_values=1,
            max_values=len(options), options=options))
        self.tmdb_id = int(tmdb_id)
        self.open_seasons = list(open_seasons)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tmdb"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Requests")
        if cog is None:
            await interaction.response.send_message(
                "Requests are switched off right now.", ephemeral=True)
            return
        values = (interaction.data or {}).get("values") or []
        seasons = [int(v) for v in values if str(v).isdigit()]
        await cog.handle_request(interaction, "tv", self.tmdb_id, seasons)


class PickSelect(ui.DynamicItem[ui.Select],
                 template=r"plexmon:pick:(?P<purpose>req)"):
    """Disambiguation when a typed title matches several things."""

    def __init__(self, results: Sequence[Dict[str, Any]] = ()) -> None:
        options = [discord.SelectOption(
            label=_clip(seerrapi.display_title(r), 100),
            description=_clip(("Movie" if r["media_type"] == "movie"
                               else "TV show")
                              + (f" · {r['status_label']}"
                                 if r.get("status_label") else ""), 100),
            emoji="🎬" if r["media_type"] == "movie" else "📺",
            value=f"{r['media_type']}:{r['tmdb_id']}")
            for r in list(results)[:25]] or [
            discord.SelectOption(label="—", value="movie:0")]
        super().__init__(ui.Select(custom_id="plexmon:pick:req",
                                   placeholder="Which one did you mean?",
                                   options=options))
        self.results = list(results)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Requests")
        values = (interaction.data or {}).get("values") or []
        if cog is None or not values:
            await interaction.response.defer()
            return
        kind, _, tmdb = str(values[0]).partition(":")
        if kind in ("movie", "tv") and tmdb.isdigit():
            await cog.show_card(interaction, kind, int(tmdb), edit=True)


DYNAMIC_ITEMS = (RequestButton, SeasonSelect, PickSelect)


# ---------------------------------------------------------------- builders

class Card(ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)


def media_card(details: Dict[str, Any], kind: str, *,
               partial_requests: bool = True, specials: bool = False,
               seerr_link: str = "", note: str = "") -> ui.LayoutView:
    """The full card a request shows: poster, what it is, whether it is on
    Plex or already asked for, and exactly the actions still possible."""
    children, accent = media_items(
        details, kind, partial_requests=partial_requests, specials=specials,
        seerr_link=seerr_link, note=note)
    view = Card()
    view.add_item(ui.Container(*children, accent_colour=accent))
    return view


def media_items(details: Dict[str, Any], kind: str, *,
                partial_requests: bool = True, specials: bool = False,
                seerr_link: str = "", note: str = ""
                ) -> Tuple[List[ui.Item], int]:
    """media_card's contents and accent colour, for pages that hold a
    title card among other things (the AI's suggestions)."""
    details = dict(details)
    details.setdefault("mediaType", kind)
    summary = seerrapi.summarize(details) or {
        "media_type": kind, "tmdb_id": int(details.get("id") or 0),
        "title": "Unknown", "year": "", "overview": "", "poster_url": "",
        "status": seerrapi.STATUS_UNKNOWN, "status_label": "",
        "plex_url": ""}
    status = summary["status"]
    lines = [f"### {'🎬' if kind == 'movie' else '📺'} "
             f"{_esc(seerrapi.display_title(summary))}"]
    facts = ["Movie" if kind == "movie" else "TV show"]
    runtime = details.get("runtime")
    if kind == "movie" and runtime:
        facts.append(f"{int(runtime) // 60}h {int(runtime) % 60:02d}m"
                     if int(runtime) >= 60 else f"{int(runtime)}m")
    if kind == "tv" and details.get("numberOfSeasons"):
        n = int(details["numberOfSeasons"])
        facts.append(f"{n} season{'s' if n != 1 else ''}")
    rating = details.get("voteAverage")
    if rating:
        try:
            facts.append(f"⭐ {float(rating):.1f}")
        except (TypeError, ValueError):
            pass
    lines.append("-# " + " · ".join(facts))
    if summary["overview"]:
        lines.append(_esc(_clip(summary["overview"], OVERVIEW_CHARS)))

    buttons: List[ui.Item] = []
    extra_rows: List[ui.ActionRow] = []
    accent = ACCENT["requestable"]
    state_line = ""
    if status == seerrapi.STATUS_BLOCKLISTED:
        accent = ACCENT["blocked"]
        state_line = "⛔ **Blocklisted** — this one can't be requested."
    elif kind == "movie":
        if status == seerrapi.STATUS_AVAILABLE:
            accent = ACCENT["available"]
            state_line = "✅ **Already on Plex**"
        elif status in (seerrapi.STATUS_PENDING, seerrapi.STATUS_PROCESSING):
            accent = ACCENT["requested"]
            who = requesters(details)
            state_line = ("⏳ **Already requested**"
                          + (f" by {_esc(', '.join(who))}" if who else "")
                          + (" — downloading now"
                             if status == seerrapi.STATUS_PROCESSING else ""))
        else:
            buttons.append(RequestButton("movie", summary["tmdb_id"]))
    else:
        seasons = season_state(details, specials)
        if not seasons["open"]:
            if status == seerrapi.STATUS_AVAILABLE or (
                    seasons["have"] and not seasons["asked"]):
                accent = ACCENT["available"]
                state_line = "✅ **Already on Plex**"
            else:
                accent = ACCENT["requested"]
                who = requesters(details)
                state_line = ("⏳ **Every season is already requested**"
                              + (f" (by {_esc(', '.join(who))})"
                                 if who else ""))
        else:
            if seasons["have"]:
                accent = ACCENT["partial"]
                state_line = (f"✅ On Plex: {season_label(seasons['have'])}")
            if seasons["asked"]:
                state_line += ("\n" if state_line else "") + \
                    f"⏳ Requested: {season_label(seasons['asked'])}"
            label = ("Request all" if not seasons["have"]
                     and not seasons["asked"] else "Request the rest")
            label += f" ({season_label(seasons['open'])})"
            buttons.append(RequestButton("tv", summary["tmdb_id"],
                                         label=label))
            if partial_requests and len(seasons["open"]) > 1:
                extra_rows.append(ui.ActionRow(
                    SeasonSelect(summary["tmdb_id"], seasons["open"])))
    if state_line:
        lines.append(state_line)
    if note:
        lines.append(f"-# {note}")

    if summary.get("plex_url"):
        buttons.append(ui.Button(label="Watch on Plex", emoji="▶️",
                                 style=discord.ButtonStyle.link,
                                 url=summary["plex_url"]))
    if seerr_link.startswith("http"):
        buttons.append(ui.Button(label="Open in Seerr",
                                 style=discord.ButtonStyle.link,
                                 url=seerr_link))

    text = ui.TextDisplay("\n".join(lines))
    head: ui.Item = (ui.Section(text, accessory=ui.Thumbnail(
        summary["poster_url"])) if summary["poster_url"] else text)
    children: List[ui.Item] = [head]
    if buttons:
        children.append(ui.ActionRow(*buttons[:5]))
    children.extend(extra_rows)
    return children, accent


def picker(results: Sequence[Dict[str, Any]], query: str) -> ui.LayoutView:
    view = Card()
    view.add_item(ui.Container(
        ui.TextDisplay(f"### A few things match “{_esc(_clip(query, 80))}”\n"
                       "-# Pick one to see its details."),
        ui.ActionRow(PickSelect(results)),
        accent_colour=ACCENT["requestable"]))
    return view


# ------------------------------------------------------------- fallbacks

def plain_version(view: ui.LayoutView) -> Tuple[str, Optional[ui.View]]:
    """The same card as message text plus a classic button row.

    Only used when Discord refuses a Components V2 payload for an
    interaction follow-up, so a person still gets their answer (and their
    Request button) rather than an error.
    """
    lines: List[str] = []
    rebuilt: List[ui.Item] = []
    for item in view.walk_children():
        if isinstance(item, ui.TextDisplay):
            lines.append(item.content)
        elif isinstance(item, RequestButton):
            rebuilt.append(RequestButton(item.kind, item.tmdb_id,
                                         item.seasons or None,
                                         label=item.label, style=item.style))
        elif isinstance(item, SeasonSelect):
            rebuilt.append(SeasonSelect(item.tmdb_id, item.open_seasons))
        elif isinstance(item, PickSelect):
            rebuilt.append(PickSelect(item.results))
        elif isinstance(item, ui.Button) and item.url:
            rebuilt.append(ui.Button(label=item.label, url=item.url,
                                     emoji=item.emoji,
                                     style=discord.ButtonStyle.link))
    text = "\n".join(lines)
    if len(text) > 2000:
        text = text[:1999] + "…"
    classic = None
    if rebuilt:
        classic = ui.View(timeout=None)
        for item in rebuilt[:25]:
            try:
                classic.add_item(item)
            except ValueError:
                break
    return text or "\u200b", classic


async def sorry(interaction: discord.Interaction,
                text: str = "Something went wrong — try again.") -> None:
    """Tell the presser something failed, whether or not the interaction
    was already answered (a deferred "thinking…" would otherwise spin)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


def answers_errors(what: str):
    """Decorator for a cog method taking (self, interaction, ...): an
    unexpected exception is logged and the person is told, instead of
    leaving them on "thinking…"."""
    import functools

    def wrap(func):
        @functools.wraps(func)
        async def run(self, interaction, *args, **kwargs):
            try:
                return await func(self, interaction, *args, **kwargs)
            except Exception:
                logger.exception("%s failed", what)
                await sorry(interaction)
        return run
    return wrap


async def followup(interaction: discord.Interaction, view: ui.LayoutView,
                   ephemeral: bool = True, wait: bool = False):
    mentions = discord.AllowedMentions.none()
    try:
        return await interaction.followup.send(
            view=view, ephemeral=ephemeral, wait=wait,
            allowed_mentions=mentions)
    except discord.HTTPException as exc:
        if getattr(exc, "status", 0) != 400:
            raise
        logger.warning("Discord refused a Components V2 follow-up (%s) — "
                       "sending the plain version", exc)
        text, classic = plain_version(view)
        kwargs: Dict[str, Any] = {"ephemeral": ephemeral, "wait": wait,
                                  "allowed_mentions": mentions}
        if classic is not None:
            kwargs["view"] = classic
        return await interaction.followup.send(text, **kwargs)


async def edit_original(interaction: discord.Interaction,
                        view: ui.LayoutView) -> None:
    try:
        await interaction.edit_original_response(view=view)
    except discord.HTTPException as exc:
        if getattr(exc, "status", 0) != 400:
            raise
        text, classic = plain_version(view)
        await interaction.edit_original_response(content=text, view=classic)
