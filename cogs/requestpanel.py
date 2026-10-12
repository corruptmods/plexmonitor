"""Request panel cog -- a permanent "request something" message.

Like the status dashboard, it's one message the bot keeps in a channel of its
own: an embed saying what it's for, with two buttons.

  Request   a popup asks for a title. A clear title shows its card (already
            on Plex, requested, or a Request button and season picks),
            several matches give a picker, and anything that isn't a title
            goes to the AI. This is the only way to request in Discord.
  Ask AI    a popup asks anything ("something like Severance but shorter?",
            "is Dune on Plex?"), answered privately with the assistant.
  Report a problem
            a popup asks which title, then the issue form: won't play,
            sound, subtitles, missing or wrong episodes. It becomes a
            Seerr issue; Seerr AI fixes most of these by itself, and with
            plain Seerr the admins see it.

Everything after a press is private to the person who pressed.

Where: `requests.panel_channel_id` in config.json, or REQUEST_PANEL_CHANNEL_ID
in .env (which wins). The message id is kept in data/request_panel.json. A
deleted panel is posted again, and a changed one (a new build's wording, the
AI switched on or off) is edited in place, checked every few minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

import discord
from discord import ui
from discord.ext import commands, tasks

from util import requestui
from util.jsonfile import data_dir, read_json, write_json

logger = logging.getLogger("plexmonitor.requestpanel")

CHECK_MINUTES = 5
ACCENT = 0xE5A00D  # Plex's orange
REQUEST_ID = "plexmon:panel:request"
ASK_ID = "plexmon:panel:ask"
REPORT_ID = "plexmon:panel:report"


class RequestPanelButton(ui.DynamicItem[ui.Button],
                         template=r"plexmon:panel:request"):
    def __init__(self) -> None:
        super().__init__(ui.Button(label="Request", emoji="🎬",
                                   style=discord.ButtonStyle.primary,
                                   custom_id=REQUEST_ID))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        requests = interaction.client.get_cog("Requests")
        if requests is None:
            await interaction.response.send_message(
                "Requests are switched off right now.", ephemeral=True)
            return
        await interaction.response.send_modal(RequestModal(requests))


class AskPanelButton(ui.DynamicItem[ui.Button], template=r"plexmon:panel:ask"):
    def __init__(self) -> None:
        super().__init__(ui.Button(label="Ask AI", emoji="✨",
                                   style=discord.ButtonStyle.secondary,
                                   custom_id=ASK_ID))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        assistant = interaction.client.get_cog("Assistant")
        if assistant is None or not assistant._ready():
            await interaction.response.send_message(
                "The assistant isn't available right now.", ephemeral=True)
            return
        await interaction.response.send_modal(AskModal(assistant))


class ReportPanelButton(ui.DynamicItem[ui.Button],
                        template=r"plexmon:panel:report"):
    def __init__(self) -> None:
        super().__init__(ui.Button(label="Report a problem", emoji="🛠️",
                                   style=discord.ButtonStyle.secondary,
                                   custom_id=REPORT_ID))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        requests = interaction.client.get_cog("Requests")
        if requests is None:
            await interaction.response.send_message(
                "Reporting is switched off right now.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(requests))


class ReportModal(ui.Modal, title="Report a problem"):
    query = ui.TextInput(label="Which movie or show?",
                         placeholder="e.g. Severance", max_length=200)

    def __init__(self, requests) -> None:
        super().__init__(timeout=600)
        self.requests = requests

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.requests.report(interaction, str(self.query.value))

    async def on_error(self, interaction, error) -> None:
        logger.exception("Report popup failed", exc_info=error)
        await _sorry(interaction)


class RequestModal(ui.Modal, title="Request a movie or show"):
    query = ui.TextInput(label="Title",
                         placeholder="e.g. Dune: Part Two, or Severance",
                         max_length=200)

    def __init__(self, requests) -> None:
        super().__init__(timeout=600)
        self.requests = requests

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.requests.route(interaction, str(self.query.value))

    async def on_error(self, interaction, error) -> None:
        logger.exception("Request popup failed", exc_info=error)
        await _sorry(interaction)


class AskModal(ui.Modal, title="Ask the assistant"):
    question = ui.TextInput(
        label="Your question", style=discord.TextStyle.paragraph,
        placeholder="Something like Severance but shorter? Is Dune on Plex?",
        max_length=500)

    def __init__(self, assistant) -> None:
        super().__init__(timeout=900)
        self.assistant = assistant

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.assistant.private_answer(
            interaction, str(self.question.value), fresh=True)

    async def on_error(self, interaction, error) -> None:
        logger.exception("Ask popup failed", exc_info=error)
        await _sorry(interaction)


_sorry = requestui.sorry


PANEL_ITEMS = (RequestPanelButton, AskPanelButton, ReportPanelButton)


class RequestPanel(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.state_file = os.path.join(data_dir(), "request_panel.json")
        stored = read_json(self.state_file, {})
        self.state: Dict[str, Any] = stored if isinstance(stored, dict) else {}
        self._warned: Optional[int] = None
        # Seerr AI fixes most reported problems by itself; plain Seerr
        # leaves them to the admins. The panel says which.
        self._self_fixing = False

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*PANEL_ITEMS)
        self.panel_loop.start()

    async def cog_unload(self) -> None:
        self.panel_loop.cancel()
        try:
            self.bot.remove_dynamic_items(*PANEL_ITEMS)
        except Exception:
            logger.debug("remove_dynamic_items failed", exc_info=True)

    # ---------------------------------------------------------------- content

    def _channel_id(self) -> int:
        requests = self.bot.get_cog("Requests")
        if requests is None:
            return 0
        requests._reload_config()
        return requests.channel_setting("panel_channel_id",
                                        "REQUEST_PANEL_CHANNEL_ID")

    def _ai_on(self) -> bool:
        assistant = self.bot.get_cog("Assistant")
        return bool(assistant is not None and assistant._ready())

    def panel(self) -> tuple:
        """(embed, view) for the panel as it should look right now."""
        ai = self._ai_on()
        lines = [
            "Press **Request** and type a title. You'll see straight away "
            "whether it's already on Plex, and can request it in one tap — "
            "a whole show, or just the seasons you want.",
        ]
        if ai:
            lines.append(
                "\nNot sure what you want? Press **Ask AI** and ask in your "
                "own words: *\"something like Severance but shorter\"*, "
                "*\"is Dune on Plex?\"*.")
        lines.append(
            "\nSomething wrong with what's on Plex — won't play, no "
            "subtitles, missing or wrong episodes? Press **Report a "
            "problem**." + (" It's usually fixed on its own, without "
                            "waiting for an admin." if self._self_fixing
                            else " The admins will take a look."))
        lines.append("\n-# Only you see what happens after you press.")
        requests = self.bot.get_cog("Requests")
        announce = (requests.channel_setting("announce_channel_id",
                                             "ANNOUNCE_CHANNEL_ID")
                    if requests is not None else 0)
        if announce:
            lines.append(f"-# New arrivals are announced in <#{announce}>.")
        embed = discord.Embed(title="🎬 Request a movie or show",
                              description="\n".join(lines), colour=ACCENT)
        embed.set_footer(text="Your Discord needs linking to Seerr once — "
                              "you'll be shown how the first time.")
        view = ui.View(timeout=None)
        view.add_item(RequestPanelButton())
        if ai:
            view.add_item(AskPanelButton())
        view.add_item(ReportPanelButton())
        return embed, view

    @staticmethod
    def fingerprint(embed: discord.Embed, view: ui.View) -> str:
        buttons = [getattr(item, "custom_id", "") for item in view.children]
        payload = json.dumps([embed.to_dict(), buttons], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _save(self, **changes) -> None:
        self.state.update(changes)
        write_json(self.state_file, self.state)

    # ------------------------------------------------------------------ keep

    async def ensure(self) -> Optional[discord.Message]:
        """Post, move or update the panel so exactly one current copy is up.
        Returns the panel message, or None when there's nowhere to post."""
        channel_id = self._channel_id()
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            if self._warned != channel_id:
                self._warned = channel_id
                logger.warning("Request panel channel %s not found (or the "
                               "bot can't see it)", channel_id)
            return None
        requests = self.bot.get_cog("Requests")
        if requests is not None and requests.seerr.enabled:
            self._self_fixing = await requests.seerr.check_ai() is True
        embed, view = self.panel()
        wanted = self.fingerprint(embed, view)

        message = None
        stored_channel = int(self.state.get("channel_id") or 0)
        stored_message = int(self.state.get("message_id") or 0)
        if stored_message and stored_channel and stored_channel != channel_id:
            # Moved to another channel: take the old one down.
            old_channel = self.bot.get_channel(stored_channel)
            if old_channel is not None:
                try:
                    old = await old_channel.fetch_message(stored_message)
                    await old.delete()
                except discord.HTTPException:
                    pass
            stored_message = 0
        if stored_message:
            try:
                message = await channel.fetch_message(stored_message)
            except discord.NotFound:
                message = None
            except discord.HTTPException as exc:
                logger.warning("Could not read the request panel: %s", exc)
                return None
            if message is not None and message.author.id != self.bot.user.id:
                message = None

        if message is None:
            message = await channel.send(embed=embed, view=view)
            logger.info("Posted the request panel in #%s",
                        getattr(channel, "name", channel_id))
            try:
                await message.pin()
            except discord.HTTPException:
                pass  # needs Manage Messages; the panel works unpinned
            self._save(channel_id=channel_id, message_id=message.id,
                       version=wanted)
        elif self.state.get("version") != wanted:
            await message.edit(embed=embed, view=view)
            logger.info("Updated the request panel")
            self._save(channel_id=channel_id, message_id=message.id,
                       version=wanted)
        return message

    @tasks.loop(minutes=CHECK_MINUTES)
    async def panel_loop(self) -> None:
        try:
            await self.ensure()
        except discord.Forbidden as exc:
            logger.warning(
                "Request panel: the bot isn't allowed in channel %s (%s). "
                "Give it View Channel, Read Message History, Send Messages "
                "and Embed Links there (Manage Messages to pin). Retrying "
                "every %d min.", self._channel_id(), exc.text or exc,
                CHECK_MINUTES)
        except discord.HTTPException as exc:
            logger.warning("Request panel check failed: %s", exc)
        except Exception:
            logger.exception("Request panel check crashed -- retrying later")

    @panel_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RequestPanel(bot))
