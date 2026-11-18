"""Music cards and buttons: albums and artists from Seerr AI.

Seerr AI requests music by album (an artist alone can't be requested), so an
album card carries the Request button and an artist card lists the artist's
albums to pick from. Both are keyed by MusicBrainz IDs, the way Seerr AI is.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord import ui

from util import seerr as seerrapi
from util.requestui import ACCENT, OVERVIEW_CHARS, Card, _clip, _esc

_MB = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog("Requests")


async def _off(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Requests are switched off right now.", ephemeral=True)


class MusicRequestButton(ui.DynamicItem[ui.Button],
                         template=rf"plexmon:reqm:(?P<artist>{_MB}):"
                                  rf"(?P<album>{_MB})"):
    """Request one album. Seerr AI needs the artist's ID with it."""

    def __init__(self, artist_mbid: str, album_mbid: str,
                 label: str = "Request") -> None:
        super().__init__(ui.Button(
            label=_clip(label, 80), style=discord.ButtonStyle.primary,
            emoji="➕",
            custom_id=f"plexmon:reqm:{artist_mbid.lower()}:"
                      f"{album_mbid.lower()}"))
        self.artist_mbid = artist_mbid.lower()
        self.album_mbid = album_mbid.lower()

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["artist"], match["album"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if cog is None:
            await _off(interaction)
            return
        await cog.handle_music_request(interaction, self.artist_mbid,
                                       self.album_mbid)


class AlbumSelect(ui.DynamicItem[ui.Select],
                  template=rf"plexmon:albums:(?P<artist>{_MB})"):
    """An artist card's albums: picking one opens its card."""

    def __init__(self, artist_mbid: str,
                 albums: Sequence[Dict[str, Any]] = ()) -> None:
        options = [discord.SelectOption(
            label=_clip(a["title"] + (f" ({a['year']})" if a.get("year")
                                      else ""), 100),
            description=_clip(" · ".join(x for x in (
                a.get("detail"), a.get("status_label")) if x) or "Album", 100),
            emoji="✅" if a.get("on_plex") else "💿",
            value=a["id"]) for a in list(albums)[:25]] or [
            discord.SelectOption(label="—", value="none")]
        super().__init__(ui.Select(
            custom_id=f"plexmon:albums:{artist_mbid.lower()}",
            placeholder="Pick an album to see or request it",
            options=options))
        self.artist_mbid = artist_mbid.lower()

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["artist"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        values = (interaction.data or {}).get("values") or []
        if cog is None or not values or not seerrapi.is_mbid(values[0]):
            await interaction.response.defer()
            return
        # A new private message, so the artist's list stays open to pick
        # another album from.
        await cog.show_music_card(interaction, "album", values[0])


MUSIC_ITEMS = (MusicRequestButton, AlbumSelect)


def _length(ms_total: int) -> str:
    minutes = round(ms_total / 60000)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 \
        else f"{minutes} min"


def album_items(details: Dict[str, Any], tracks: Sequence[Dict[str, Any]] = (),
                *, seerr_link: str = "", note: str = ""
                ) -> Tuple[List[ui.Item], int]:
    """An album card: cover, artist and year, what's on it, whether it's on
    Plex or already asked for, and a Request button when it can be."""
    summary = seerrapi.summarize_music({**details, "mediaType": "album"}) or {
        "media_type": "album", "id": "", "title": "Unknown album",
        "artist": "", "artist_id": "", "year": "", "detail": "",
        "poster_url": "", "status": seerrapi.STATUS_UNKNOWN,
        "on_plex": False, "adding": False, "plex_url": ""}
    lines = [f"### 💿 {_esc(summary['title'])}"]
    by = summary["artist"] + (f" · {summary['year']}" if summary["year"]
                              else "")
    if by:
        lines.append(f"**{_esc(by)}**")
    facts = [summary["detail"] or "Album"]
    if tracks:
        facts.append(f"{len(tracks)} track{'s' if len(tracks) != 1 else ''}")
        total = sum(int(t.get("length") or 0) for t in tracks)
        if total:
            facts.append(_length(total))
    genres = [str(g) for g in details.get("genres") or [] if g][:3]
    if genres:
        facts.append(", ".join(genres))
    lines.append("-# " + _esc(" · ".join(facts)))

    status = summary["status"]
    buttons: List[ui.Item] = []
    accent = ACCENT["requestable"]
    if status == seerrapi.STATUS_BLOCKLISTED:
        accent = ACCENT["blocked"]
        lines.append("⛔ **Blocklisted** — this one can't be requested.")
    elif status == seerrapi.STATUS_AVAILABLE:
        accent = ACCENT["available"]
        lines.append("✅ **Already on Plex**")
    elif status == seerrapi.STATUS_PARTIAL:
        accent = ACCENT["partial"]
        lines.append("✅ **Partly on Plex**")
    elif status in (seerrapi.STATUS_PENDING, seerrapi.STATUS_PROCESSING):
        accent = ACCENT["requested"]
        lines.append("⏳ **Already requested**" + (
            " — being added to Plex now" if summary.get("adding") else
            " — downloading now" if status == seerrapi.STATUS_PROCESSING
            else ""))
    elif summary["artist_id"] and summary["id"]:
        buttons.append(MusicRequestButton(summary["artist_id"],
                                          summary["id"]))
    if note:
        lines.append(f"-# {note}")
    if summary.get("plex_url"):
        buttons.append(ui.Button(label="Listen on Plex", emoji="🎧",
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
    return children, accent


def _album_order(album: Dict[str, Any]) -> tuple:
    """Studio albums first, newest first; then EPs, singles and the rest."""
    kind = str(album.get("detail") or "").split(" · ")[0]
    rank = {"Album": 0, "EP": 1, "Single": 2}.get(kind, 3)
    extra = " · " in str(album.get("detail") or "")     # live, compilation…
    return (rank + (3 if extra else 0), -int(album.get("year") or 0))


def artist_items(details: Dict[str, Any], albums: Sequence[Dict[str, Any]],
                 *, seerr_link: str = "", note: str = ""
                 ) -> Tuple[List[ui.Item], int]:
    """An artist card: who they are, how much of them is on Plex, and their
    albums to pick from (Seerr AI requests music album by album)."""
    summary = seerrapi.summarize_music({**details, "mediaType": "artist"}) \
        or {"id": "", "title": "Unknown artist", "detail": "",
            "overview": "", "status": seerrapi.STATUS_UNKNOWN}
    ordered = sorted(albums, key=_album_order)
    lines = [f"### 🎤 {_esc(summary['title'])}"]
    facts = [summary["detail"]] if summary["detail"] else []
    genres = [str(g) for g in details.get("genres") or [] if g][:3]
    if genres:
        facts.append(", ".join(genres))
    on_plex = sum(1 for a in albums if a.get("on_plex"))
    if albums:
        facts.append(f"{on_plex} of {len(albums)} on Plex" if on_plex
                     else f"{len(albums)} releases")
    if facts:
        lines.append("-# " + _esc(" · ".join(facts)))
    if summary["overview"]:
        lines.append(_esc(_clip(summary["overview"], OVERVIEW_CHARS)))
    accent = (ACCENT["available"] if albums and on_plex == len(albums)
              else ACCENT["partial"] if on_plex else ACCENT["requestable"])
    if note:
        lines.append(f"-# {note}")
    text = ui.TextDisplay("\n".join(lines))
    cover = next((a["poster_url"] for a in ordered if a.get("poster_url")
                  and a.get("on_plex")), "") or next(
        (a["poster_url"] for a in ordered if a.get("poster_url")), "")
    head: ui.Item = (ui.Section(text, accessory=ui.Thumbnail(cover))
                     if cover else text)
    children: List[ui.Item] = [head]
    if ordered and summary["id"]:
        children.append(ui.ActionRow(AlbumSelect(summary["id"], ordered)))
    if seerr_link.startswith("http"):
        children.append(ui.ActionRow(ui.Button(
            label="Open in Seerr", style=discord.ButtonStyle.link,
            url=seerr_link)))
    return children, accent


def music_card(children: List[ui.Item], accent: int) -> ui.LayoutView:
    view = Card()
    view.add_item(ui.Container(*children, accent_colour=accent))
    return view
