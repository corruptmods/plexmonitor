"""PlexMonitor bot entrypoint.

Cogs load in setup_hook, which runs once before the gateway connects, so a
reconnect never loads them twice. Logging always reaches stdout, which is
what `docker compose logs` shows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import sys
from logging.handlers import TimedRotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from util import ratelimit
from util.auth import is_admin
from util.jsonfile import data_dir

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"
if not RUNNING_IN_DOCKER:
    load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN must be set")

# ------------------------------------------------------------------ logging

LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

# Settings whose values must never reach a log line. Some services take
# their key in the URL (Tautulli, SABnzbd, Plex), and an HTTP error's text
# can include that URL.
SECRET_SETTINGS = (
    "DISCORD_TOKEN", "TAUTULLI_API_KEY", "PLEX_TOKEN", "SABNZBD_API_KEY",
    "UPTIME_API_KEY", "UPTIME_PASSWORD", "UPTIMEROBOT_API_KEY",
    "RADARR_API_KEY", "SONARR_API_KEY", "SEERR_API_KEY", "AI_API_KEY",
)
_SECRET_PARAM = re.compile(
    r"(?i)\b(apikey|api_key|x-plex-token|token|key|password)=([^&\s'\"]+)")


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then blanks out secrets, tracebacks included."""

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        self.secrets = sorted(
            {value for value in (os.getenv(name, "").strip()
                                 for name in SECRET_SETTINGS)
             if len(value) >= 6}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return _SECRET_PARAM.sub(r"\1=***", text)


formatter = RedactingFormatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

bot_logger = logging.getLogger("plexmonitor")
bot_logger.setLevel(LOG_LEVEL)

console = logging.StreamHandler(sys.stdout)
console.setFormatter(formatter)
bot_logger.addHandler(console)

# discord.py logs through its own "discord" logger -- and that includes
# exceptions raised inside view/button callbacks and gateway problems.
# Without a handler here those vanish silently, which once made a dead
# pager button look like nothing happened at all.
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.WARNING)
discord_logger.addHandler(console)


