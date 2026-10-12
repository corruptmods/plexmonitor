"""/emojis -- list every emoji the bot can use, with copy-pasteable tags.

Exists because of a Discord quirk: a user without Nitro cannot *send* an
animated emoji, so the usual "type \\:name: and read the tag" trick gets
blocked by Clyde. The Nitro gate applies to user accounts, not bots -- this
bot can render animated emoji perfectly well -- so the simplest fix is to
have the bot print the tags itself.

The reply shows each emoji rendered next to the exact string to paste into
data/config.json, which doubles as proof that the bot really can display it.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

logger = logging.getLogger("plexmonitor.emojis")

# Discord rejects a message body over 2000 characters.
MESSAGE_LIMIT = 1900


class Emojis(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @discord.app_commands.command(
        name="emojis",
        description="List this server's emoji with the tags to paste into config.json",
    )
    @discord.app_commands.describe(
        animated_only="Show only animated emoji (the ones for the status dots)"
    )
    async def emojis(self, interaction: discord.Interaction,
                     animated_only: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this inside a server.")
            return

        emojis = sorted(guild.emojis, key=lambda e: (not e.animated, e.name.lower()))
        if animated_only:
            emojis = [e for e in emojis if e.animated]

        if not emojis:
            await interaction.followup.send(
                "This server has no custom emoji yet. Upload them under "
                "**Server Settings → Emoji**, then run this again."
            )
            return

        # str(emoji) yields "<a:name:id>" for animated and "<:name:id>"
        # otherwise -- exactly what config.json needs.
        lines = []
        for emoji in emojis:
            tag = str(emoji)
            kind = "animated" if emoji.animated else "static"
            lines.append(f"{tag} `{tag}`  ({kind})")

        header = (
            f"**{len(emojis)} emoji in {guild.name}**\n"
            "Each one is rendered on the left; copy the code beside it into "
            "`data/config.json` under `emoji`.\n\n"
        )

        body = ""
        shown = 0
        for line in lines:
            if len(header) + len(body) + len(line) + 1 > MESSAGE_LIMIT:
                break
            body += line + "\n"
            shown += 1
        if shown < len(emojis):
            body += f"\n*…and {len(emojis) - shown} more. "
            body += "Use `/emojis animated_only:True` to narrow the list.*"

        await interaction.followup.send(header + body)

    @discord.app_commands.command(
        name="statusdots",
        description="Show the config snippet for your animated status dots",
    )
    async def statusdots(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this inside a server.")
            return

        def find(*names):
            for name in names:
                for emoji in guild.emojis:
                    if emoji.name.lower() == name:
                        return emoji
            return None

        green = find("greendot", "green_dot", "online", "up")
        red = find("reddot", "red_dot", "offline", "down")

        if not green and not red:
            await interaction.followup.send(
                "Could not find emoji named `greendot` or `reddot` in this server.\n"
                "Upload them under **Server Settings → Emoji** with those exact names, "
                "then run this again. Or use `/emojis` to see everything available."
            )
            return

        online = str(green) if green else "🟢"
        offline = str(red) if red else "🔴"

        snippet = (
            '"emoji": {\n'
            f'    "online": "{online}",\n'
            f'    "offline": "{offline}"\n'
            "}"
        )
        preview = f"{online} **Online**    {offline} **Offline**"

        message = (
            f"{preview}\n\n"
            "Paste this into `data/config.json` (merge it with the existing "
            "`emoji` block rather than replacing it):\n"
            f"```json\n{snippet}\n```\n"
        )
        if green and not green.animated:
            message += "\n⚠️ `greendot` is not animated — re-upload the GIF version."
        if red and not red.animated:
            message += "\n⚠️ `reddot` is not animated — re-upload the GIF version."
        message += "\nThe dashboard picks it up on the next refresh, within a minute."

        await interaction.followup.send(message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Emojis(bot))
