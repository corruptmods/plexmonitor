"""Paging through the titles an AI answer suggests: one title card at a
time, with ◀ n / N ▶ under it.

Stateless, so it keeps working across restarts: the ◀/▶ buttons carry the
whole list in their custom_id ("m693134.t95396..." -- m = movie, t = TV;
a = album and r = artist, whose MusicBrainz IDs are packed into 22 characters)
and the page to show. The answer text is read back from the message itself
(its text blocks carry fixed component ids), and the title card is fetched
fresh from Seerr on each flip, so its status and buttons are current.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from discord import ui

logger = logging.getLogger("plexmonitor.aipager")

PREFIX = "plexmon:ai:pg"
# Room left for the list in a 100-character custom_id.
KEYS_ROOM = 100 - len(f"{PREFIX}:n:99:")

# Component ids of the answer's text blocks, read back when paging.
ASKED_ID = 9001
TEXT_ID = 9002
FOOTER_ID = 9003
ALSO_ID = 9004


_MUSIC_KEYS = {"album": "a", "artist": "r"}


def key_of(pick: Dict[str, Any]) -> str:
    kind = pick["media_type"]
    if kind in _MUSIC_KEYS:
        raw = uuid.UUID(str(pick["id"])).bytes
        return _MUSIC_KEYS[kind] + base64.urlsafe_b64encode(raw).decode() \
            .rstrip("=")
    return ("m" if kind == "movie" else "t") + str(int(pick["tmdb_id"]))


def encode(picks: Sequence[Dict[str, Any]]) -> List[str]:
    """Keys for as many picks as fit in one custom_id, in order."""
    keys: List[str] = []
    for pick in picks:
        key = key_of(pick)
        if len(".".join(keys + [key])) > KEYS_ROOM:
            break
        keys.append(key)
    return keys


def decode(key: str) -> Tuple[str, Any]:
    """(kind, id): a TMDB number for films and series, a MusicBrainz ID
    string for albums and artists."""
    if key[0] in ("a", "r"):
        raw = base64.urlsafe_b64decode(key[1:] + "==")
        return ("album" if key[0] == "a" else "artist"), \
            str(uuid.UUID(bytes=raw))
    return ("movie" if key[0] == "m" else "tv"), int(key[1:])


class PageButton(ui.DynamicItem[ui.Button],
                 template=r"plexmon:ai:pg:(?P<dir>[pn]):(?P<page>\d+)"
                          r":(?P<keys>[mtar][\w.-]*)"):
    """◀ or ▶. `page` is where it goes."""

    def __init__(self, direction: str, page: int, keys: Sequence[str]
                 ) -> None:
        super().__init__(ui.Button(
            emoji="◀️" if direction == "p" else "▶️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{PREFIX}:{direction}:{int(page)}:{'.'.join(keys)}"))
        self.page = int(page)
        self.keys = list(keys)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        keys = [k for k in match["keys"].split(".")
                if re.fullmatch(r"[mt]\d+|[ar][A-Za-z0-9_-]{22}", k)]
        return cls(match["dir"], int(match["page"]), keys)

    async def callback(self, interaction: discord.Interaction) -> None:
        assistant = interaction.client.get_cog("Assistant")
        requests = interaction.client.get_cog("Requests")
        if assistant is None or requests is None or not self.keys \
                or not 0 <= self.page < len(self.keys):
            await interaction.response.send_message(
                "These suggestions can't be browsed right now.",
                ephemeral=True)
            return
        await interaction.response.defer()
        await assistant.turn_page(interaction, self.keys, self.page)


def nav_row(keys: Sequence[str], page: int) -> Optional[ui.ActionRow]:
    """◀ n / N ▶, wrapping round; None for a single title."""
    if len(keys) < 2:
        return None
    count = len(keys)
    position = ui.Button(label=f"{page + 1} / {count}", disabled=True,
                         style=discord.ButtonStyle.secondary,
                         custom_id=f"{PREFIX}:at")
    return ui.ActionRow(PageButton("p", (page - 1) % count, keys), position,
                        PageButton("n", (page + 1) % count, keys))


def walk(components: Iterable[Any]) -> Iterable[Any]:
    """Every component in a message, nested ones included."""
    for component in components or []:
        yield component
        yield from walk(getattr(component, "children", None) or [])
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            yield from walk([accessory])


def texts_by_id(message: Optional[discord.Message]) -> Dict[int, str]:
    found: Dict[int, str] = {}
    for component in walk(getattr(message, "components", None) or []):
        ident = getattr(component, "id", None)
        content = getattr(component, "content", None)
        if ident in (ASKED_ID, TEXT_ID, FOOTER_ID, ALSO_ID) \
                and isinstance(content, str):
            found[ident] = content
    return found


def custom_ids(message: Optional[discord.Message]) -> List[str]:
    return [c.custom_id for c in
            walk(getattr(message, "components", None) or [])
            if isinstance(getattr(c, "custom_id", None), str)]