class _DropRateLimitChatter(logging.Filter):
    """Silence discord.py's own "We are being rate limited" warnings.

    They duplicate what util.ratelimit already records -- and records
    better, with scope, bucket and an aggregated summary instead of one
    line per retry. At steady state that was two log lines per scheduled
    edit saying the same thing. Everything else discord.http reports
    (gateway trouble, callback exceptions) still comes through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "We are being rate limited" not in record.getMessage()


logging.getLogger("discord.http").addFilter(_DropRateLimitChatter())

if not RUNNING_IN_DOCKER:
    os.makedirs("logs", exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join("logs", "plex-monitor.log"),
        when="midnight", interval=1, backupCount=7, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    bot_logger.addHandler(file_handler)

# --------------------------------------------------------------------- bot

# Only the intents actually needed. Intents.all() requests the privileged
# members and presence intents, which refuse to connect unless they are also
# ticked in the Discord developer portal.
intents = discord.Intents.default()
intents.message_content = False

COGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cogs")
DATA_DIR = data_dir()


class SharedTree(app_commands.CommandTree):
    """The command tree, tolerant of sharing its bot token.

    Another program signed in with the same bot token (a watch party bot,
    say) has its commands delivered here too. Those are its to answer, so an
    unknown command is only logged at debug level instead of as an error.
    """

    async def on_error(self, interaction: discord.Interaction,
                       error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            bot_logger.debug("Ignoring /%s: not one of this bot's commands "
                             "(another program on this token may answer "
                             "it, or its cog isn't loaded)", error.name)
            return
        await super().on_error(interaction, error)


class PlexMonitorBot(commands.Bot):
    def __init__(self) -> None:
        # Watch Discord's rate-limit headers on every REST response. discord.py
        # accepts an aiohttp TraceConfig here, so the dashboard can pace itself
        # from the budget Discord reports rather than inferring it from 429s.
        self.rate_limits = ratelimit.RateLimitObserver()
        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=SharedTree,
            http_trace=ratelimit.make_trace(self.rate_limits),
        )

    async def setup_hook(self) -> None:
        """Runs once before the gateway connects -- not on every reconnect."""
        for filename in sorted(os.listdir(COGS_DIR)):
            if not filename.endswith(".py") or filename.startswith("__"):
                continue
            name = filename[:-3]
            try:
                await self.load_extension(f"cogs.{name}")
                bot_logger.info("Loaded cog: %s", name)
            except commands.ExtensionError:
                bot_logger.exception("Failed to load cog %s", name)
        # Sync only when the commands Discord would receive actually
        # changed. Command writes are rate limited, and a crash-looping
        # container syncing on every start burns that quota. The fingerprint
        # is the exact payload (names, descriptions, options, choices,
        # limits, default permissions) plus the application it belongs to,
        # so switching to another bot token syncs too.
        try:
            payload = sorted(
                (cmd.to_dict(self.tree) for cmd in self.tree.get_commands()),
                key=lambda item: item["name"])
            digest = hashlib.sha256(json.dumps(
                [self.application_id, payload], sort_keys=True,
                default=str).encode()).hexdigest()
            marker = os.path.join(DATA_DIR, "command_sync.json")
            previous = None
            try:
                with open(marker, encoding="utf-8") as handle:
                    previous = json.load(handle).get("digest")
            except (OSError, ValueError):
                pass
            if previous == digest:
                bot_logger.info("Command tree unchanged -- skipping sync")
            else:
                await self.tree.sync()
                bot_logger.info("Command tree synced globally")
                try:
                    with open(marker, "w", encoding="utf-8") as handle:
                        json.dump({"digest": digest}, handle)
                except OSError as exc:
                    # A read-only or full data/ must not stop the bot --
                    # it only means the next start syncs again.
                    bot_logger.warning("Could not record command sync: %s",
                                       exc)
        except discord.HTTPException:
            bot_logger.exception("Failed to sync command tree")

    async def on_ready(self) -> None:
        bot_logger.info("Bot is online as %s", self.user)
        # This bot's commands are global. Server-level commands are left
        # alone: another program sharing the token may have registered them.


bot = PlexMonitorBot()
tree = bot.tree


async def is_authorized(interaction: discord.Interaction) -> bool:
    """Bot admins only: DISCORD_AUTHORIZED_USERS, else the bot's owner."""
    return await is_admin(bot, interaction.user)


async def _manage(interaction: discord.Interaction, cog: str, action: str) -> None:
    await interaction.response.defer(ephemeral=True)
    if not await is_authorized(interaction):
        await interaction.followup.send("You are not authorized to run this command.")
        return
    handler = {
        "load": bot.load_extension,
        "unload": bot.unload_extension,
        "reload": bot.reload_extension,
    }[action]
    try:
        await handler(f"cogs.{cog}")
        await interaction.followup.send(f"Cog `{cog}` {action}ed.")
        bot_logger.info("Cog %s %sed by %s", cog, action, interaction.user)
    except commands.ExtensionError as exc:
        await interaction.followup.send(f"Error: `{exc}`")
        bot_logger.error("Error during %s of %s: %s", action, cog, exc)


@tree.command(name="load", description="Load a specific cog")
async def load(interaction: discord.Interaction, cog: str) -> None:
    await _manage(interaction, cog, "load")


@tree.command(name="unload", description="Unload a specific cog")
async def unload(interaction: discord.Interaction, cog: str) -> None:
    await _manage(interaction, cog, "unload")


@tree.command(name="reload", description="Reload a specific cog")
async def reload(interaction: discord.Interaction, cog: str) -> None:
    await _manage(interaction, cog, "reload")


@tree.command(name="cogs", description="List all available cogs")
async def list_cogs(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        await interaction.response.send_message(
            "You are not authorized to run this command.", ephemeral=True)
        return
    available = [f[:-3] for f in sorted(os.listdir(COGS_DIR))
                 if f.endswith(".py") and not f.startswith("__")]
    loaded = {ext.split(".")[-1] for ext in bot.extensions}
    embed = discord.Embed(title="Cogs", colour=0x5865F2)
    for cog in available:
        embed.add_field(name=cog, value="Loaded" if cog in loaded else "Not loaded", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        bot_logger.error("Invalid DISCORD_TOKEN")
    except Exception:
        bot_logger.exception("Bot crashed")
