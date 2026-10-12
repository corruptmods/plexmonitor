"""Linking a Discord account to a Seerr account, from Discord.

Offered wherever the bot meets someone Seerr doesn't know yet: a Request
button, /issue, or the AI (including just asking it "link my account").

Two ways, neither of which lets anyone claim someone else's account:

  1. Do it yourself: add your Discord ID in Seerr (profile -> Settings ->
     Notifications -> Discord), then press "Check again". Only someone
     signed in to that Seerr account can do this -- the proof is Seerr's.
  2. Ask an admin: name your Seerr account; the bot admins get Approve /
     Deny buttons, and only they can press them. Approved links live in
     data/seerr_links.json, like /link.

The handlers live on the Requests cog, which owns the Seerr client.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import ui

logger = logging.getLogger("plexmonitor.linkui")

ACCENT = 0x5865F2


def _requests(interaction: discord.Interaction):
    return interaction.client.get_cog("Requests")


async def _off(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Linking isn't available right now.", ephemeral=True)


class LinkStartButton(ui.DynamicItem[ui.Button],
                      template=r"plexmon:link:start"):
    """Shown under an AI answer to someone who isn't linked."""

    def __init__(self) -> None:
        super().__init__(ui.Button(label="Link my Seerr account", emoji="🔗",
                                   style=discord.ButtonStyle.secondary,
                                   custom_id="plexmon:link:start"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _requests(interaction)
        if cog is None:
            await _off(interaction)
            return
        await cog.start_link(interaction)


class LinkCheckButton(ui.DynamicItem[ui.Button],
                      template=r"plexmon:link:check"):
    def __init__(self) -> None:
        super().__init__(ui.Button(label="I added it — check again",
                                   emoji="🔄",
                                   style=discord.ButtonStyle.primary,
                                   custom_id="plexmon:link:check"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _requests(interaction)
        if cog is None:
            await _off(interaction)
            return
        await cog.check_link(interaction)


class LinkAskButton(ui.DynamicItem[ui.Button], template=r"plexmon:link:ask"):
    def __init__(self) -> None:
        super().__init__(ui.Button(label="Ask an admin instead", emoji="🙋",
                                   style=discord.ButtonStyle.secondary,
                                   custom_id="plexmon:link:ask"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _requests(interaction)
        if cog is None:
            await _off(interaction)
            return
        await interaction.response.send_modal(AccountModal(cog))


class LinkDecisionButton(
        ui.DynamicItem[ui.Button],
        template=r"plexmon:link:(?P<verdict>ok|no):(?P<discord>\d+)"
                 r":(?P<seerr>\d+)"):
    """Approve / Deny on the request the admins receive."""

    def __init__(self, verdict: str, discord_id: int, seerr_id: int) -> None:
        approve = verdict == "ok"
        super().__init__(ui.Button(
            label="Approve" if approve else "Deny",
            emoji="✅" if approve else "✖️",
            style=discord.ButtonStyle.success if approve
            else discord.ButtonStyle.secondary,
            custom_id=f"plexmon:link:{verdict}:{int(discord_id)}"
                      f":{int(seerr_id)}"))
        self.approve = approve
        self.discord_id = int(discord_id)
        self.seerr_id = int(seerr_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["verdict"], int(match["discord"]),
                   int(match["seerr"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _requests(interaction)
        if cog is None:
            await _off(interaction)
            return
        await cog.decide_link(interaction, self.discord_id, self.seerr_id,
                              self.approve)


class AccountModal(ui.Modal, title="Which Seerr account is yours?"):
    account = ui.TextInput(label="Your Seerr or Plex username",
                           max_length=100,
                           placeholder="As it shows in Seerr")

    def __init__(self, cog) -> None:
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.ask_admin_link(interaction, str(self.account.value))

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception) -> None:
        logger.exception("Link request form failed", exc_info=error)
        from util.requestui import sorry
        await sorry(interaction)


def prompt_view(discord_id: int, settings_url: str,
                note: Optional[str] = None) -> ui.LayoutView:
    """How to link: the do-it-yourself steps, and the admin route."""
    from util.requestui import Card
    text = ("### 🔗 Link your Seerr account\n"
            + (note + "\n" if note else "")
            + "Then your requests, issue reports and AI answers use your own "
              "Seerr account and watch history.\n\n"
              "**Quickest:** in Seerr open **your profile → Settings → "
              "Notifications → Discord**, add this Discord ID, save, and "
              "press **check again**:")
    buttons = []
    if settings_url.startswith("http"):
        buttons.append(ui.Button(label="Open Seerr", emoji="↗️",
                                 url=settings_url,
                                 style=discord.ButtonStyle.link))
    buttons += [LinkCheckButton(), LinkAskButton()]
    view = Card()
    view.add_item(ui.Container(
        ui.TextDisplay(text),
        ui.TextDisplay(f"```\n{int(discord_id)}\n```"),
        ui.ActionRow(*buttons),
        ui.TextDisplay("-# No access to Seerr's settings? **Ask an admin** "
                       "and they'll approve it."),
        accent_colour=ACCENT))
    return view


LINK_ITEMS = (LinkStartButton, LinkCheckButton, LinkAskButton,
              LinkDecisionButton)
