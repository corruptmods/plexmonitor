"""Dashboard cog -- builds and maintains the Discord status embed.

Replaces the original plex_core.py. The important behavioural changes:

  * Tautulli is the default data source. One JSON call returns every active
    stream already enriched with transcode decisions and stream bitrates.
  * The plexapi fallback holds a single PlexServer instance for the bot's
    lifetime instead of reconstructing it -- and therefore re-authenticating
    and re-fetching the server capability document -- on every tick.
  * Library counts use totalSize / totalViewSize, which ask Plex for a number.
    The original called section.all() twice per section, downloading every
    item's metadata just to take len() of it.
  * Every blocking call is pushed through asyncio.to_thread, so synchronous
    HTTP no longer stalls discord.py's event loop and trips heartbeat warnings.
  * Uptime is read from a persisted state tracker, so it survives restarts
    and never caps out.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re as _re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

try:
    from aiohttp import web
    WEB_AVAILABLE = True
except ImportError:  # pragma: no cover
    WEB_AVAILABLE = False

from util.jsonfile import data_dir, read_json, write_json
from util.formatting import (
    discord_timestamp, format_bitrate, format_count,
    format_duration, format_duration_long, format_hms,
)
from util import arr as arrapi
from util import cardimage
from util import textwidth
from util.kuma import KumaClient
from util.uptimerobot import UptimeRobotClient
from util.kuma_history import SOCKETIO_AVAILABLE, KumaHistoryClient
from util.progress import ProgressBar
from util.state import ServerStateTracker
from util.tautulli import TautulliClient, TautulliError

logger = logging.getLogger("plexmonitor.dashboard")

# Bumped on every code change. Shown in the embed footer and logged at
# startup, so "is my new code actually running?" is answered by looking.
BUILD = "v1.0.0"
# Longest the bot's status goes without being re-sent (see _sync_presence).
PRESENCE_REFRESH = 15 * 60.0
# Shortest gap between two status changes.
PRESENCE_MIN_GAP = 15.0
# custom_id prefix of the dashboard's own buttons. A message by this bot login
# carrying any other button or menu is someone else's -- the request panel
# ("plexmon:"), or another program sharing the bot token -- and is never
# swept.
OWN_ID_PREFIXES = ("plex-monitor:",)


def not_ours(message) -> bool:
    """True when a message carries a button or menu this bot didn't mint."""
    def walk(items):
        for item in items or []:
            yield item
            yield from walk(getattr(item, "children", None))
            accessory = getattr(item, "accessory", None)
            if accessory is not None:
                yield accessory
    for component in walk(getattr(message, "components", None)):
        custom_id = getattr(component, "custom_id", None)
        if isinstance(custom_id, str) and custom_id \
                and not custom_id.startswith(OWN_ID_PREFIXES):
            return True
    return False
# How long a failed activity fetch may reuse the last good snapshot while
# Tautulli still reports Plex as connected.
ACTIVITY_HOLD = 120.0

# Discord error codes that mean "this message cannot be edited right now,
# and retrying won't help": 30046 caps edits to messages older than an hour;
# 400009 caps attachment uploads via message edits, server-wide. Both reset
# for a freshly posted message, which is exactly what the repost path does.
# Minimum seconds between reposts, whatever triggers them.
REPOST_COOLDOWN = 600
# Rapid button presses are coalesced into a single render this many seconds
# after the last one. A flip re-uploads every file on the page (~8), so
# mashing the arrows is by far the most expensive thing a user can do; this
# turns a burst of N presses into one edit instead of N.
FLIP_COALESCE = 1.0
# How long a button press will wait for the render lock before giving up.
# Short by design: the press is a person waiting on a screen, and the page
# is already committed either way.
FLIP_LOCK_WAIT = 3.0
# How long to let a single message edit run before treating it as stalled.
# Normal edits finish in well under a second; anything past this is sitting
# inside discord.py's rate limiter waiting out a 429 it never surfaces.
# Only a backstop against a genuinely hung request -- NOT a throttling
# signal. discord.py waits out 429s and retries correctly, and measured
# waits have run to 47s and beyond. A 30s limit was aborting edits that were
# seconds from succeeding, then charging the bot a stall for the failure it
# had just caused.
#
# Lowered from 180 to 60. This timeout also decides how long the
# render lock stays held: a refresh sitting inside discord.py's limiter
# for 153 seconds blocks every button press for those 153 seconds. Past a
# minute the content is stale enough that the next tick may as well redraw
# it, and freeing the lock matters more than saving the attempt.
EDIT_TIMEOUT = 60.0
# Landed-but-slow is not the same as failed. Shared-scope penalties measure
# 15-68s live and discord.py rides most of them out successfully, so an edit
# that RETURNS in that range was throttled, not broken. Sits just under
# EDIT_TIMEOUT: anything slower than this never returns at all and is
# escalated by the timeout path instead.
#
# EDIT_TIMEOUT deliberately stays at 60s. Raising it to outlast every
# penalty would also hold the render lock that long, and this ceiling
# exists precisely because a 153s wait meant 153s of dead pager buttons.
HARD_STALL_SECONDS = 55.0
# A penalty shorter than this is ordinary traffic shaping. Pause exactly as
# long as Discord asked and carry on -- no interval doubling, no ladder.
SHORT_PENALTY_SECONDS = 15.0
# Discord allows 2 channel renames per 10 minutes. Never attempt more than
# one per this window: a rename that Discord normalises on arrival would
# otherwise look unapplied and retry forever, exhausting the budget before
# a real status flip needs it.
RENAME_COOLDOWN = 330.0
# Slack between the bar line and every other line in a card. Absorbs the
# error in the width table and the "|- " prefix the detail lines carry.
CARD_WIDTH_MARGIN = 26.0
# Discord's embed interior at maximum width, and what a poster thumbnail
# takes out of the text column beside it. Only meaningful when an anchor
# image is pinning the embed to that maximum -- otherwise the embed is
# only as wide as its own content and there is nothing to centre within.
EMBED_CONTENT_PX = 484.0
THUMBNAIL_PX = 96.0
NBSP_PX = 4.45          # U+00A0 at 16px, from the width table
# The state and delivery emoji that prefix a headline.
HEADLINE_BADGE_PX = 44.0
# How long a signed CDN url may be reused in an embed before it is
# re-resolved. Every re-resolution is a visible poster reload, so this
# wants to sit as close to Discord's 24h expiry as is safe rather than
# comfortably inside it: 6h meant four avoidable flashes a day.
ATTACHMENT_URL_TTL = 20 * 3600.0
# Absolute floor between edit attempts. Only bites when an edit outran the
# whole refresh interval; normal pacing is the loop's job, not this.
MIN_ATTEMPT_GAP = 8.0
# A playback event fires several webhooks in a burst (start + transcode
# decision + ...). Wait this long after the LAST one before refreshing, so
# a burst costs one edit.
WEBHOOK_DEBOUNCE = 2.0
# Webhooks may pull the next edit FORWARD, but never push the sustained
# rate past what the channel's edit bucket will carry.
#
# 45s was set in v73, when every refresh could drag a fresh set of bar
# images behind it and uploads were the thing drawing 429s. Since v76 a
# steady-state edit carries no files at all -- text only, posters already
# adopted by URL -- and a text edit is one cheap PATCH. So the gap after a
# fileless edit drops to 10s, while an edit that actually UPLOADED keeps
# the old conservative spacing.
WEBHOOK_MIN_GAP = 10.0
WEBHOOK_UPLOAD_GAP = 45.0
# Never spend the bucket's last slot on a webhook. The scheduled refresh is
# the guarantee that the dashboard is never wrong for long; a poke is an
# optimisation and must yield to it.
WEBHOOK_HEADROOM_RESERVE = 1
# A poke arriving during a rate-limit backoff waits for the backoff to end
# (up to this long) instead of being dropped -- a stream that ended during
# the pause still lands promptly after it.
WEBHOOK_MAX_WAIT = 600.0
# Default stale image re-uploads allowed per edit (config: image_batch).
# Repaints spread across the regular once-a-minute edits instead of landing
# as one attachment-heavy burst -- bursts, not volume, are what the shared
# budget punishes.
IMAGE_BATCH = 2
# Minimum seconds between image-carrying edits (config:
# image_edit_gap_seconds). v66's quantum-exact repaints removed the timers
# that had been limiting image-edit FREQUENCY, and 15 rate limits in 6 hours
# followed. Crossings still trigger repaints -- delivery just waits for the
# next slot at least this far from the previous image edit.
IMAGE_EDIT_GAP = 180.0
# Exponential backoff after a rate-limited edit: 5, 10, 20, then 30 minutes.
# Short waits were the mistake -- resuming inside the penalty window kept the
# channel's bucket permanently empty, so it never got a chance to refill.
STALL_BACKOFF_BASE = 300.0
STALL_BACKOFF_MAX = 1800.0
# Automatic refresh throttling. Each stall doubles the multiplier applied to
# the configured refresh interval; a sustained clean run halves it back. This
# is what keeps the bot inside a budget it cannot measure, without anyone
# hand-tuning config.
# Cap expressed as a MULTIPLE, kept only as a floor for the derived value
# below. The real ceiling is THROTTLE_CEILING_SECONDS: a fixed multiple
# breaks at a fast base clock, where 8x of 10s tops out at 80s and cannot
# clear a 153s penalty -- the ladder would walk straight back into the wall
# it just backed away from.
THROTTLE_MAX = 8
THROTTLE_CEILING_SECONDS = 600.0
# Consecutive good refreshes required before relaxing one throttle step.
CLEAN_RUN_TO_RECOVER = 4
# Images ride their own, far slower clock -- see the note in _compose. This
# is the ceiling once the automatic throttle has widened it.
IMAGE_THROTTLE_MAX = 4
IMAGE_REFRESH_MAX = 3600.0
# An edit that takes longer than this was rate limited even though it
# eventually succeeded -- discord.py hides short 429s by sleeping through
# them. A healthy edit completes in well under a second.
SOFT_LIMIT_SECONDS = 2.0
# How long a real 429 keeps the empty-bucket early warning meaningful.
# Outside this window an exhausted bucket is just the bot using its
# allowance, which is what an allowance is for.
HEADROOM_MEMORY = 900.0
# Consecutive soft limits tolerated before pre-emptively slowing down.
SOFT_LIMITS_BEFORE_THROTTLE = 2
# Roughly how many times a bar should visibly advance over one item, whatever
# its runtime. The image clock is derived from this and the shortest thing
# playing, so a 22-minute episode is not treated like a three-hour film.
IMAGE_UPDATES_PER_ITEM = 8
# Hard floor on the image clock, so a very short item cannot drive uploads
# faster than the attachment budget tolerates.
IMAGE_REFRESH_MIN = 300.0

STATUS_COLOURS = {
    "online": 0x23A55A,
    "offline": 0xF23F43,
    "degraded": 0xF0B232,
}

# Discord hard-limits an embed field value to 1024 characters, and an embed to
# 25 fields total. The dashboard spends up to 12 on fixed panels (3 header,
# now-playing, downloading, 2 download extras, 3 uptime windows, up to 2 row
# padders), which leaves this many for libraries.
FIELD_LIMIT = 1024
MAX_EMBED_FIELDS = 25


def _box_glyph(ch: str) -> bool:
    """True for the block/geometric glyphs a progress bar is drawn from.

    Box Drawing (U+2500-257F), Block Elements (U+2580-259F) and Geometric
    Shapes (U+25A0-25FF). None of these are covered by the client's own
    font, so every one of them resolves through fallback -- which is why
    their real widths do not match any width table we can build from a
    normal text face.
    """
    return 0x2500 <= ord(ch) <= 0x25FF

# Section-rule glyphs, mapped to the repeat count that spans the embed's
# ~484px interior at field-NAME size. Box-drawing glyphs (U+250x) are roughly
# 9.5px there; Block Elements (U+258x) are about an em, so they need fewer.
#
# Block Elements are the safe family: the same reason "█░" survived the bar
# work while "▰▱" did not. Geometric Shapes fall out of the client's primary
# font and pick up a fallback with different metrics, so the run's width
# drifts. U+258x is covered everywhere the bar glyphs already are.
#
# The counts below are sized for a PHONE, not a desktop. Mobile gives an
# embed roughly 300px of interior against the desktop's ~484, and a rule
# that wraps looks far worse than one that stops short -- a wrapped rule
# puts a stub on its own line, which reads as a second broken divider.
# Desktop simply shows the rule ending before the right margin, which is
# invisible unless you go looking for it.
DIVIDER_GLYPHS = {
    "line": ("─", 30),      # ─ light horizontal, mid-height
    "heavy": ("━", 30),     # ━ heavier, still mid-height
    "dotted": ("┄", 30),    # ┄ broken, reads as a softer break
    "double": ("═", 30),    # ═ two thin rules
    "hairline": ("▁", 20),  # ▁ lower one eighth block -- sits ON the
                                 #   baseline, tiles seamlessly, and is the
                                 #   closest text gets to a rendered rule
    "shade": ("░", 20),     # ░ light shade, a muted band
}
MAX_LIBRARY_FIELDS = 8

# Monospace character budgets before Discord wraps a line for us. A full-width
# embed field fits roughly 32 characters on a phone; an inline field is a third
# of that. Breaking the lines ourselves at these widths keeps the layout
# predictable instead of letting text spill mid-token.
WIDE_WIDTH = 40
MAX_TITLE_LENGTH = 100
# Combined visible budget for a stream's title + user on one headline. Long
# titles wrap onto a second line rather than being cut -- only truly absurd
# release names get clipped.
HEADLINE_BUDGET = 100

DEFAULT_EMOJI = {
    "online": "🟢", "offline": "🔴",
    "uptime": "🖥️", "streams": "📡", "free_space": "💾",
    "queue_speed": "🚀", "total_space": "🗄️",
    "now_playing": "🎬", "downloading": "📥", "uptime_stats": "📊",
    "episodes": "🎥", "tracks": "🎵", "bandwidth": "📶", "progress": "⏳",
    "playing": "▶️", "paused": "⏸️", "transcode": "🔄", "direct": "⚡",
    "response": "⏱️",
    "last_downtime": "🔻",
    "all_time": "🏆", "bot_uptime": "🤖",
    "idle": "💤",
    # Its own key rather than reusing "downloading": the two
    # sections sit next to each other and shared an icon, so
    # nothing distinguished "in progress" from "finished".
    "completed": "✅",
}


class CardPager(discord.ui.View):
    """Persistent button row for flipping between pages of stream cards.

    timeout=None plus fixed custom_ids make the view survive bot restarts --
    Discord re-delivers presses to whatever handler is registered for the id.
    """

    def __init__(self, cog: "Dashboard") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary,
                       custom_id="plex-monitor:cards:prev")
    async def prev(self, interaction: discord.Interaction,
                   button: discord.ui.Button) -> None:
        await self.cog._flip_page(interaction, -1)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary,
                       disabled=True, custom_id="plex-monitor:cards:page")
    async def page(self, interaction: discord.Interaction,
                   button: discord.ui.Button) -> None:
        pass  # label only, always disabled

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary,
                       custom_id="plex-monitor:cards:next")
    async def next(self, interaction: discord.Interaction,
                   button: discord.ui.Button) -> None:
        await self.cog._flip_page(interaction, 1)

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item) -> None:
        """Surface button-callback failures in OUR log, not just discord.py's."""
        logger.warning(
            "Pager button %s errored: %r",
            getattr(item, "custom_id", "?"), error,
        )

    def sync(self, page: int, total: int, stream_count: int) -> None:
        for child in self.children:
            if child.custom_id == "plex-monitor:cards:prev":
                child.disabled = page <= 0
            elif child.custom_id == "plex-monitor:cards:next":
                child.disabled = page >= total - 1
            elif child.custom_id == "plex-monitor:cards:page":
                child.label = (
                    f"Page {page + 1}/{total} · {stream_count} "
                    f"stream{'s' if stream_count != 1 else ''}"
                )



# Sections of config.json that must be objects. Anything else (a null, a
# list, a stray string) is replaced by {} -- that section's defaults -- with
# a warning, instead of breaking every refresh that reads it.
CONFIG_SECTIONS = (
    "dashboard", "panels", "emoji", "progress_bar", "plex_sections",
    "presence", "cache", "sabnzbd", "channel_dot", "stream_cards",
    "webhook", "recent_downloads",
)


def normalise_config(config: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(config)
    for key in CONFIG_SECTIONS:
        if key in config and not isinstance(config[key], dict):
            logger.warning("config.json: %r should be an object -- using its "
                           "defaults", key)
            config[key] = {}
    sections = config.get("plex_sections") or {}
    if "sections" in sections:
        named = sections.get("sections")
        clean = ({k: v for k, v in named.items() if isinstance(v, dict)}
                 if isinstance(named, dict) else {})
        if clean != named:
            logger.warning("config.json: plex_sections.sections should map "
                           "each library name to an object -- skipping the "
                           "rest")
        config["plex_sections"] = {**sections, "sections": clean}
    presence = config.get("presence") or {}
    if "sections" in presence:
        listed = presence.get("sections")
        clean = ([e for e in listed if isinstance(e, dict)]
                 if isinstance(listed, list) else [])
        if clean != listed:
            logger.warning("config.json: presence.sections should be a list "
                           "of objects -- skipping the rest")
        config["presence"] = {**presence, "sections": clean}
    return config

class Dashboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        self.data_dir = data_dir()
        self._config_path = os.path.join(self.data_dir, "config.json")
        self._config_mtime: float = 0.0
        self.config = self._load_json(self._config_path, {})
        if not isinstance(self.config, dict):
            # Before ANY .get(): a list or string here crashed the cog on
            # load, so there was no dashboard at all.
            logger.error("config.json is not a JSON object -- using defaults")
            self.config = {}
        self.config = normalise_config(self.config)
        try:
            self._config_mtime = os.path.getmtime(self._config_path)
        except OSError:
            pass
        self.user_mapping = self._load_json(os.path.join(self.data_dir, "user_mapping.json"), {})

        channel_id = os.getenv("CHANNEL_ID")
        if not channel_id:
            raise ValueError("CHANNEL_ID must be set")
        self.channel_id = int(channel_id)

        # Stream cards can live in their own channel. Discord buckets
        # message edits by channel_id -- it is the major parameter of the
        # route -- so a second channel is a second budget. The dashboard
        # then carries one embed and (at most) its strip, which is the shape
        # upstream runs at 1/min without ever being limited; the cards keep
        # their posters and take whatever pressure comes with them.
        try:
            self.cards_channel_id = int(
                (self.config.get("stream_cards") or {}).get("channel_id") or 0)
        except (TypeError, ValueError):
            self.cards_channel_id = 0

        self.message_id_file = os.path.join(self.data_dir, "dashboard_message_id.json")
        stored = self._load_json(self.message_id_file, {})
        self.dashboard_message_id = stored.get("message_id") if isinstance(stored, dict) else None

        # The cards message keeps its own id and its own dedupe state. File
        # bookkeeping stays shared: names never collide between the two
        # surfaces (strip_/plexicon on one, p_/anchor on the other), so one
        # url cache serves both.
        self.cards_id_file = os.path.join(self.data_dir, "cards_message_id.json")
        stored_cards = self._load_json(self.cards_id_file, {})
        self.cards_message_id = (stored_cards.get("message_id")
                                 if isinstance(stored_cards, dict) else None)
        self._cards_payload: Optional[str] = None
        # Whether the cards message is currently parked on the idle
        # placeholder. Starts False so the first pass after a restart always
        # writes a known state rather than trusting what is on screen.
        self._cards_idle = False

        # Recently-completed downloads: its own message, its own id file, its
        # own dedupe signature. Nothing here touches the dashboard message.
        self.recent_id_file = os.path.join(self.data_dir,
                                           "recent_message_id.json")
        stored_recent = self._load_json(self.recent_id_file, {})
        self.recent_message_id = (stored_recent.get("message_id")
                                  if isinstance(stored_recent, dict) else None)
        self._recent_payload: Optional[str] = None
        self._recent_cache = None
        self._recent_cache_at = 0.0
        self._recent_pending = None
        # session_key -> (view_offset_ms, monotonic_at, was_paused)
        self._seek_positions: Dict[str, tuple] = {}
        self._seek_serial = 0
        # Whether anything is playing. Drives the tightened refresh cadence,
        # so it must be updated from the same place the signature is built --
        # that runs on the change poll AND the scheduled tick, so it never
        # goes stale on its own.
        self._streams_active = False

        # Locally observed completions, because SABnzbd's history is emptied
        # by the *arr apps as soon as they import. Persisted so a restart
        # does not blank the panel.
        # Radarr/Sonarr: the apps that delete SABnzbd's history keep their
        # own, and theirs records whether the IMPORT succeeded -- which is
        # the question the panel is really asking.
        self._arr = [
            arrapi.ArrClient(os.getenv("RADARR_URL", ""),
                             os.getenv("RADARR_API_KEY", ""), "radarr"),
            arrapi.ArrClient(os.getenv("SONARR_URL", ""),
                             os.getenv("SONARR_API_KEY", ""), "sonarr"),
        ]
        for client in self._arr:
            if client.enabled:
                logger.info("%s history enabled as a completed-downloads "
                            "source", client.kind.title())

        self.completed_file = os.path.join(self.data_dir,
                                           "completed_downloads.json")
        stored_done = self._load_json(self.completed_file, {})
        self._completed = (stored_done.get("items")
                           if isinstance(stored_done, dict) else []) or []
        self._queue_seen: Dict[str, dict] = {}
        self._cards_swept = False
        self._last_change_sig: Optional[str] = None
        self._started_at = datetime.now(timezone.utc)

        try:
            confirmations = int((self.config.get("cache") or {})
                                .get("down_confirmations", 2))
        except (TypeError, ValueError):
            confirmations = 2
        self.state = ServerStateTracker(
            os.path.join(self.data_dir, "server_state.json"),
            down_confirmations=confirmations,
        )
        try:
            self._apply_config()
        except Exception:
            # Hot reload already rolls back a bad edit; startup had no such
            # net, so one "dashboard": null meant no cog and no dashboard at
            # all. Fall back to built-in defaults and say so loudly.
            logger.exception("config.json could not be applied at startup "
                             "-- running on built-in defaults until it is "
                             "fixed")
            self.config = {}
            self._apply_config()

        # ---- data sources -------------------------------------------------
        self.tautulli: Optional[TautulliClient] = None
        if os.getenv("TAUTULLI_URL") and os.getenv("TAUTULLI_API_KEY"):
            self.tautulli = TautulliClient(
                os.environ["TAUTULLI_URL"],
                os.environ["TAUTULLI_API_KEY"],
                verify_ssl=os.getenv("TAUTULLI_VERIFY_SSL", "true").lower() != "false",
            )
            logger.info("Using Tautulli as the activity source")
        elif os.getenv("PLEX_URL") and os.getenv("PLEX_TOKEN"):
            logger.info("TAUTULLI_URL/TAUTULLI_API_KEY not set -- falling back to plexapi")
        else:
            logger.error("Neither Tautulli (TAUTULLI_URL, TAUTULLI_API_KEY) nor "
                         "Plex (PLEX_URL, PLEX_TOKEN) is set in .env -- the "
                         "dashboard will show Plex as offline until one is")

        # UptimeRobot wins when configured. It watches from outside the
        # network, so its numbers survive the cases a self-hosted probe
        # cannot report on: this container restarting, the host rebooting,
        # or the monitoring stack going down alongside what it monitors.
        self.uptimerobot: Optional[UptimeRobotClient] = None
        if os.getenv("UPTIMEROBOT_API_KEY"):
            self.uptimerobot = UptimeRobotClient(
                os.environ["UPTIMEROBOT_API_KEY"],
                monitor_id=os.getenv("UPTIMEROBOT_MONITOR_ID", ""),
            )
            logger.info("Using UptimeRobot as the uptime source")

        self.kuma: Optional[KumaClient] = None
        if (not self.uptimerobot and os.getenv("UPTIME_URL")
                and os.getenv("UPTIME_MONITOR_ID")):
            self.kuma = KumaClient(
                os.environ["UPTIME_URL"],
                os.environ["UPTIME_MONITOR_ID"],
                api_key=os.getenv("UPTIME_API_KEY", ""),
                username=os.getenv("UPTIME_USERNAME", ""),
                password=os.getenv("UPTIME_PASSWORD", ""),
                verify_ssl=os.getenv("UPTIME_VERIFY_SSL", "true").lower() != "false",
            )

        # Historical uptime, read from Uptime Kuma's transition log. Kuma keeps
        # these for a year, so the counter can start from the real last outage
        # instead of from first deploy.
        self.kuma_history: Optional[KumaHistoryClient] = None
        if (
            not self.uptimerobot
            and os.getenv("UPTIME_URL")
            and os.getenv("UPTIME_MONITOR_ID")
            and os.getenv("UPTIME_USERNAME")
            and os.getenv("UPTIME_HISTORY", "true").lower() != "false"
        ):
            if SOCKETIO_AVAILABLE:
                self.kuma_history = KumaHistoryClient(
                    os.environ["UPTIME_URL"],
                    os.environ["UPTIME_MONITOR_ID"],
                    username=os.environ["UPTIME_USERNAME"],
                    password=os.getenv("UPTIME_PASSWORD", ""),
                    verify_ssl=os.getenv("UPTIME_VERIFY_SSL", "true").lower() != "false",
                )
            else:
                logger.info(
                    "python-socketio is not installed -- historical uptime seeding disabled"
                )

        self._warned_channel_perms = False
        self._last_rename_try = 0.0
        # 429s that discord.py swallowed without costing us a
        # meaningful wait. Tracked to stay visible, not acted on.
        self._absorbed_429 = 0
        # When each cached CDN url was issued, so a stale one can be
        # re-resolved before Discord expires it.
        self._attachment_pinned: Dict[str, float] = {}
        # Monotonic timestamp of the last message repost, so a burst of
        # rejected edits (e.g. page-flip spam during quota exhaustion)
        # cannot turn into a burst of delete-and-repost cycles.
        self._last_repost = 0.0
        # Last presence actually sent, so the 5-minute loop only calls the
        # gateway when the text or status genuinely changed. Same principle
        # as the dashboard's payload fingerprint: timers decide when to
        # CHECK, content decides whether to SEND.
        self._last_presence: Optional[tuple] = None
        self._last_presence_at = float("-inf")
        # Flip coalescing state: when the last flip actually rendered, and
        # the pending task that will render the settled page.
        self._last_flip_render = 0.0
        # Did the most recent delivered edit UPLOAD anything?
        # Webhook spacing keys off this: text edits are cheap.
        self._last_push_had_files = False
        # The page the live message is actually DISPLAYING. It
        # trails _card_page whenever a render is deferred, and
        # that gap is what makes a repeat press meaningful.
        self._rendered_page = 0
        self._flip_task: Optional[asyncio.Future] = None
        # Monotonic deadline before which edits are pointless: set when an
        # edit stalls in the rate limiter, cleared by the first edit that
        # completes. Without it the loop keeps feeding a bucket that is
        # already empty, which is what made the 429s self-sustaining.
        self._stalled_until = 0.0
        self._stall_count = 0
        # When the last edit attempt finished. tasks.Loop fires the next
        # iteration IMMEDIATELY when the previous one overran its interval,
        # so after a 120-second rate-limit wait the bot would otherwise
        # retry one second later -- and earn a longer penalty each time.
        self._last_edit_at = 0.0
        # Self-governing throttle: multiplier on the configured refresh
        # interval, plus a separate, much slower clock for image uploads.
        self._throttle = 1
        self._image_throttle = 1
        # When the last edit carrying ANY file went out -- image edits are
        # spaced at least _image_gap apart.
        self._last_image_edit = 0.0
        # Per-item repaint clocks: when each stream's bar last actually
        # shipped, keyed by session. Pruned as streams end.
        self._bar_painted: Dict[str, float] = {}
        self._pending_bar_sessions: Dict[str, tuple] = {}
        # Set when attachments are adopted after a restart; unknown-age bars
        # measure their first repaint from here instead of uploading at once.
        self._adopted_at = 0.0
        self._clean_refreshes = 0
        self._soft_limits = 0
        # Lifetime counters for the health snapshot. _soft_limits and
        # _stall_count are LADDER state and reset as it moves; these do not.
        self._soft_limit_total = 0
        self._escalation_total = 0
        self._stall_total = 0
        self._health_written = 0.0
        self._shortest_ms = 0
        # Which media occupied each card slot in the last render that landed.
        # Image holds are only valid while this is unchanged.
        self._rendered_layout: tuple = ()
        self._pending_layout: tuple = ()
        # Serialises compose+push. The refresh loop and button presses run in
        # separate tasks, and _compose writes instance state that _push reads
        # after its own await -- without this they interleave and corrupt the
        # attachment bookkeeping.
        self._render_lock = asyncio.Lock()
        # Set on unload; any in-flight button work checks it before editing.
        self._closed = False
        # Realtime webhook listener state.
        self._web_runner = None
        self._webhook_task: Optional[asyncio.Future] = None
        self._webhook_again = False
        self._webhook_debouncing = False
        self._cards_fallback_warned = False
        self._page_rendered = 0
        # When content last actually reached Discord; drives the heartbeat.
        self._last_delivered = 0.0
        # Duplicate sweep runs once, on the first refresh tick after start.
        self._swept = False

        # Poster plumbing. Discord's servers cannot fetch images from a
        # LAN-only Tautulli, so posters are downloaded by the bot and attached
        # to the message. They are only re-uploaded when the set of streams
        # changes; routine progress edits reuse the CDN URLs Discord assigned
        # to the existing attachments.
        self._poster_cache: Dict[str, bytes] = {}
        # Fire-and-forget poster work, cancelled on unload so nothing
        # reopens a client session after it was closed.
        self._side_tasks: set = set()
        self._card_sessions: List[Dict[str, Any]] = []
        self._attachment_urls: Dict[str, str] = {}
        # Bundled Plex-style icon for the dashboard thumbnail. Shipped as an
        # attachment because Discord's CDN cannot fetch from a LAN host, and
        # an external icon_url in config still takes precedence.
        self._icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "plex_icon.png")

        self._last_payload: Optional[str] = None
        # Snapshot of the last tick's fetched data. Page flips render from
        # this instead of re-querying Tautulli/SABnzbd, which is most of the
        # button latency.
        self._snap: Optional[Dict[str, Any]] = None
        self._file_keys: Dict[str, tuple] = {}
        self._pending_keys: Dict[str, tuple] = {}
        self._keep_names: set = set()
        self._refs: List[tuple] = []
        self._card_page = 0
        self._total_pages = 1
        self._stream_total = 0
        self.pager = CardPager(self)
        # Registering the view makes button presses reach us even for
        # messages posted before the last restart.
        bot.add_view(self.pager)

        # plexapi fallback state
        self._plex = None
        self._plex_lock = asyncio.Lock()

        # caches
        self._library_cache: List[Dict[str, Any]] = []
        self._library_cache_at: Optional[datetime] = None
        # Floored: a zero or garbage value in config must not turn the cache
        # into a per-tick refetch (or crash the cog at load).
        cache_cfg = self.config.get("cache", {})
        try:
            self._library_interval = max(60, int(cache_cfg.get("library_update_interval", 900)))
        except (TypeError, ValueError):
            self._library_interval = 900
        self._kuma_cache: Dict[str, Any] = {}
        self._kuma_cache_at: Optional[datetime] = None
        try:
            self._kuma_interval = max(60, int(cache_cfg.get("uptime_update_interval", 300)))
        except (TypeError, ValueError):
            self._kuma_interval = 300

        logger.info("PlexMonitor dashboard build %s", BUILD)
        self.update_dashboard.change_interval(seconds=self.refresh_seconds)
        self.update_dashboard.start()
        self.update_presence.start()
        if self.change_poll_seconds:
            self.watch_changes.change_interval(seconds=self.change_poll_seconds)
            self.watch_changes.start()
            logger.info("Change watcher on: polling every %.0fs, refreshing "
                        "only when playback or downloads actually change",
                        self.change_poll_seconds)
        # UptimeRobot's transition log is read by this loop too; gating it
        # on Kuma alone meant the UptimeRobot branch never ran.
        if self.kuma_history or self.uptimerobot:
            self.sync_uptime_history.start()

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _load_json(path: str, default: Any) -> Any:
        return read_json(path, default)

    def _apply_config(self) -> None:
        """(Re)derive every setting computed from config.json.

        Called at startup and again whenever the file changes on disk, so an
        emoji or style edit takes effect on the next refresh instead of
        waiting for a container restart.
        """
        self.bar = ProgressBar(self.config.get("progress_bar"))
        # Transcoding streams may draw with their own style, so the bar can
        # carry the same amber the card border and the legend already use.
        # Falls back to the main style, so this costs nothing unless set.
        _bar_cfg = dict(self.config.get("progress_bar") or {})
        _alt = _bar_cfg.get("transcode_style")
        self.bar_transcode = (
            ProgressBar({**_bar_cfg, "style": _alt})
            if _alt and _alt in (_bar_cfg.get("styles") or {}) else self.bar)

        style = str(self.config.get("dashboard", {}).get("field_style", "pill")).lower()
        if style not in ("pill", "boxed", "plain"):
            logger.warning(
                "dashboard.field_style %r is not 'pill', 'boxed' or 'plain' -- using 'pill'",
                style,
            )
            style = "pill"
        self.field_style = style
        # Emoji after the words rather than before them (upstream's shape).
        self.label_emoji_suffix = str(
            self.config.get("dashboard", {}).get("label_emoji_position",
                                                 "suffix")).lower() == "suffix"
        # How many monospace characters a detail line may use before the bot
        # wraps it. Wider lines stretch the embed; 40 fills desktop nicely.
        try:
            self.wide_width = int(self.config.get("dashboard", {}).get("line_width", WIDE_WIDTH))
        except (TypeError, ValueError):
            self.wide_width = WIDE_WIDTH
        self.wide_width = max(28, min(56, self.wide_width))
        # "big" renders the status line as a markdown heading (# ...), which
        # Discord draws large -- and custom emoji scale up with it, so the
        # animated dot grows too. If a client ever shows a literal "# ",
        # switch to "compact" for the old single-line style.
        self.status_style = str(
            self.config.get("dashboard", {}).get("status_style", "big")
        ).lower()
        if self.status_style not in ("big", "compact"):
            self.status_style = "big"
        dash_cfg = self.config.get("dashboard", {})
        # Discord renders <t:UNIX:R> client-side and reticks it every second,
        # so an ETA stays live between bot edits -- no extra API traffic.
        self.live_eta = bool(dash_cfg.get("live_eta", True))
        # What stands in for the ETA while a stream is paused. An ETA cannot
        # be computed then -- the finish time moves with the pause, so any
        # number would be a lie that keeps counting down. Leaving the slot
        # empty made a paused card look like one that simply had no ETA, so
        # it says so instead. Empty string restores the old blank.
        self.paused_text = str(dash_cfg.get("paused_text", "Paused"))
        recent = self.config.get("recent_downloads") or {}
        self.recent_enabled = bool(recent.get("enabled", False))
        # Width anchor for this panel, matching the dashboard's own strip.
        self.recent_strip = bool(recent.get("width_strip", True))
        # A block at the bottom of the dashboard embed instead of a message
        # of its own. Strictly cheaper: no extra message, no extra embed, no
        # extra attachment -- it rides an edit that was already happening,
        # so it costs field slots and characters and nothing else.
        self.recent_in_main = bool(recent.get("in_main_embed", True))
        # Render the list inside a code block, matching the boxed stats.
        # "line"     one subtext line per item (default -- compact, and
        #             keeps the live timestamp)
        # "block"     the monospace code block, aligned columns
        # "detailed"  bold title with the metadata on its own line
        self.recent_style = str(recent.get("style", "line")).lower()
        if self.recent_style not in ("line", "block", "detailed"):
            self.recent_style = "line"
        self.recent_boxed = self.recent_style == "block"
        # A live <t:...:R> reads "13 minutes ago" and updates itself; the
        # baked form is "13m" and does not. Long vs self-maintaining.
        self.recent_live_age = bool(recent.get("live_age", False))
        # Size is the least useful column here: the panel answers
        # "did it land, and when", and a byte count answers neither.
        self.recent_show_size = bool(recent.get("show_size", False))
        # Only list what Radarr/Sonarr confirmed as IMPORTED. Falls back to
        # SAB history + the queue ledger when no *arr is configured.
        self.recent_require_import = bool(recent.get("require_import", True))
        # Pixel budget for one line. 0 derives it from the embed width.
        try:
            self.recent_line_px = max(0.0, float(recent.get("line_px", 0)))
        except (TypeError, ValueError):
            self.recent_line_px = 0.0
        # Total characters a row may use inside the code block. Monospace,
        # so this is a character count -- but how many fit depends on the
        # reader's window, and a block that wraps looks far worse than one
        # that stops short: a wrapped row puts "GB 16m" alone on the next
        # line and the column alignment stops meaning anything.
        try:
            self.recent_width_chars = max(
                24, min(80, int(recent.get("width_chars", 42))))
        except (TypeError, ValueError):
            self.recent_width_chars = 42
        try:
            self.recent_cache_seconds = max(
                10.0, float(recent.get("cache_seconds", 120)))
        except (TypeError, ValueError):
            self.recent_cache_seconds = 120.0
        self.recent_title = str(recent.get("title",
                                           "Recently Completed Downloads"))
        try:
            self.recent_limit = max(1, min(15, int(recent.get("limit", 5))))
        except (TypeError, ValueError):
            self.recent_limit = 5
        try:
            self.recent_max_age_hours = max(
                0.0, float(recent.get("max_age_hours", 48)))
        except (TypeError, ValueError):
            self.recent_max_age_hours = 48.0
        # Queue items keep the compact tree form even when the stat values
        # are boxed. See _download_line for why the two are separate.
        self._downloads_boxed = bool(dash_cfg.get("downloads_boxed", False))
        # Bitrate gets its own line on a stream card. See _card_lines.
        self.card_bandwidth_line = bool(
            (self.config.get("stream_cards") or {}).get(
                "bandwidth_line", True))
        _sc = self.config.get("stream_cards") or {}
        # Icon in front of the delivery label on a card's detail line.
        self.delivery_icon = bool(_sc.get("delivery_icon", True))
        # Tautulli appends "(HW)" when Plex hardware-encodes. Useful when
        # you are debugging a transcode, noise the rest of the time.
        self.show_hw_encode = bool(_sc.get("show_hw_encode", False))
        # The headline used to carry the delivery badge too. With the
        # icon now leading the detail line, keeping both says the same
        # thing twice on a five-line card.
        self.headline_delivery_badge = bool(
            _sc.get("headline_delivery_badge", False))
        # Live elapsed counter. Discord reticks <t:...:R> client-side, so this
        # counts UP on its own between refreshes. Granularity is Discord's:
        # seconds only under a minute, then whole minutes.
        # Put "up 7d 17h · 2 streams" on the author line beside the dashboard
        # name instead of on its own row under the heading.
        self.stats_in_author = bool(dash_cfg.get("stats_in_author", True))
        try:
            self.refresh_seconds = int(dash_cfg.get("refresh_seconds", 60))
        except (TypeError, ValueError):
            self.refresh_seconds = 60
        # Discord rate-limits message edits; below ~30s the dashboard starts
        # visibly re-rendering more than it updates.
        # Floor was 30s while every edit carried poster attachments and
        # the shared limit made 2/min expensive. Measured since going
        # zero-attachment: 4 edits/min for 15h with no 429 at all. 10s is
        # 12/min -- beyond anything measured, so the floor moves but the
        # DEFAULT does not, and the soft-limit ladder still sits underneath
        # to walk the cadence back if the ceiling turns out to be real.
        self.refresh_seconds = max(10, min(600, self.refresh_seconds))
        # Heartbeat: push an edit at least this often even when nothing
        # changed, so the footer's "last updated" stays fresh and a glance
        # confirms the bot is alive. Text-only edits are cheap -- uploads
        # are the metered resource, and a heartbeat never carries files.
        # 0 disables it (pure change-detection, maximum headroom).
        try:
            heartbeat = float(dash_cfg.get("heartbeat_minutes", 1))
        except (TypeError, ValueError):
            heartbeat = 1.0
        self.heartbeat_seconds = (
            0.0 if heartbeat <= 0 else max(60.0, min(3600.0, heartbeat * 60))
        )
        # Replace the dashboard message once it reaches this age.
        #
        # OFF by default as of v75. v74 added this on the theory that Discord
        # meters edits against a message as it ages -- disproven: the same
        # bot went nine consecutive hours editing every minute on an old
        # message with zero 429s, and a freshly reposted message drew 429s
        # within 27 seconds back in b47. Age was never the variable;
        # attachment uploads were. Reposting is pure churn, so it now only
        # happens when Discord actually refuses an edit (30046 / 10008).
        try:
            repost_hours = float(dash_cfg.get("repost_hours", 0))
        except (TypeError, ValueError):
            # Match the documented default. This still read 12.0 from v74,
            # so a garbled value would have quietly re-armed the very
            # behaviour v75 disabled.
            repost_hours = 0.0
        self.repost_seconds = (
            0.0 if repost_hours <= 0 else max(1.0, min(168.0, repost_hours)) * 3600
        )
        # Put the viewer on their own bold line under the title instead of
        # trailing it after an em dash. The title keeps the headline to
        # itself, and "who is watching" stops competing with it.
        # Inline stat fields per row. Discord packs three side by side,
        # and that row is what sets the whole embed's width -- wider than
        # any line of text in it. Dropping to two narrows the dashboard so
        # it can sit level with cards that size themselves to their bar,
        # which is the only way to match widths once the anchor image is
        # gone. Padders do the work; Discord has no column setting.
        try:
            cols = int(dash_cfg.get("stat_columns", 3))
        except (TypeError, ValueError):
            cols = 3
        self._stat_columns = 2 if cols <= 2 else 3
        self._stat_emitted = 0
        # "compact" abandons inline columns entirely and writes each group
        # as one subtext line. Discord's mobile client stacks inline fields
        # into a single column whenever the screen is narrow -- 15 of them
        # become ~31 lines on a phone -- and no API setting changes that.
        # A line stays a line on every client.
        #
        # "tight" is the answer to "put the divider under ALL of it".
        # Discord spreads inline fields evenly across the embed and offers no
        # control over that: three of them are ALWAYS at 0%, 33% and 66% of
        # the full width, so "Downloads" sits hard against the right margin
        # and no rule short enough to survive a phone can ever reach it.
        # Tight drops the columns and writes the group as one field, items
        # sitting next to each other -- so the group is as wide as its own
        # text and a 20-glyph rule spans it. Same content, same pills, no
        # dead space between them.
        layout = str(dash_cfg.get("field_layout", "columns")).lower()
        self._tight = layout == "tight"
        self._compact = layout in ("compact", "tight")
        # A rule between groups of fields. Discord has no separator, so
        # this is a field of its own carrying a run of box-drawing dashes as
        # subtext -- muted and thin rather than a heavy line. Costs one of
        # the 25 field slots per divider, which is why it is opt-in.
        # Event-driven refresh. A short poll of Tautulli and SABnzbd -- both
        # on the LAN and effectively free -- watching for CHANGES rather
        # than for the clock. Progress and elapsed time are deliberately
        # excluded from the signature: they move every second, and reacting
        # to them would just be a 20-second refresh loop wearing a disguise.
        try:
            watch = float(dash_cfg.get("change_poll_seconds", 0))
        except (TypeError, ValueError):
            watch = 0.0
        self.change_poll_seconds = 0.0 if watch <= 0 else max(10.0, min(120.0, watch))
        # Cadence while streams are live. 0 disables, falling back to
        # refresh_seconds at all times.
        try:
            active = float(dash_cfg.get("active_refresh_seconds", 120))
        except (TypeError, ValueError):
            active = 120.0
        self.active_refresh_seconds = (0.0 if active <= 0
                                       else max(30.0, min(600.0, active)))
        # Refresh when a viewer scrubs. Costs nothing while nobody seeks:
        # the token only changes on a jump, so a quiet stream still produces
        # an identical signature poll after poll.
        self.seek_detection = bool(dash_cfg.get("seek_detection", True))
        try:
            self.seek_threshold_seconds = max(
                3.0, float(dash_cfg.get("seek_threshold_seconds", 15)))
        except (TypeError, ValueError):
            self.seek_threshold_seconds = 15.0
        self.section_divider = bool(dash_cfg.get("section_divider", False))
        # Named headings instead of rules. Mutually compatible with the
        # divider but there is no reason to run both.
        self.section_titles = bool(dash_cfg.get("section_titles", True))
        # False swaps Total Uptime's live relative timestamp for a boxed
        # static duration, so it matches the other values in boxed mode.
        self.uptime_relative = bool(dash_cfg.get("uptime_relative", False))
        # How a heading is drawn. "h3" is deliberately the default: it is
        # the smallest of Discord's three heading sizes, which still reads
        # clearly larger than a field name without the shouting of "# ".
        # "plain" puts the title back in the field name -- no markdown, no
        # empty line above it, and no size difference either.
        style = str(dash_cfg.get("section_title_style", "h3")).lower()
        # Discord does NOT render markdown headings inside an embed FIELD.
        # "# ", "## " and "### " work in a message body and in an embed
        # DESCRIPTION -- which is why the "Plex is Online" line can use one --
        # and print literally anywhere else. h1/h2/h3 are therefore not
        # offered here.
        #
        # What does render in a field value: bold, italic, code, links, and
        # subtext. Subtext is the useful one -- small and muted, so it reads
        # as a section marker rather than competing with the bold labels
        # underneath it. Uppercased, it stops looking like body text.
        self._title_prefix, self._title_suffix = {
            "subtext": ("-# ", ""),
            "bold": ("**", "**"),
            "plain": ("", ""),
        }.get(style, ("-# ", ""))
        self._title_upper = bool(dash_cfg.get("section_title_upper", True))
        titles = dash_cfg.get("section_title_text") or {}
        self._titles = {
            "streams": str(titles.get("streams", "Streaming")),
            "bandwidth": str(titles.get("bandwidth", "Bandwidth")),
            "library": str(titles.get("library", "Library")),
            "uptime": str(titles.get("uptime", "Reliability")),
        }
        # An IMAGE divider is not buildable here, and the reason is worth
        # writing down so it does not get re-litigated: an embed has exactly
        # one image slot and Discord always renders it at the BOTTOM, below
        # every field. There is no API surface that places a picture between
        # two fields. Putting a rendered rule under each section would mean
        # one embed per section -- which is the multi-embed, multi-attachment
        # payload the cards-channel split exists to get rid of. The main
        # embed's one image slot is already spent on the width strip.
        #
        # So the rule stays text, and the lever that is actually available is
        # WHICH glyph. Block Elements (U+258x) tile seamlessly and sit on the
        # baseline, which reads much closer to a hairline than box-drawing
        # dashes floating at mid-height.
        style = str(dash_cfg.get("divider_style", "line")).lower()
        glyph, default_width = DIVIDER_GLYPHS.get(
            style, DIVIDER_GLYPHS["line"])
        # NO subtext marker here. Discord renders no markdown at all in a
        # field NAME, so "-# " printed literally -- and at full name size a
        # 66-dash rule wrapped onto a second line. At name size a dash is
        # roughly 9.5px, so ~46 spans the embed's ~484px interior; a block
        # element is about an em wide, so it needs fewer to span the same run.
        try:
            width = int(dash_cfg.get("divider_width", default_width))
        except (TypeError, ValueError):
            width = default_width
        self._divider_text = glyph * max(4, min(80, width))
        self.user_line = bool(dash_cfg.get("user_line", True))
        # Indent for that line, counted in ordinary space widths. The
        # character is a NO-BREAK space (U+00A0) rather than a plain one:
        # same width, but Discord strips a leading run of plain spaces.
        # Em spaces (U+2003) were the v80 unit and read as a full tab stop,
        # which was far too much for a name sitting under a title.
        try:
            indent = int(dash_cfg.get("user_indent", 1))
        except (TypeError, ValueError):
            indent = 1
        self._user_indent = "\u00a0" * max(0, min(8, indent))
        self.online_text = str(dash_cfg.get("online_text", "Plex is Online"))
        self.offline_text = str(dash_cfg.get("offline_text", "Plex is Offline"))
        # Show every counter even at zero, so the line keeps a stable shape
        # instead of items appearing and vanishing between refreshes.
        self.always_show_stats = bool(dash_cfg.get("always_show_stats", True))
        # "-#" is Discord's subtext: smaller, muted. It reads as a caption
        # belonging to the heading instead of body text crowding the fields.
        # Set to "normal" if your client shows a literal "-#".
        self.stats_style = str(dash_cfg.get("stats_style", "subtext")).lower()
        if self.stats_style not in ("subtext", "normal", "fields"):
            self.stats_style = "subtext"

        # Poster cards: one extra embed per stream with the cover art as its
        # thumbnail. Discord allows 10 embeds per message, and the dashboard
        # takes one, so at most 9 cards.
        self.use_builtin_icon = bool(
            self.config.get("dashboard", {}).get("use_builtin_icon", False)
        ) and not self.config.get("dashboard", {}).get("icon_url")

        cards = self.config.get("stream_cards", {})
        self._bar_images = bool(cards.get("bar_image", False)) and cardimage.AVAILABLE
        # The dashboard's full-width colour strip. One file, re-uploaded only
        # when Plex flips online/offline -- so it is independent of the card
        # bars and stays on even when those are text.
        self._width_strip = bool(
            cards.get("width_strip", True)) and cardimage.AVAILABLE
        # Per-card transparent width anchor. Discord sizes an embed to its
        # image, so an anchor on every card makes every card identical --
        # which is what the rendered bars used to do for free. It costs a
        # second attachment slot per card, and Discord caps a message at 10
        # attachments, so this is what drops the page size from 9 to 4.
        # The bytes never change, so it uploads once and is then adopted by
        # URL for the life of the message.
        self._card_anchor = bool(
            cards.get("card_anchor", True)) and cardimage.AVAILABLE
        # What to draw in the anchor. Discord reserves a margin above every
        # embed image, so the anchor costs that space whatever is in it --
        # the only real choice is whether the space is filled.
        #   strip     the dashboard's own accent line, so a card closes the
        #             same way the main embed does (default)
        #   rule      a thinner hairline, for the smallest possible gap
        #   invisible fully transparent; the v76 behaviour
        # "classic" reproduces the image-bar era: the headline carries the
        # title, the detail lines sit in a fixed order under it, and the bar
        # is the last line with no tree branch -- exactly where the rendered
        # PNG used to sit. "tree" is the v90 arrangement where the bar is
        # the closing branch of the tree.
        # Centre the bar in the card. Discord has no alignment of any kind,
        # so this is leading no-break spaces -- computed from the measured
        # gap rather than guessed. Only does anything when an anchor image
        # is holding the embed wider than the bar.
        # Indent for the lines BETWEEN the title and the bar, in space
        # widths. Title and bar stay flush left so they frame the card.
        try:
            ind = int(cards.get("detail_indent", 1))
        except (TypeError, ValueError):
            ind = 1
        ind = max(0, min(8, ind))
        self._detail_indent = "\u00a0" * ind
        # Same indent, but for a "-# " subtext line. Discord trims leading
        # whitespace from subtext CONTENT, which silently ate the plain
        # version -- the zero-width space is not whitespace to that trimmer,
        # so it survives and carries the real spaces in behind it.
        self._detail_indent_sub = ("\u200b" + "\u00a0" * ind) if ind else ""
        # Where the bar sits in a clean-layout card: "bottom" closes the
        # card the way the rendered image used to, "middle" splits identity
        # from timing.
        pos = str(cards.get("bar_position", "bottom")).lower()
        self._bar_position = "middle" if pos == "middle" else "bottom"
        # Keep a placeholder embed on the cards message when nothing is
        # playing, instead of deleting the message. Deleting forces the next
        # stream to SEND, and only a send notifies the channel.
        self.cards_idle_embed = bool(cards.get("idle_embed", True))
        self.cards_idle_text = str(
            cards.get("idle_text", "Nothing playing right now"))
        # Where the percent goes: "line" (the default) gives it a line of
        # its own under Bandwidth -- "Progress · 57%"; "time" puts it on the
        # time line -- "1:00:40 / 1:45:52 · 57% · ends in 45 minutes".
        # Either way the bar takes the room it left as extra segments, so
        # the bar line is still every card's widest and identical, and the
        # cards stay one width. "bar" puts it after the bar in a code span
        # of its own (the only way its digits measure the same at every
        # percentage), which draws as a grey box. (This replaces
        # "bar_percent", which every config carried as true from the
        # defaults; it is no longer read.)
        position = str(cards.get("percent_position", "line")).lower()
        self._percent_position = (
            position if position in ("line", "time", "bar") else "line")
        self._bar_percent = self._percent_position == "bar"
        self._center_bar = bool(cards.get("center_bar", False))
        # Blank lines between the detail block and the bar. The rendered
        # image used to carry its own margin; a text or emoji bar butts
        # straight up against the line above it without this.
        try:
            gap = int(cards.get("bar_gap", 1))
        except (TypeError, ValueError):
            gap = 1
        self._bar_gap = max(0, min(3, gap))
        # Space UNDER the bar. The gap above it comes for free -- a subtext
        # line is short and the emoji's line box is tall, so the two do not
        # meet evenly. Discord has no sub-line spacing, so the smallest
        # thing that can go here is a subtext line holding a zero-width
        # space: shorter than a real blank line, taller than nothing.
        #   0 = none, 1 = subtext-height, 2 = full blank line
        try:
            below = int(cards.get("bar_gap_below", 0))
        except (TypeError, ValueError):
            below = 0
        self._bar_gap_below = max(0, min(2, below))
        layout = str(cards.get("layout", "clean")).lower()
        if layout not in ("clean", "classic", "tree"):
            layout = "clean"
        self._layout = layout
        self._classic_layout = layout == "classic"
        style = str(cards.get("anchor_style", "strip")).lower()
        self._anchor_style = (style if style in ("invisible", "rule", "strip")
                              else "strip")
        if self._bar_images:
            self._card_anchor = False   # the bar already anchors the card
        # How many percent the bar must advance before its image re-renders.
        # Larger = fewer image swaps, smaller = smoother bar. This is not
        # just cosmetic: every swap is an attachment UPLOAD on a message
        # edit, and Discord budgets those per server (error 400009). At 10,
        # a movie re-uploads its bar ~10 times total instead of every few
        # minutes, which keeps the dashboard far inside that budget.
        try:
            quantum = int(cards.get("bar_quantum", 10))
        except (TypeError, ValueError):
            quantum = 10
        self._bar_quantum = max(1, min(25, quantum))
        # How often a bar image may be RE-uploaded. Progress text, ETAs and
        # every other number still refresh on the normal loop; this governs
        # only the pictures, which are what Discord meters hardest. New
        # posters are exempt -- a new stream gets its art immediately.
        try:
            self.image_refresh_seconds = float(
                cards.get("image_refresh_seconds", 900))
        except (TypeError, ValueError):
            self.image_refresh_seconds = 900.0
        self.image_refresh_seconds = max(60.0, min(3600.0,
                                                   self.image_refresh_seconds))
        # Stale image re-uploads allowed per edit. 8-in-one-edit bursts were
        # the measured 429 trigger; a small number rides each minute-edit
        # instead. Hot-reloadable, so it can be tuned against the live 429
        # count without a redeploy.
        try:
            batch = int(cards.get("image_batch", IMAGE_BATCH))
        except (TypeError, ValueError):
            batch = IMAGE_BATCH
        self._image_batch = max(1, min(4, batch))
        # Minimum spacing between image-carrying edits.
        try:
            gap = float(cards.get("image_edit_gap_seconds", IMAGE_EDIT_GAP))
        except (TypeError, ValueError):
            gap = IMAGE_EDIT_GAP
        self._image_gap = max(60.0, min(900.0, gap))
        try:
            max_cards = int(cards.get("max_cards", 9))
        except (TypeError, ValueError):
            max_cards = 9
        # Defined here as well as below: _apply_config runs before
        # cards_channel_id exists on the very first call from __init__.
        # The channel id itself is only read at startup, so once it exists
        # it is the truth: a channel_id added by hot reload must not raise
        # the page size while the cards still render on the dashboard (10
        # cards + the dashboard = 11 embeds, which Discord rejects).
        if hasattr(self, "cards_channel_id"):
            split = bool(self.cards_channel_id)
        else:
            split = bool(cards.get("channel_id") or 0)
        self.stream_cards = {
            "enabled": bool(cards.get("enabled", True)),
            "max_cards": max(1, min(10 if split else 9, max_cards)),
        }
        # Discord hard-caps a message at 10 attachments. In bar-image mode
        # each card costs TWO files (poster + bar) and the dashboard strip
        # costs one, so the page size must fit the attachment budget --
        # missing this is exactly what wedged the dashboard at 5+ streams.
        files_per_card = 2 if (self._bar_images or self._card_anchor) else 1
        # With the cards on their own message, the dashboard's strip and
        # icon are not competing for its 10 attachment slots -- and the
        # main embed is not competing for its 10 embed slots either, so a
        # page can hold ten cards instead of nine.
        overhead = 0 if split else (
            (1 if self._width_strip else 0)
            + (1 if self.use_builtin_icon else 0))
        budget_cap = max(1, (10 - overhead) // files_per_card)
        if self.stream_cards["max_cards"] > budget_cap:
            logger.info(
                "Capping cards per page at %d (attachment budget: 10 files, "
                "%d per card, %d overhead)", budget_cap, files_per_card, overhead,
            )
            self.stream_cards["max_cards"] = budget_cap
        self._bar_autofit = bool(cards.get("bar_autofit", True))
        # Break long titles at the bar's width so every card is the width of
        # the bar, not of its longest title.
        self.wrap_card_titles = bool(cards.get("wrap_titles", True))
        # What to do when a title is too long for the bar:
        #   wrap      EVERY card's title is bold body text, broken at the
        #             bar's width, never cut (default). Uniform weight
        #             across the deck because no card uses the heading
        #             marker -- Discord cannot wrap a heading without
        #             putting a margin between the lines.
        #   truncate  one heading line on every card, tail kept
        #   bold      heading when it fits, plain-bold two lines when it
        #             does not (mixed weight -- kept for the option)
        self.title_wrap = str(cards.get("title_wrap", "wrap")).lower()
        if self.title_wrap not in ("wrap", "truncate", "bold"):
            self.title_wrap = "wrap"
        # Where card artwork comes from:
        #   attach  upload via Tautulli's image proxy (the original way).
        #           Every edit of a message CARRYING attachments is metered
        #           against the shared limit that throttles this bot.
        #   remote  public TMDB URLs from Radarr/Sonarr. Discord fetches
        #           them itself; the message stays text-only and its edits
        #           stay cheap -- upstream's never-rate-limited shape.
        #   off     no artwork.
        self._artwork = str(cards.get("artwork", "attach")).lower()
        if self._artwork not in ("attach", "remote", "off"):
            self._artwork = "attach"
        # Hard override for the fit budget, in pixels. Every value
        # feeding the default is an estimate of a client we cannot
        # measure, so there has to be a way to correct it without a
        # code change. 0 keeps the computed default.
        try:
            self._bar_budget_px = float(cards.get("bar_budget_px", 0) or 0)
        except (TypeError, ValueError):
            self._bar_budget_px = 0.0
        self._fit_bar_segments()
        # Real-time refresh: Tautulli POSTs here the moment a stream starts,
        # stops, pauses or resumes, and the dashboard refreshes within
        # seconds instead of waiting for the next minute tick. The listener
        # binds once at startup; changing the port needs a restart.
        hook = self.config.get("webhook", {}) or {}
        try:
            hook_port = int(hook.get("port", 8322))
        except (TypeError, ValueError):
            hook_port = 8322
        self.webhook = {
            "enabled": bool(hook.get("enabled", True)),
            "port": max(1024, min(65535, hook_port)),
            "secret": str(hook.get("secret", "") or ""),
        }
        try:
            gap = float(hook.get("min_gap_seconds", WEBHOOK_MIN_GAP))
        except (TypeError, ValueError):
            gap = WEBHOOK_MIN_GAP
        self._webhook_gap = max(5.0, min(300.0, gap))

        # Status dot in the channel NAME. Unicode only -- Discord does not
        # support custom emoji in channel names, so the animated dot cannot
        # go here; this is its static sibling.
        dot = self.config.get("channel_dot", {})
        self.channel_dot = {
            "enabled": bool(dot.get("enabled", True)),
            "online": str(dot.get("online", "🟢")),
            "offline": str(dot.get("offline", "🔴")),
        }
        # One zero-width blank line appended to each section, so sections
        # breathe without the old full divider-row gaps.
        self.section_gap = bool(self.config.get("dashboard", {}).get("section_gap", True))

        # Optional panels. total_space reports SABnzbd's download
        # volume, which is usually a scratch disk rather than media storage,
        # so they are off by default.
        panels = self.config.get("panels", {})
        self.panels = {
            "total_space": bool(panels.get("total_space", False)),
            "queue_speed": bool(panels.get("queue_speed", True)),
            "downloads": bool(panels.get("downloads", True)),
            "reliability": bool(panels.get("reliability", True)),
            "legend": bool(panels.get("legend", True)),
            "bandwidth": bool(panels.get("bandwidth", True)),
            # Defaults are the trimmed status embed: totals only.
            "transcode_count": bool(panels.get("transcode_count", False)),
            "direct_play": bool(panels.get("direct_play", False)),
            "wan_lan": bool(panels.get("wan_lan", False)),
            "stream_downloads": bool(panels.get("stream_downloads", False)),
            # Global gate over plex_sections.*.show_episodes: off hides the
            # Episodes / Tracks column for every library.
            "episodes": bool(panels.get("episodes", True)),
            "all_time_uptime": bool(panels.get("all_time_uptime", False)),
            "response": bool(panels.get("response", False)),
            "bot_uptime": bool(panels.get("bot_uptime", False)),
            "last_downtime": bool(panels.get("last_downtime", False)),
            "legend_bar_colours": bool(panels.get("legend_bar_colours",
                                                  False)),
            # One row for the three "right now" figures -- Streams, Total
            # bandwidth, Total Uptime. Desktop Discord packs inline fields
            # three to a row, so three single-value groups each sat alone
            # in a third-width box. Only applies while WAN/LAN is off (with
            # it on, bandwidth is its own full row of three again).
            "live_row": bool(panels.get("live_row", True)),
        }

    def _maybe_reload_config(self) -> None:
        """Hot-reload config.json if it changed since the last tick."""
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime

        fresh = self._load_json(self._config_path, None)
        if not isinstance(fresh, dict):
            logger.error(
                "config.json changed but could not be parsed -- keeping the previous "
                "settings. Fix the JSON (check for a duplicate or trailing comma) and save again."
            )
            return
        previous = self.config
        self.config = normalise_config(fresh)
        self.user_mapping = self._load_json(
            os.path.join(self.data_dir, "user_mapping.json"), {}
        )
        try:
            self._apply_config()
        except Exception:
            # A hot edit that parses as JSON can still be structurally wrong
            # -- "dashboard": null makes every .get() chain raise. This runs
            # as the first statement of the refresh loop, and the exception
            # types it produces are not ones tasks.Loop retries, so letting
            # it escape would kill the dashboard until the container was
            # restarted. Roll back and keep running on the old settings.
            logger.exception(
                "config.json is structurally invalid -- keeping the previous "
                "settings. Check for null values where an object is expected."
            )
            self.config = previous
            try:
                self._apply_config()
            except Exception:
                logger.exception("Could not restore the previous config")
            return
        # Respect any active throttle -- a config reload must not undo the
        # slowdown that is keeping the bot inside Discord's budget.
        try:
            if self.update_dashboard.seconds != self._effective_interval():
                self._apply_interval()
        except Exception:
            pass
        logger.info("Reloaded config.json -- new settings apply from this refresh")

    async def cog_load(self) -> None:
        """Start the webhook listener, if enabled and aiohttp is present."""
        if not (WEB_AVAILABLE and self.webhook["enabled"]):
            if self.webhook["enabled"]:
                logger.warning("Webhook listener unavailable (aiohttp web "
                               "missing) -- realtime refresh disabled")
            return
        try:
            app = web.Application()
            app.router.add_post("/webhook", self._handle_webhook)
            app.router.add_post("/webhook/{secret}", self._handle_webhook)
            self._web_runner = web.AppRunner(app, access_log=None)
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, "0.0.0.0",
                               self.webhook["port"])
            await site.start()
            logger.info(
                "Effective config: build=%s refresh=%ss poll=%ss artwork=%s "
                "cards/page=%d title_wrap=%s arr=%s recent_downloads=%s",
                BUILD, self.refresh_seconds, int(self.change_poll_seconds),
                getattr(self, "_artwork", "attach"),
                self.stream_cards.get("max_cards", 0),
                getattr(self, "title_wrap", "?"),
                ",".join(c.kind for c in getattr(self, "_arr", [])
                         if c.enabled) or "none",
                bool(getattr(self, "recent_enabled", False)))
            logger.info(
                "Realtime webhook listening on port %d -- point a Tautulli "
                "Webhook agent at http://<this host>:%d/webhook",
                self.webhook["port"], self.webhook["port"],
            )
        except OSError as exc:
            # A taken port must not stop the bot -- polling still works.
            logger.error("Could not bind webhook port %d (%s) -- realtime "
                         "refresh disabled, minute polling continues",
                         self.webhook["port"], exc)
            self._web_runner = None

    async def _handle_webhook(self, request):
        """Tautulli playback event: coalesce the burst, then refresh."""
        if self.webhook["secret"]:
            given = request.match_info.get("secret") or ""
            if not hmac.compare_digest(given.encode(),
                                       self.webhook["secret"].encode()):
                return web.Response(status=403)
        self._schedule_webhook_refresh()
        return web.Response(text="ok")

    def _schedule_webhook_refresh(self) -> None:
        # One refresh at a time. A poke while one is pending is remembered
        # and gets one more refresh once it's done, so a burst of events
        # (or a flood of requests) costs at most two edits -- and can never
        # keep pushing the refresh back by restarting it.
        task = self._webhook_task
        if task is not None and not task.done():
            # Still in its short debounce: this poke is part of the same
            # burst and that refresh will see it. Past it, the refresh may
            # already have read Plex, so one more follows.
            if not self._webhook_debouncing:
                self._webhook_again = True
            return
        self._webhook_again = False
        self._webhook_debouncing = True
        self._webhook_task = asyncio.ensure_future(self._webhook_refresh())

    def _webhook_done(self) -> None:
        self._webhook_debouncing = False
        if self._webhook_again:
            self._webhook_task = None
            self._schedule_webhook_refresh()

    async def _webhook_refresh(self) -> None:
        try:
            await asyncio.sleep(WEBHOOK_DEBOUNCE)
            self._webhook_debouncing = False
            deadline = time.monotonic() + WEBHOOK_MAX_WAIT

            # A poke during a rate-limit backoff WAITS the backoff out
            # rather than firing into a guaranteed skip and being lost.
            while True:
                stalled = self._stalled_until - time.monotonic()
                if stalled <= 0 or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(stalled + 0.5,
                                        deadline - time.monotonic()))

            # Rate ceiling: webhooks accelerate the NEXT edit, they do not
            # add edits. Sustained event streams collapse to one edit per
            # gap; the gap itself depends on what the last edit COST, since
            # an upload is worth far more of the budget than a text patch.
            gap = (WEBHOOK_UPLOAD_GAP if self._last_push_had_files
                   else self._webhook_gap)
            if self._last_edit_at:
                remaining = gap - (time.monotonic() - self._last_edit_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)

            # Discord's own headers, not a guess: stop one slot short so the
            # minute refresh always has somewhere to land.
            room = self._rate_headroom()
            if room is not None and room <= WEBHOOK_HEADROOM_RESERVE:
                wait = self._rate_wait()
                if wait > 0:
                    if time.monotonic() + wait > deadline:
                        logger.info("Dropping webhook poke -- bucket empty "
                                    "past the wait deadline (%s)",
                                    self._rate_describe())
                        self._webhook_done()
                        return
                    logger.info("Webhook poke yielding %.1fs -- %d slot(s) "
                                "left in the edit bucket", wait, room)
                    await asyncio.sleep(wait)
            logger.info("Playback changed (webhook) -- refreshing now")
            # Shielded: a newer poke cancels THIS task, but the edit already
            # under way runs to completion and records what it cost.
            await asyncio.shield(self.update_dashboard())
        except asyncio.CancelledError:
            self._webhook_debouncing = False
            return  # the cog is unloading
        except Exception:
            logger.exception("Webhook-triggered refresh failed")
        self._webhook_done()

    def _track(self, coro) -> None:
        if self._closed:
            coro.close()
            return
        task = asyncio.ensure_future(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def cog_unload(self) -> None:
        if self._webhook_task is not None and not self._webhook_task.done():
            self._webhook_task.cancel()
        if self._web_runner is not None:
            try:
                await self._web_runner.cleanup()
            except Exception:
                logger.debug("Webhook listener cleanup failed", exc_info=True)
        # Refuse new work first: a button press dispatched to THIS instance
        # can still be executing after unload, and its edit would race the
        # replacement cog (each instance holds its own render lock, so the
        # two are not serialised against each other).
        self._closed = True
        self.update_dashboard.cancel()
        self.update_presence.cancel()
        self.watch_changes.cancel()
        self.sync_uptime_history.cancel()
        # A pending flip render would otherwise outlive the cog, wake up after
        # the Tautulli session is closed, build a NEW session nobody closes,
        # and edit the dashboard alongside the replacement cog.
        if self._flip_task is not None and not self._flip_task.done():
            self._flip_task.cancel()
        for task in list(self._side_tasks):
            task.cancel()
        # discord.py has no Client.remove_view; stopping the view is what
        # takes it out of the persistent-view store. The old call raised
        # AttributeError here, so every session below leaked on reload.
        try:
            self.pager.stop()
        except Exception:
            logger.debug("pager stop failed", exc_info=True)
        for client in getattr(self, "_arr", []) or []:
            try:
                await client.close()
            except Exception:
                logger.debug("arr close failed", exc_info=True)
        if self.tautulli:
            await self.tautulli.close()
        if self.kuma:
            await self.kuma.close()
        if self.uptimerobot:
            await self.uptimerobot.close()

    # ------------------------------------------------------------- plexapi fb

    async def _get_plex(self):
        """Return a cached PlexServer, connecting only when needed."""
        async with self._plex_lock:
            if self._plex is not None:
                return self._plex
            url, token = os.getenv("PLEX_URL"), os.getenv("PLEX_TOKEN")
            if not url or not token:
                return None
            try:
                from plexapi.server import PlexServer
                self._plex = await asyncio.to_thread(PlexServer, url, token)
                logger.info("Connected to Plex at %s", url)
            except Exception as exc:
                logger.warning("Plex connection failed: %s", exc)
                self._plex = None
            return self._plex

    def _drop_plex(self) -> None:
        """Force a reconnect on the next call."""
        self._plex = None

    # ------------------------------------------------------------ data gather

    async def _fetch_activity(self) -> Optional[Dict[str, Any]]:
        if self.tautulli:
            try:
                return await self.tautulli.get_activity()
            except TautulliError as exc:
                logger.warning("Tautulli activity fetch failed: %s", exc)
                return None
        return await self._fetch_activity_plexapi()

    async def _fetch_activity_plexapi(self) -> Optional[Dict[str, Any]]:
        plex = await self._get_plex()
        if plex is None:
            return None
        try:
            sessions = await asyncio.to_thread(plex.sessions)
        except Exception as exc:
            logger.warning("Plex session fetch failed: %s", exc)
            self._drop_plex()
            return None

        normalised = []
        for session in sessions:
            try:
                normalised.append(self._normalise_plex_session(session))
            except Exception as exc:
                logger.debug("Skipping unparseable session: %s", exc)
        return {
            "count": len(normalised),
            "sessions": normalised,
            "transcode_count": sum(1 for s in normalised if s["decision"] == "transcode"),
            "total_bandwidth": sum(s.get("bitrate", 0) for s in normalised),
        }

    @staticmethod
    def _normalise_plex_session(session) -> Dict[str, Any]:
        media = session.media[0] if getattr(session, "media", None) else None
        player = session.players[0] if getattr(session, "players", None) else None
        transcode = getattr(session, "transcodeSession", None)
        duration = getattr(session, "duration", 0) or 0
        offset = getattr(session, "viewOffset", 0) or 0

        resolution = ""
        if media is not None:
            raw = str(getattr(media, "videoResolution", "") or "")
            resolution = "4K" if raw.lower() in ("4k", "2160") else (f"{raw}p" if raw else "")

        return {
            "media_type": getattr(session, "type", ""),
            "title": getattr(session, "title", "Unknown"),
            "series": getattr(session, "grandparentTitle", "") or "",
            "season": getattr(session, "parentIndex", 0) or 0,
            "episode": getattr(session, "index", 0) or 0,
            "year": str(getattr(session, "year", "") or ""),
            "user": (session.usernames[0] if getattr(session, "usernames", None) else "Unknown"),
            "paused": getattr(player, "state", "") == "paused" if player else False,
            "view_offset": offset,
            "duration": duration,
            "progress": int(offset / duration * 100) if duration else 0,
            "player": (getattr(player, "product", "") or "Unknown").replace("Plex for ", ""),
            "decision": "transcode" if transcode else "direct play",
            "video_decision": "transcode" if transcode else "direct play",
            "audio_decision": "", "subtitle_decision": "", "hw_encode": False,
            "resolution": resolution, "source_resolution": resolution,
            "bitrate": int(getattr(media, "bitrate", 0) or 0) if media else 0,
        }

    async def _fetch_libraries(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if self._library_cache_at and (now - self._library_cache_at).total_seconds() < self._library_interval:
            return self._library_cache

        libraries: List[Dict[str, Any]] = []
        if self.tautulli:
            try:
                libraries = await self.tautulli.get_libraries()
            except TautulliError as exc:
                logger.warning("Tautulli library fetch failed: %s", exc)
        if not libraries:
            libraries = await self._fetch_libraries_plexapi()
        elif any(self._missing_children(lib) for lib in libraries):
            libraries = await self._fill_child_counts(libraries)

        if libraries:
            self._library_cache = libraries
            self._library_cache_at = now
        return self._library_cache

    @staticmethod
    def _missing_children(library: Dict[str, Any]) -> bool:
        """A TV/music library that has items but reports no episodes/tracks.

        Tautulli's child_count goes stale -- observed at 0 episodes beside
        552 shows and 2,788 seasons -- and the Episodes box hides on 0, so
        the column silently vanished rather than showing a wrong number.
        """
        return (library.get("type") in ("show", "artist")
                and (library.get("count") or 0) > 0
                and not library.get("child_count"))

    async def _fill_child_counts(self, libraries):
        """Repair stale episode/track counts. Never raises.

        1. Ask Plex directly (PLEX_URL/PLEX_TOKEN) -- exact, one small
           request per library, and only when Tautulli came back empty.
        2. Otherwise ask Tautulli to refresh its library list (at most once
           an hour) and re-read; that is what repopulates child_count.
        """
        plex = await self._get_plex()
        if plex is not None:
            def count(section_id, kind):
                section = plex.library.sectionByID(int(section_id))
                return section.totalViewSize(
                    libtype="episode" if kind == "show" else "track")
            for lib in libraries:
                if not self._missing_children(lib):
                    continue
                try:
                    lib["child_count"] = int(await asyncio.to_thread(
                        count, lib["section_id"], lib["type"]))
                except Exception as exc:
                    logger.warning("Plex child count for %r failed: %s",
                                   lib.get("name"), exc)
                    self._drop_plex()
            if not any(self._missing_children(lib) for lib in libraries):
                return libraries

        now = time.monotonic()
        # None, not 0.0: monotonic() can be under an hour on a freshly
        # booted host, and "now - 0 < 3600" would block the first refresh.
        last = getattr(self, "_tautulli_refresh_at", None)
        if self.tautulli and (last is None or now - last >= 3600):
            self._tautulli_refresh_at = now
            names = [lib.get("name") for lib in libraries
                     if self._missing_children(lib)]
            logger.warning(
                "Tautulli reports 0 episodes/tracks for %s -- asking it to "
                "refresh its library list (set PLEX_URL/PLEX_TOKEN to read "
                "the count from Plex directly)", ", ".join(map(str, names)))
            try:
                await self.tautulli.call("refresh_libraries_list")
                fresh = await self.tautulli.get_libraries()
                if fresh:
                    return fresh
            except Exception as exc:
                logger.warning("Tautulli library refresh failed: %s", exc)
        return libraries

    async def _fetch_libraries_plexapi(self) -> List[Dict[str, Any]]:
        plex = await self._get_plex()
        if plex is None:
            return []

        def collect():
            out = []
            for section in plex.library.sections():
                # totalSize asks Plex for a count. The original bot called
                # section.all() -- which downloads every item -- and took len().
                entry = {
                    "section_id": str(section.key),
                    "name": section.title,
                    "type": section.type,
                    "count": section.totalSize,
                    "parent_count": 0,
                    "child_count": 0,
                    "is_active": True,
                }
                if section.type == "show":
                    entry["child_count"] = section.totalViewSize(libtype="episode")
                elif section.type == "artist":
                    entry["child_count"] = section.totalViewSize(libtype="track")
                out.append(entry)
            return out

        try:
            return await asyncio.to_thread(collect)
        except Exception as exc:
            logger.warning("Plex library fetch failed: %s", exc)
            self._drop_plex()
            return []

    async def _fetch_uptime(self) -> Dict[str, Any]:
        if self.uptimerobot:
            now = datetime.now(timezone.utc)
            if (self._kuma_cache_at
                    and (now - self._kuma_cache_at).total_seconds()
                    < self._kuma_interval):
                return self._kuma_cache
            self._kuma_cache = await self.uptimerobot.fetch()
            self._kuma_cache_at = now
            return self._kuma_cache
        if not self.kuma:
            return {}
        now = datetime.now(timezone.utc)
        if self._kuma_cache_at and (now - self._kuma_cache_at).total_seconds() < self._kuma_interval:
            return self._kuma_cache
        self._kuma_cache = await self.kuma.fetch()
        self._kuma_cache_at = now
        return self._kuma_cache

    def _sab_enabled(self) -> bool:
        cog = self.bot.get_cog("SABnzbd")
        return bool(cog is not None and getattr(cog, "enabled", False))

    async def _fetch_downloads(self) -> Dict[str, Any]:
        cog = self.bot.get_cog("SABnzbd")
        if not cog:
            return {}
        try:
            data = await cog.get_sabnzbd_info() or {}
        except Exception as exc:
            logger.warning("SABnzbd fetch failed: %s", exc)
            return {}
        # Every queue read is also a chance to notice something finished.
        # Hooked here rather than in the refresh loop so it sees the change
        # poll too -- a download that starts and ends between dashboard
        # edits would otherwise be missed entirely.
        try:
            self._observe_queue(data)
        except Exception:
            logger.debug("Queue completion tracking failed", exc_info=True)
        return data

    # ------------------------------------------------------------- rendering

    def _section_config(self, name: str) -> Optional[Dict[str, Any]]:
        sections = self.config.get("plex_sections", {})
        configured = sections.get("sections", {})
        if name in configured:
            return configured[name]
        return {} if sections.get("show_all", True) else None

    def _ordered_libraries(self, libraries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort libraries to match the order written in config.json.

        Tautulli returns them in its own order, which is why Shows appeared
        before Movies regardless of how the config was written.
        """
        order = list(self.config.get("plex_sections", {}).get("sections", {}).keys())
        if not order:
            return libraries
        rank = {name: index for index, name in enumerate(order)}
        return sorted(libraries, key=lambda lib: rank.get(lib["name"], len(rank)))

    def _emoji(self, key: str) -> str:
        """Look up a configured emoji. Missing or blank means 'render nothing'."""
        configured = self.config.get("emoji", {})
        value = configured.get(key, DEFAULT_EMOJI.get(key, ""))
        return str(value or "")

    @staticmethod
    def _visible_len(text: str) -> int:
        """Approximate what Discord actually DRAWS for a line.

        Padding computed from raw string length was the bug behind uneven
        card widths: "<t:1785702077:R>" is 18 characters of source but
        renders as roughly "in 8 minutes". Markdown syntax and custom emoji
        tags are likewise invisible or far narrower than their source.
        """
        import re as _re
        out = text
        # Relative timestamps -> average rendered width.
        out = _re.sub(r"<t:\d+:[RtTdDfF]>", "x" * 12, out)
        # Custom emoji render at about two characters wide.
        out = _re.sub(r"<a?:\w+:\d+>", "xx", out)
        # Markdown that draws nothing.
        for token in ("```", "**", "__", "-# ", "###", "##", "#", "`", "*", "~~"):
            out = out.replace(token, "")
        return len(out)

    @staticmethod
    def _nowrap(text: str) -> str:
        """Bind a label's words with non-breaking spaces.

        An inline field is a third of the embed, so "Uptime 7 Days" wrapped
        onto two lines and knocked its row out of alignment. NBSP keeps short
        labels on one line.
        """
        return text.replace(" ", "\u00a0")

    def _label(self, key: str, text: str) -> str:
        """Field name with its emoji, if one is configured.

        Upstream puts the emoji AFTER the words -- "Server Uptime 🖥️" -- and
        it reads better than a prefix once values are boxed: the label's
        first character is a letter, so a column of labels starts on a
        straight left edge instead of a ragged one made of glyphs with
        different widths.
        """
        emoji = self._emoji(key)
        if not emoji:
            return text.strip()
        if self.label_emoji_suffix:
            return f"{text} {emoji}".strip()
        return f"{emoji} {text}".strip()

    @classmethod
    def _pack(cls, tokens: List[str], width: int, separator: str = " · ") -> List[str]:
        """Greedily pack tokens into lines no wider than `width`.

        Choosing the break points ourselves means a long detail line splits at
        a separator instead of wherever Discord happens to run out of room.
        """
        lines: List[str] = []
        current = ""
        for token in [t for t in tokens if t]:
            candidate = f"{current}{separator}{token}" if current else token
            # Measure what Discord draws, not the source: "<t:123:R>" is 18
            # characters of markup that renders as about 12.
            if current and cls._visible_len(candidate) > width:
                lines.append(current)
                current = token
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _truncate(text: str, limit: int = MAX_TITLE_LENGTH) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _box(self, value: str) -> str:
        """Contain a stat value so it reads as its own element.

        "pill"  -- each line wrapped in inline code, giving the compact grey
                   capsule look. Lightest, and the default.
        "boxed" -- a fenced code block per value, like the original bot.
                   Heavier, more separation.
        "plain" -- bold text, no container.
        """
        if self.field_style == "boxed":
            return f"```{value}```"
        if self.field_style == "plain":
            return f"**{value}**"
        # Pill: wrap each line separately so multi-line values render as two
        # stacked capsules rather than one capsule containing a newline.
        return "\n".join(f"`{line}`" for line in str(value).split("\n") if line)

    # A field value must stay under Discord's 1024-char cap even after _gap()
    # appends its spacer, so chunking targets this instead.
    GAP_ALLOWANCE = 8

    def _total_uptime_stat(self):
        """Total Uptime as (label, rendered value), or None when unknown."""
        label = self._nowrap(self._label("uptime", "Total Uptime"))
        if self.uptime_relative and self.state.up_since:
            return label, discord_timestamp(self.state.up_since, "R")
        if self.state.uptime_seconds():
            return label, self._box(
                format_duration_long(self.state.uptime_seconds()))
        return None

    def _live_row_on(self) -> bool:
        return (self.panels.get("live_row", True)
                and not self.panels.get("wan_lan", False)
                and self.stats_style == "fields")

    def _gap(self, value: str) -> str:
        """Append a slim spacer line to a section's last content."""
        return f"{value}\n\u200b" if self.section_gap else value

    @staticmethod
    def _pretty_resolution(value: str) -> str:
        """Tautulli reports "4k"/"sd" lowercase; everything else is fine as-is."""
        lowered = value.strip().lower()
        if lowered in ("4k", "2160", "2160p"):
            return "4K"
        if lowered == "sd":
            return "SD"
        return value

    def _card_budget(self) -> float:
        """Pixel width every non-bar line in a card must stay under.

        The progress bar is monospace and identical on every card, so it is
        the one element whose rendered width is both predictable and shared.
        Make it the widest line and every card ends up exactly one bar wide
        -- which is what the transparent anchor PNG was faking, without
        spending an attachment to do it.

        Absolute pixels do not need to be right, only the RATIO: body text
        and code text scale together on every client and display, so a line
        that measures narrower than the bar here measures narrower there.
        """
        # When the bar line carries its percent, the budget has to include
        # it -- and 100% is a character wider than the 85% that was on
        # screen when this wrapped, so budget for the worst case. When the
        # percent is on the time line, the bar has taken its room instead.
        caps = bool(self.bar.cap_left) + bool(self.bar.cap_right)
        widest = textwidth.bar_px(self.bar.segments,
                                  emoji=self.bar.is_emoji, caps=caps) + (
            textwidth.px(" ") + textwidth.code_px("100%")
            if self._bar_percent else 0.0)
        # Margin covers the approximation in the width table (Liberation
        # Sans standing in for gg sans) and the tree-branch prefix.
        return max(120.0, widest - CARD_WIDTH_MARGIN)

    @staticmethod
    def _bar_line_px(renderer, segments: int, caps: int,
                     with_percent: bool = True) -> float:
        """Width of the bar line at `segments`, measured both ways.

        bar_px assumes a monospace cell for anything not emoji, and that is
        a lie for the glyphs this bot actually ships. "▰▱" live in Geometric
        Shapes, which Consolas and Menlo do not cover -- so even inside a
        code span the client falls back to a proportional face and each
        glyph comes out around 13.6px instead of 7.7. A 28-segment bar
        measures 269px under the monospace model and 381px in reality, which
        is why the fit said it was fine and the client wrapped it anyway.

        So render the real string and measure it, and take the LARGER of the
        two models. Underestimating wraps the line; overestimating only
        costs a segment.
        """
        original = renderer.segments
        try:
            renderer.segments = segments
            percent = (textwidth.px(" ") + textwidth.code_px("100%")
                       if with_percent else 0.0)
            if renderer.is_emoji:
                return textwidth.bar_px(segments, emoji=True,
                                        caps=caps) + percent
            drawn = renderer.render(100.0, show_percent=False, inline=False)
            model = textwidth.bar_px(segments, caps=caps) + percent
            actual = textwidth.px(drawn) + percent + (
                textwidth.CODE_CHROME if renderer.inline_code else 0.0)
            # The width table gives "▰" 750/1000 em because that is its
            # metric in Liberation Sans. The client never uses Liberation
            # Sans for it -- gg sans has no Geometric Shapes or Block
            # Elements coverage either, so the glyph resolves through
            # fallback to a face that draws these on a full em square.
            # Measured against the table, a 24-segment bar came to 333px
            # under a 344px budget and still wrapped; at 1em it comes to
            # 429px, which matches what the client actually did.
            wide = sum(textwidth.BODY_PX if _box_glyph(ch)
                       else textwidth.px(ch) for ch in drawn)
            if wide > 0:
                actual = max(actual, wide + percent)
            return max(model, actual)
        finally:
            renderer.segments = original

    def _fit_bar_segments(self) -> None:
        """Shrink the bar until its percent fits on the same line.

        The percent is part of the bar's own line, so the bar cannot be
        sized against the card's full text column -- it has to leave room
        for " 100%", the widest the percent ever gets. When it does not, the
        line wraps and the percent drops underneath, which also breaks width
        matching, since a wrapped card is a taller card.

        Worst case is assumed throughout: poster present (thumbnails steal
        column width) and a three-digit percent. A bar that fits the worst
        case fits everything.
        """
        if not self._bar_autofit:
            return
        for renderer in {id(self.bar): self.bar,
                         id(self.bar_transcode): self.bar_transcode}.values():
            caps = bool(renderer.cap_left) + bool(renderer.cap_right)
            budget = self._bar_budget_px or (
                EMBED_CONTENT_PX - THUMBNAIL_PX
                - textwidth.px(self._detail_indent)
                - CARD_WIDTH_MARGIN)
            segments = renderer.segments
            while segments > 4 and self._bar_line_px(
                    renderer, segments, caps) > budget:
                segments -= 1
            # Logged unconditionally. "No shrink happened" and "the fit never
            # ran" look identical from the outside, and telling them apart by
            # screenshot cost two builds.
            logger.info(
                "Bar fit: %d segments (%.0fpx) vs budget %.0fpx -> %d "
                "segments%s [emoji=%s inline_code=%s]",
                renderer.segments,
                self._bar_line_px(renderer, renderer.segments, caps),
                budget, segments,
                "" if segments == renderer.segments else " SHRUNK",
                renderer.is_emoji, renderer.inline_code)
            if not self._bar_percent:
                # The percent is on the time line: the bar takes the room
                # " 100%" took, so its line is as wide as it was with it.
                target = self._bar_line_px(renderer, segments, caps)
                while self._bar_line_px(renderer, segments + 1, caps,
                                        with_percent=False) <= target:
                    segments += 1
            if segments != renderer.segments:
                renderer.segments = segments
                # min_filled was clamped against the OLD segment count.
                renderer.min_filled = max(0, min(segments,
                                                 renderer.min_filled))

    @staticmethod
    def _headline_parts(session) -> tuple:
        """(title, suffix) for a card headline, markdown-safe.

        Episodes read "Show S01E02" -- season 0 included, so specials keep
        their number. Music reads "Artist – Track": Tautulli puts the artist
        in grandparent_title and album/track numbers where an episode's
        season/episode would be, which used to render "Pink Floyd S01E04"
        and drop the song. Films read "Title (Year)". Titles are escaped, so
        "M*A*S*H" cannot break the bold around it, and an empty title falls
        back to something visible instead of a bare "****".
        """
        esc = discord.utils.escape_markdown
        kind = str(session.get("media_type") or "").lower()
        title = str(session.get("title") or "").strip()
        series = str(session.get("series") or "").strip()
        if kind == "track":
            name = f"{series} \u2013 {title}" if series and title else \
                (title or series)
            return esc(name or "Unknown track"), ""
        if series:
            season, episode = session.get("season"), session.get("episode")
            suffix = ""
            if episode and (season or kind == "episode"):
                suffix = f" S{int(season or 0):02d}E{int(episode):02d}"
            return esc(series), suffix
        year = session.get("year")
        suffix = f" ({year})" if year else ""
        name = title or str(session.get("full_title") or "").strip()
        return esc(name or "Unknown"), suffix

    @property
    def _card_heading(self) -> str:
        """The marker every card title starts with, or "" in wrap mode.

        A heading cannot wrap cleanly: Discord inserts its own margin
        between the lines of a wrapped `###`, so a two-line title reads as
        two separate cards. Wrap mode therefore drops the marker on EVERY
        card -- not just the wrapped ones -- because a deck where long
        titles are body text and short ones are headings is exactly the
        mixed-weight look the cards are meant to avoid.
        """
        return "" if self.title_wrap == "wrap" else "### "

    def _wrap_headline(self, title: str, suffix: str = "",
                       prefix: str = "", line_prefix: str = "") -> str:
        """Break a card title so it never sets the embed's width.

        Measured against the bar, because the bar is the reference width
        every card shares. Each wrapped line is emitted with its own bold
        markers -- Discord does not carry ** across a newline, so a naive
        split leaves the second line unstyled and the asterisks visible.
        """
        headline = f"**{title}**{suffix}"
        if not self.wrap_card_titles:
            one = f"{prefix} {headline}".strip() if prefix else headline
            return f"{line_prefix}{one}"
        caps = bool(self.bar.cap_left) + bool(self.bar.cap_right)
        budget = self._bar_line_px(self.bar, self.bar.segments, caps,
                                   with_percent=self._bar_percent)
        if self.title_wrap == "truncate":
            # Symmetry mode: every card gets exactly one heading line, so
            # the decision must be made at HEADING size. The wrap loop
            # below measures at body size -- correct for the bold two-line
            # form it feeds, but a title can fit the bar at 16px and
            # overflow it at 20px, and those slipped through as client-side
            # wraps in a mixed weight.
            room = budget - (textwidth.px(prefix + " ",
                                          size=textwidth.HEADING_PX)
                             if prefix else 0.0)
            fitted = self._fit_release(f"{title}{suffix}",
                                       max(60.0, room),
                                       size=textwidth.HEADING_PX)
            one = f"**{fitted}**"
            if prefix:
                one = f"{prefix} {one}"
            return f"{line_prefix}{one}"
        first = max(60.0, budget - (textwidth.px(prefix + " ")
                                    if prefix else 0.0))

        # The suffix rides on the final word rather than being a word of
        # its own -- "(2011)" or "S04E15" stranded alone on a second line
        # reads as a mistake, and it is the part that identifies WHICH
        # episode or release this is.
        words = str(title).split(" ")
        if words and suffix:
            words[-1] = f"{words[-1]}{suffix}"
        elif suffix:
            words = [suffix.strip()]
        lines, current, room = [], [], first
        for word in words:
            width = textwidth.px(word, bold=True)
            gap = textwidth.px(" ", bold=True) if current else 0.0
            used = sum(textwidth.px(w, bold=True) for w in current)
            used += textwidth.px(" ", bold=True) * max(0, len(current) - 1)
            if current and used + gap + width > room:
                lines.append(" ".join(current))
                current, room = [word], budget
            else:
                current.append(word)
        if current:
            lines.append(" ".join(current))
        if not lines:
            return f"{line_prefix}{headline}"
        out = [f"**{line}**" for line in lines]
        if prefix:
            out[0] = f"{prefix} {out[0]}"
        if self.title_wrap == "wrap":
            # Uniform mode: no heading marker on ANY card, so a one-line
            # title and a two-line title are the same weight and size.
            # Every line is measured against the bar, so the card is the
            # bar's width whether the title took one line or three.
            return "\n".join(out)
        if len(out) == 1:
            return f"{line_prefix}{out[0]}"
        # A wrapped title cannot use the heading marker: one marked line
        # leaves a smaller bold orphan, both marked puts Discord's
        # uncontrollable heading margin mid-title. And a
        # mixed deck -- some cards with big headings, wrapped ones in plain
        # bold -- breaks the symmetry the cards exist for. So the default
        # is ONE heading line for every card: the title is pixel-fitted to
        # the bar at heading size, tail preserved, so "The SpongeBob Movie:
        # Sponge Out of Water (2015)" becomes "The SpongeBob Movie:
        # Spo... (2015)" rather than two lines in a different weight.
        return "\n".join(out)

    def _bar_with_percent(self, renderer, progress) -> str:
        """The bar and its percent as one fixed-width unit.

        Trailing the percent outside the code span made it proportional
        text, so "5%", "15%" and "100%" were three different widths -- and
        since the bar line is the widest line in a card, every card ended up
        a slightly different width depending on how far along its stream
        was. Inside the span the digits are monospace, and right-aligning
        them to three columns makes the whole line identical at every
        percentage.

        Emoji bars cannot go inside a code span at all (Discord renders the
        raw tag), so those keep the trailing form and pad with figure
        spaces, which are digit-width by definition.
        """
        try:
            pct = max(0.0, min(100.0, float(progress)))
        except (TypeError, ValueError):
            pct = 0.0
        shown = renderer.display_percent(pct)
        bar = renderer.render(pct, show_percent=False, inline=False)
        if renderer.inline_code and not renderer.is_emoji:
            # Inside a code span the font is monospace, so an ordinary space
            # already measures the same as a digit.
            return f"`{bar} {shown:>3}%`"
        # Everywhere else the percent gets a code span of its own. Outside
        # one, its digits are in Discord's gg sans, whose figures are
        # PROPORTIONAL -- "1" narrow, "5" and "6" wide -- so figure-space
        # padding (digit-width in Arial, where the width table comes from)
        # still left "35%" a few pixels wider than "41%". The bar line is a
        # card's widest, so that became a card a few pixels wider, its
        # poster sitting out of line with the cards above. Monospace digits
        # make every percent the same width; it also matches the timing
        # line's code-span times.
        return f"{bar} `{shown:>3}%`"

    @staticmethod
    def _unjumbo(line: str, renderer, percent: str = "") -> str:
        """Stop Discord from enlarging an emoji-only line.

        A line containing nothing but custom emoji gets "jumbo" sizing --
        Discord draws them well above their inline size, and the rules
        differ between desktop and mobile. For a bar built from emoji that
        means segments rendered bigger than intended, unevenly between
        clients, and a bar wider than the column it was measured for.
        Only REAL text disables it. A trailing no-break space
        did nothing: Discord trims trailing whitespace before deciding, so
        the line still counted as emoji-only and kept flipping between
        jumbo and inline sizing between renders -- segments visibly
        changing size on mobile.
        """
        if percent:
            return f"{line} {percent}"
        if not getattr(renderer, "is_emoji", False):
            return line
        return line + "\u200d\u00a0."

    def _clean_lines(self, session: Dict[str, Any],
                     heading: str = "") -> List[str]:
        """Five lines, no tree, bar in the middle.

        Identity above the bar, timing below it, hardware detail demoted to
        subtext. The tree branches were doing real work when every line was
        equal weight; once the bar became the focal point they were just
        noise down the left edge.
        """
        user = self.user_mapping.get(session.get("user", ""),
                                     session.get("user", "Unknown"))
        title, suffix = self._headline_parts(session)

        badges = [self._emoji("paused" if session.get("paused") else "playing")]
        if self.headline_delivery_badge:
            badges.append(self._emoji("transcode"
                                      if session.get("decision") == "transcode"
                                      else "direct"))
        prefix = " ".join(b for b in badges if b)
        # Wrap the title OURSELVES at the bar's width. Discord grows an
        # embed to its widest line before it wraps anything, so a title
        # longer than the bar makes that one card wider than the rest --
        # which is the whole problem the anchor image used to paper over.
        # The client will not break a line early on our behalf, but it does
        # honour a newline we put there.
        headline = self._wrap_headline(title, suffix, prefix,
                                       line_prefix=heading)

        # Who, and on what.
        who = [f"**{discord.utils.escape_markdown(str(user))}**"]
        if session.get("player"):
            who.append(self._truncate(
                str(session["player"]).split(".")[0].strip(), 30))
        pad = self._detail_indent
        sub = self._detail_indent_sub
        lines = [headline, "-# " + sub + " · ".join(who)]

        renderer = (self.bar_transcode
                    if session.get("decision") == "transcode" else self.bar)
        if self._bar_percent:
            bar_line = self._bar_with_percent(renderer,
                                              session.get("progress", 0))
        else:
            bar_line = self._unjumbo(
                renderer.render(session.get("progress", 0),
                                show_percent=False), renderer)
        if self._bar_position == "middle":
            lines.extend([""] * self._bar_gap)
            lines.append(pad + bar_line)
            if self._bar_gap_below == 1:
                lines.append("-# \u200b")
            elif self._bar_gap_below == 2:
                lines.append("")

        progress = session.get("progress", 0)
        try:
            pct_val = max(0.0, min(100.0, float(progress)))
        except (TypeError, ValueError):
            pct_val = 0.0
        timing = []
        if session.get("duration"):
            timing.append(
                f"`{format_hms(session['view_offset'], session['duration'])}"
                f" / {format_hms(session['duration'], session['duration'])}`")
        if self._percent_position == "time":
            timing.append(f"{renderer.display_percent(pct_val)}%")
        if (self.live_eta and session.get("duration")
                and session.get("view_offset") is not None):
            if session.get("paused"):
                if self.paused_text:
                    timing.append(self.paused_text)
            else:
                remaining = session["duration"] - session["view_offset"]
                if remaining > 0:
                    finish = datetime.now(timezone.utc) + timedelta(
                        milliseconds=remaining)
                    timing.append(f"ends {discord_timestamp(finish, 'R')}")
        # Timing stays flush with the title -- those two are the card's
        # left edge, everything else steps in from them.
        lines.append(" · ".join(timing))

        # Everything about HOW it is playing, one weight down.
        detail = TautulliClient.describe_playback(session).split(" · ")
        if detail:
            # Tautulli's label is plain text; the icon and the hardware note
            # are presentation, so both are decided here rather than in the
            # client, which stays a pure description of what Plex reported.
            head = detail[0]
            if not self.show_hw_encode:
                head = head.replace(" (HW)", "")
            icon = self._emoji("transcode"
                               if session.get("decision") == "transcode"
                               else "direct")
            if icon and self.delivery_icon:
                head = f"{icon} {head}"
            detail[0] = head
        if session.get("resolution") and "→" not in " ".join(detail):
            detail.append(self._pretty_resolution(session["resolution"]))
        rate = format_bitrate(session.get("bitrate"))
        # Bitrate on its own line rather than trailing the codec chain. It
        # is the one figure on a card that changes while nothing else does,
        # and buried at the end of "Transcode (HW) · HEVC→H264 · 20.3 Mbps"
        # it read as another codec detail instead of a live measurement.
        if rate and not self.card_bandwidth_line:
            detail.append(rate)
        if detail:
            lines.append("-# " + sub + " · ".join(detail))
        if rate and self.card_bandwidth_line:
            # Emoji leads here regardless of label_emoji_position: this is a
            # sentence, not a column header, so "Bandwidth 📶 · 20.3 Mbps"
            # would put the icon between the word and its value.
            icon = self._emoji("bandwidth")
            label = f"{icon} Bandwidth".strip() if icon else "Bandwidth"
            lines.append(f"-# {sub}{label} · {rate}")
        if self._percent_position == "line":
            # Its own line, worded like Bandwidth's, under it.
            icon = self._emoji("progress")
            label = f"{icon} Progress".strip() if icon else "Progress"
            lines.append(f"-# {sub}{label} · "
                         f"{renderer.display_percent(pct_val)}%")
        if self._bar_position == "bottom":
            lines.extend([""] * self._bar_gap)
            lines.append(pad + bar_line)
        return lines

    def _stream_line(self, session: Dict[str, Any],
                     headline_budget: Optional[int] = None,
                     include_bar: bool = True) -> str:
        """One stream rendered as two lines: title/user, then bar plus detail."""
        # Budget the title and user together. The headline is proportional
        # text so it wraps gracefully, but an untruncated film title plus a
        # long username still runs to three lines on a phone.
        user = self.user_mapping.get(session.get("user", ""), session.get("user", "Unknown"))
        # In image mode (large headline budget) the bar anchors the embed
        # width and long headlines simply wrap -- a tight cap on the name is
        # a leftover from the text-width era and just draws "..." for no
        # gain. Keep the short cap only for the narrow text-mode layout.
        # On its own line the name has the full card width, so the tight
        # inline cap no longer applies.
        user = self._truncate(user, 40 if (self.user_line or (headline_budget or 0) >= 200) else 28)

        title, suffix = self._headline_parts(session)

        budget = headline_budget or HEADLINE_BUDGET
        if include_bar:
            # Text mode: the bar sets the card width, so the title is
            # trimmed to PIXELS under it rather than to a character count
            # that says nothing about how wide "WWW" renders.
            room = self._card_budget()
            if not self.user_line:
                room -= textwidth.px(f" \u2014 {user}", bold=True)
            room -= textwidth.px(suffix, bold=True)
            room -= HEADLINE_BADGE_PX
            # Wrap, do not cut: a two-line title reads fine and costs no
            # width, while "The Office..." loses information for nothing.
            title = textwidth.fit_wrap(title, max(60.0, room), bold=True)
        else:
            spent = len(suffix) if self.user_line else len(user) + len(suffix)
            title = self._truncate(title, max(14, budget - spent))
        headline = (f"**{title}**{suffix}" if self.user_line
                    else f"**{title}**{suffix} — {user}")

        # State and delivery emoji sit on the headline, which is ordinary
        # rich text -- they cannot go inside the code block below.
        badges = [self._emoji("paused" if session.get("paused") else "playing")]
        if self.headline_delivery_badge:
            badges.append(self._emoji(
                "transcode" if session.get("decision") == "transcode"
                else "direct"))
        prefix = " ".join(b for b in badges if b)
        if prefix:
            headline = f"{prefix} {headline}"

        boxed = self.field_style == "boxed"
        # Inside a fenced block the text is already monospace, so the bar must
        # not carry its own backticks. The percentage is rendered separately
        # so the same " · " separator used by the detail lines can sit
        # between bar, percent and position.
        progress = session.get("progress", 0)
        renderer = (self.bar_transcode
                    if session.get("decision") == "transcode" else self.bar)
        bar = renderer.render(progress, show_percent=False, inline=not boxed)
        try:
            pct_val = max(0.0, min(100.0, float(progress)))
        except (TypeError, ValueError):
            pct_val = 0.0
        pct = f"{int(pct_val) if pct_val < 100 else 100}%"

        position = ""
        if session.get("duration"):
            position = (
                f"{format_hms(session['view_offset'], session['duration'])}"
                f" / {format_hms(session['duration'], session['duration'])}"
            )

        detail: List[str] = []
        playback = TautulliClient.describe_playback(session)
        # Split on the separator so a long reason like
        # "Transcode (HW) · subtitles, 4K→1080p" can break across lines
        # instead of being packed as one oversized token.
        parts = playback.split(" · ")
        if parts:
            # Same presentation rules as the clean layout, so the two card
            # styles do not disagree about what a delivery label looks like.
            head = parts[0]
            if not self.show_hw_encode:
                head = head.replace(" (HW)", "")
            icon = self._emoji("transcode"
                               if session.get("decision") == "transcode"
                               else "direct")
            if icon and self.delivery_icon:
                head = f"{icon} {head}"
            parts[0] = head
        detail.extend(parts)
        # Skip the standalone resolution when the transcode summary already
        # spells it out as "4K→1080p".
        if session.get("resolution") and "→" not in playback:
            detail.append(self._pretty_resolution(session["resolution"]))
        rate = format_bitrate(session.get("bitrate"))
        if rate:
            detail.append(rate)
        player_line = ""
        if session.get("player"):
            # Raw Plex player strings are noisy ("LG OLED77C4AUA.DUSQLJR");
            # cut at the first dot.
            player = str(session["player"]).split(".")[0].strip()
            if include_bar:
                # Legacy text mode: player joins the packed detail, tightly
                # truncated because text width sets embed width there.
                detail.append(self._truncate(player, 18))
            else:
                # Image mode: the player gets its own closing line, and can
                # afford to be longer since width comes from the bar image.
                player_line = self._truncate(player, 30)

        eta = ""
        if (
            self.live_eta
            and session.get("duration")
            and session.get("view_offset") is not None
        ):
            if session.get("paused"):
                eta = self.paused_text
            else:
                remaining_ms = session["duration"] - session["view_offset"]
                if remaining_ms > 0:
                    finish = datetime.now(timezone.utc) + timedelta(milliseconds=remaining_ms)
                    eta = f"ends {discord_timestamp(finish, 'R')}"

        if boxed:
            # Title stays outside the block so it keeps its bold styling; the
            # block itself is what separates one stream from the next.
            head = f"{bar} · {pct}"
            first = f"{head} · {position}" if position else head
            body = [first] if len(first) <= self.wide_width else [head, position]
            body += self._pack(detail, self.wide_width)
            return f"{headline}\n```{chr(10).join(l for l in body if l)}```"

        # Tree branches tie the detail lines to their title, so several
        # streams stacked together stay visually grouped.
        lines: List[str] = []
        if self.user_line:
            shown = (textwidth.fit_wrap(user, self._card_budget(), bold=True)
                     if include_bar else user)
            lines.append(f"{self._user_indent}**{shown}**")
        # The bar's backticks are markup, not visible width -- measure the
        # rendered text so the split decision matches what a reader sees.
        if eta and include_bar:
            detail.append(eta)

        if not include_bar:
            # Image mode: the rendered bar image carries progress, so the
            # times and percent become an ordinary first detail line.
            marker = " ⏸" if session.get("paused") else ""
            if position:
                detail.insert(0, f"`{position}` · {pct}{marker}")
            else:
                detail.insert(0, f"{pct}{marker}")
        if include_bar:
            detail_lines = textwidth.pack(
                detail, self._card_budget() - textwidth.px("\u251c "))
        else:
            detail_lines = self._pack(detail, self.wide_width - 2)
        if not include_bar:
            # Fixed layout, not packer's choice: the ETA sits on its own line
            # directly under the progress-time line, on every card.
            if eta:
                detail_lines.insert(min(1, len(detail_lines)), eta)
            if player_line:
                detail_lines.append(player_line)
        if include_bar:
            # Bar last, and it closes the tree. Every detail line branches
            # with |-, the bar gets the terminator, so the card reads as one
            # connected block ending on the thing you actually look at.
            # Percent stays with the bar. Pause state is already on the
            # headline badge, so it is not repeated here -- that width goes
            # to the bar instead.
            if position:
                lines.append(f"├ `{position}`")
            for line in detail_lines:
                lines.append(f"├ {line}")
            lines.append(f"└ {bar} · {pct}")
        else:
            for index, line in enumerate(detail_lines):
                lines.append(
                    f"{'└' if index == len(detail_lines) - 1 else '├'} {line}")
        return f"{headline}\n" + "\n".join(lines)

    def _download_line(self, download: Dict[str, Any]) -> str:
        cog = self.bot.get_cog("SABnzbd")
        name = download.get("name", "Unknown")
        if cog and hasattr(cog, "clean_name"):
            name = cog.clean_name(name)
        try:
            percent = float(download.get("progress", 0))
        except (TypeError, ValueError):
            percent = 0.0

        detail = []
        if download.get("timeleft"):
            detail.append(f"{download['timeleft']} left")
        if download.get("speed"):
            detail.append(str(download["speed"]))
        if download.get("size"):
            detail.append(str(download["size"]))

        # Deliberately NOT tied to field_style. A boxed stat is one short
        # value and the box costs nothing; a boxed download is a name, a
        # bar and three details, so every queued item became a tall slab.
        # The stat values can stay boxed while the queue stays compact.
        boxed = self._downloads_boxed
        bar = self.bar.render(percent, show_percent=False, inline=not boxed)
        pct = f"{int(max(0.0, min(100.0, percent))) if percent < 100 else 100}%"
        head = f"{bar} · {pct}"
        name = self._truncate(name)
        if boxed:
            return f"**{name}**\n```{chr(10).join([head] + self._pack(detail, self.wide_width))}```"
        detail_lines = self._pack(detail, self.wide_width - 2)
        lines = [f"├ {head}"]
        for index, line in enumerate(detail_lines):
            lines.append(f"{'└' if index == len(detail_lines) - 1 else '├'} {line}")
        return f"**{name}**\n" + "\n".join(lines)

    @classmethod
    def _chunk_within_limit(cls, entries: List[str], separator: str = "\n\n",
                            max_chunks: int = 4) -> List[str]:
        """Pack entries into up to max_chunks strings of <=1024 chars each.

        One field caps at 1024 characters (~7 streams), but an embed holds
        6000 across all fields -- so heavy nights spill into continuation
        fields instead of being cut off at seven.

        The budget is FIELD_LIMIT minus GAP_ALLOWANCE because callers append
        a spacer afterwards; packing to exactly 1024 first made Discord
        reject the whole message with 50035 once the spacer was added.
        A single oversized entry is truncated rather than passed through.
        """
        budget = FIELD_LIMIT - cls.GAP_ALLOWANCE
        chunks: List[str] = []
        current: List[str] = []
        length = 0
        for index, entry in enumerate(entries):
            if len(entry) > budget:      # never emit an unbounded value
                entry = entry[: budget - 1].rstrip() + "…"
            addition = len(entry) + (len(separator) if current else 0)
            if current and length + addition > budget:
                chunks.append(separator.join(current))
                current, length = [], 0
                if len(chunks) >= max_chunks:
                    remaining = len(entries) - index
                    chunks[-1] += f"{separator}*+{remaining} more*" if len(chunks[-1]) < budget - 30 else ""
                    return chunks
                addition = len(entry)
            current.append(entry)
            length += addition
        if current:
            chunks.append(separator.join(current))
        return chunks or [""]

    def _join_within_limit(self, entries: List[str], separator: str = "\n\n") -> str:
        return self._chunk_within_limit(entries, separator, max_chunks=1)[0]

    async def build_embed(self, use_cache: bool = False) -> discord.Embed:
        snap = self._snap
        if (
            use_cache and snap
            and (datetime.now(timezone.utc) - snap["at"]).total_seconds() < 90
        ):
            # Page flip: render from the last tick's data. No backend calls,
            # and no state.update -- a flip is not a new observation.
            activity = snap["activity"]
            online = snap["online"]
            libraries = snap["libraries"]
            uptime = snap["uptime"]
            downloads = snap["downloads"]
        else:
            activity = await self._fetch_activity()
            online = activity is not None
            if activity is not None:
                self._last_good_activity = (time.monotonic(), activity)
            if self.tautulli and not online:
                # Distinguish "Tautulli unreachable" from "Plex down".
                try:
                    connected = await self.tautulli.server_status()
                    online = bool(connected)
                except TautulliError:
                    online = False
                if online:
                    # Plex is up but the activity call failed. Rendering
                    # that as "0 streams" flipped the cards to the idle
                    # placeholder mid-movie and reset the pager. Hold the
                    # last good snapshot through a short blip instead.
                    held = getattr(self, "_last_good_activity", None)
                    if held and time.monotonic() - held[0] <= ACTIVITY_HOLD:
                        activity = held[1]
                        logger.info("Activity fetch failed -- holding the "
                                    "last good snapshot (%.0fs old)",
                                    time.monotonic() - held[0])

            self.state.update(online)

            libraries = await self._fetch_libraries() if online else self._library_cache
            uptime = await self._fetch_uptime()
            downloads = await self._fetch_downloads()
            self._snap = {
                "at": datetime.now(timezone.utc), "activity": activity,
                "online": online, "libraries": libraries,
                "uptime": uptime, "downloads": downloads,
            }
            # Warm the poster cache for EVERY session, not just the visible
            # page, so a flip never waits on Tautulli for art.
            sessions_all = (activity or {}).get("sessions") or []
            if sessions_all and self.tautulli and self._artwork == "attach":
                self._track(self._warm_posters(sessions_all))
            # The bot's status shows the same reading as the dashboard.
            try:
                await self._sync_presence(activity, online, libraries)
            except Exception:
                logger.warning("Status update failed", exc_info=True)

        dash = self.config.get("dashboard", {})
        embed = discord.Embed(
            colour=STATUS_COLOURS["online"] if online else STATUS_COLOURS["offline"],
            timestamp=datetime.now(timezone.utc),
        )
        # The status line lives in the DESCRIPTION rather than the title,
        # because custom and animated emoji are only guaranteed to render in
        # an embed's description and field values. Putting an <a:...:id> dot
        # in the title would print the raw tag on some clients.
        dot = self._emoji("online" if online else "offline")
        state = self.online_text if online else self.offline_text
        stats: List[str] = []
        if online:
            count = (activity or {}).get("count", 0)
            stats.append(
                f"{self._emoji('streams')} `{count}` Stream{'s' if count != 1 else ''}".strip()
            )

            transcodes = (activity or {}).get("transcode_count", 0)
            if self.panels["transcode_count"] and (
                    transcodes or self.always_show_stats):
                stats.append(f"{self._emoji('transcode')} `{transcodes}` Transcoding".strip())

            queued = (downloads or {}).get("queued_total") or 0
            if queued or (self.always_show_stats and self.panels["downloads"]
                          and self._sab_enabled()):
                stats.append(
                    f"{self._emoji('downloading')} `{queued}` "
                    f"Download{'s' if queued != 1 else ''}".strip()
                )

        show_stats_here = bool(stats) and not self.stats_in_author
        subtext = self.stats_style == "subtext"

        # "fields" promotes the counters out of the description and into the
        # same inline columns the library counts use: a heading, then the
        # number in a box beneath it. One shape for every statistic on the
        # embed rather than two.
        as_fields = self.stats_style == "fields"
        self._pending_stats: List[tuple] = []
        # Set only when Total Uptime actually lands in the live row, so the
        # uptime block below never loses it (offline, or live_row off).
        self._uptime_in_live_row = False
        if as_fields and online:
            count = (activity or {}).get("count", 0)
            self._pending_stats.append(
                (self._label("streams", f"Stream{'s' if count != 1 else ''}"),
                 format_count(count)))
            transcodes = (activity or {}).get("transcode_count", 0)
            if self.panels["transcode_count"] and (
                    transcodes or self.always_show_stats):
                self._pending_stats.append(
                    (self._label("transcode", "Transcoding"),
                     format_count(transcodes)))
            # Third slot in the streaming row. A download count here said
            # the same thing the Downloading section says a few lines down,
            # and said it about a different subsystem than the two figures
            # beside it. Direct Play belongs with Streams and Transcoding:
            # together the three account for every session exactly once.
            if self.panels.get("direct_play", True):
                self._pending_stats.append(
                    (self._label("direct", "Direct Play"),
                     format_count(max(0, count - transcodes))))
            elif self.panels.get("stream_downloads", False):
                queued = (downloads or {}).get("queued_total") or 0
                if queued or (self.always_show_stats
                              and self.panels["downloads"]
                              and self._sab_enabled()):
                    self._pending_stats.append(
                        (self._label("downloading",
                                     f"Download{'s' if queued != 1 else ''}"),
                         format_count(queued)))
            live_row = self._live_row_on()
            if self.panels.get("bandwidth", True) and not live_row:
                # None breaks the compact line here: counters read as one
                # thought, bandwidth as another. Ignored in column mode,
                # where the row break does the same job.
                self._pending_stats.append(
                    ("__section__", "bandwidth", self._titles["bandwidth"]))
            if self.panels.get("bandwidth", True):
                if self.panels["wan_lan"]:
                    self._pending_stats.append(
                        (self._label("bandwidth", "WAN"),
                         format_bitrate((activity or {}).get("wan_bandwidth", 0))
                         or "0 Mbps"))
                    self._pending_stats.append(
                        (self._label("bandwidth", "LAN"),
                         format_bitrate((activity or {}).get("lan_bandwidth", 0))
                         or "0 Mbps"))
                # Third column completes the bandwidth row rather than
                # introducing a different kind of number: WAN, LAN, and
                # what they add up to. Response time moved down beside the
                # uptime figures, which is the health block.
                self._pending_stats.append(
                    (self._label("bandwidth", "Total"),
                     format_bitrate((activity or {}).get("total_bandwidth", 0))
                     or "0 Mbps"))
            if live_row:
                # Third slot of the live row. Already rendered (boxed, or a
                # relative timestamp that must NOT be boxed), so it rides
                # as a raw item the emitter will not re-box.
                uptime_stat = self._total_uptime_stat()
                if uptime_stat:
                    self._pending_stats.append(("__raw__",) + uptime_stat)
                    self._uptime_in_live_row = True
            show_stats_here = False

        if self.status_style == "big":
            lines = [f"# {dot} {state}".strip()]
            if show_stats_here:
                stat_line = " · ".join(stats)
                lines.append(f"-# {stat_line}" if subtext else stat_line)
        else:
            joined = [f"{dot} **{state}**".strip()] + (stats if show_stats_here else [])
            lines = [" · ".join(joined)]

        if online and self.panels.get("bandwidth", True) and not as_fields:
            total = (activity or {}).get("total_bandwidth", 0)
            if total or self.always_show_stats:
                wan = (activity or {}).get("wan_bandwidth", 0)
                lan = (activity or {}).get("lan_bandwidth", 0)
                # WAN and LAN shown side by side, zeros included -- the pair
                # keeps a stable shape and fills the line.
                if self.panels["wan_lan"]:
                    text = (f"WAN `{format_bitrate(wan) or '0 Mbps'}` · "
                            f"LAN `{format_bitrate(lan) or '0 Mbps'}`")
                else:
                    text = f"Total `{format_bitrate(total) or '0 Mbps'}`"
                bw_line = f"{self._emoji('bandwidth')} {text}"
                # Zero-width space on its own line: a slim gap that separates
                # the bandwidth row from the stats row without the height of
                # a full blank paragraph.
                lines.append("​")
                lines.append(
                    f"-# {bw_line.strip()}"
                    if self.stats_style == "subtext" else bw_line.strip()
                )

        embed.description = "\n".join(lines)

        sessions = (activity or {}).get("sessions") or []
        self._stream_total = len(sessions)
        # Shortest runtime currently streaming drives the image clock, so a
        # half-hour episode gets its bar advanced as often as a long film.
        runtimes = [s.get("duration") or 0 for s in sessions]
        runtimes = [r for r in runtimes if r > 0]
        self._shortest_ms = min(runtimes) if runtimes else 0
        # Drop repaint clocks for streams that ended.
        alive = {str(s.get("session_key") or s.get("poster") or "")
                 for s in sessions}
        self._bar_painted = {k: v for k, v in self._bar_painted.items()
                             if k in alive}
        if online and self.tautulli and self.stream_cards["enabled"] and sessions:
            per_page = self.stream_cards["max_cards"]
            self._total_pages = max(1, -(-len(sessions) // per_page))
            self._card_page = max(0, min(self._card_page, self._total_pages - 1))
            start = self._card_page * per_page
            self._card_sessions = sessions[start:start + per_page]
            # The page actually SLICED here, remembered so the pager label
            # always describes the rendered cards. A button press landing
            # mid-render mutates _card_page, and syncing the label from that
            # would caption this render with the next render's page number.
            self._page_rendered = self._card_page
            # Warm the poster cache for the pages the user has NOT opened.
            # The first flip to a page used to sit waiting on Tautulli for
            # art nobody had asked for yet; fetching it in the background
            # while the current page is on screen makes that flip a cache
            # hit. Fire-and-forget: a failure here costs nothing, the flip
            # just fetches as it used to.
            if self._total_pages > 1 and self._artwork == "attach":
                offpage = [s.get("poster") for s in sessions
                           if s not in self._card_sessions and s.get("poster")]
                if offpage:
                    self._track(self._prefetch_posters(offpage))
        else:
            self._card_sessions = []
            self._total_pages = 1
            self._card_page = 0
            self._page_rendered = 0

        if online:
            # Fetched here because _add_online_fields is synchronous and
            # the panel belongs in the middle of it, not appended after.
            self._recent_pending = (
                await self._recent_items()
                if (self.recent_enabled and self.recent_in_main) else None)
            self._add_online_fields(embed, activity or {}, libraries, uptime, downloads)
        else:
            self._add_offline_fields(embed, uptime)

        if dash.get("name"):
            author = dash["name"]
            if self.stats_in_author and stats:
                author = f"{author} · {' · '.join(stats)}"
            # Author name caps at 256 characters and renders as plain text.
            embed.set_author(name=author[:256], icon_url=dash.get("icon_url") or None)
        if dash.get("icon_url"):
            embed.set_thumbnail(url=dash["icon_url"])
        # Footers are plain text -- Discord renders no markdown or live
        # timestamps here, and always pins the clock ("Today at ...") to the
        # end. So this is a STATIC interval note refreshed by each edit; the
        # ticking countdown lives on the description's last line.
        interval = self._effective_interval()
        # What this used to report was refresh_seconds -- the SCHEDULED loop,
        # which since the change-driven rework is only the backstop. Saying
        # "next update ~5 min" while a webhook was landing edits within
        # seconds was simply untrue. When an event source is live, say so and
        # quote the poll that bounds the worst case instead.
        if self.webhook.get("enabled") or self.change_poll_seconds:
            worst = self.change_poll_seconds or interval
            cadence = f"live · checks every {int(worst)}s"
        else:
            cadence = (f"next update ~{int(interval)}s" if interval < 120
                       else f"next update ~{interval / 60:.0f} min")
        embed.set_footer(
            text=f"Last updated · {BUILD} · {cadence}",
            icon_url=dash.get("footer_icon_url") or None,
        )

        # Last line of defence: Discord rejects the entire message over 25
        # fields, which would otherwise wedge the dashboard permanently.
        # Row padders are the least useful thing here, so drop those first.
        while len(embed.fields) > MAX_EMBED_FIELDS:
            index = next(
                (i for i, f in enumerate(embed.fields) if f.name == "​"),
                len(embed.fields) - 1,
            )
            embed.remove_field(index)
            logger.warning("Trimmed an embed field to stay within Discord's 25-field limit")
        return embed

    def _add_online_fields(self, embed, activity, libraries, uptime, downloads) -> None:
        """Inline columns give the embed its width -- Discord sizes an embed
        to its content, and a single narrow column lets it collapse. The old
        gap problem came from heading fields with blank values, not from the
        columns, so the headings stay dead and the columns come back.
        """
        # --- activity counters, when stats_style is "fields" ---
        if getattr(self, "_pending_stats", None):
            self._section_title(embed, "streams", self._titles["streams"])
        self._begin_stats()
        for item in getattr(self, "_pending_stats", []):
            if item is None:
                self._pad_row(embed)      # flush this group, start the next
                self._begin_stats()
                continue
            if len(item) == 3 and item[0] == "__raw__":
                self._add_stat(embed, item[1], self._gap(item[2]))
                continue
            if len(item) == 3 and item[0] == "__section__":
                # Group boundary AND its heading, in one marker.
                self._pad_row(embed)
                self._section_title(embed, item[1], item[2])
                self._begin_stats()
                continue
            name, value = item
            self._add_stat(embed, self._nowrap(name),
                           self._gap(self._box(value)))
        if getattr(self, "_pending_stats", None):
            # Close the row so the library columns start a fresh one and
            # every group keeps the same spacing.
            self._pad_row(embed)
            self._divider(embed)

        # --- library: side-by-side columns ---
        shown = 0
        hidden = 0
        self._section_title(embed, "library", self._titles["library"])
        self._begin_stats()
        for library in self._ordered_libraries(libraries):
            config = self._section_config(library["name"])
            if config is None:
                continue
            if shown >= MAX_LIBRARY_FIELDS:
                hidden += 1
                continue
            label = config.get("display_name", library["name"])
            emoji = config.get("emoji", "")
            self._add_stat(embed, self._nowrap(f"{emoji} {label}".strip()),
                           self._gap(self._box(format_count(library["count"]))))
            shown += 1
            # Episodes (or tracks) get their own column beside the parent
            # library, mirroring how Movies and Shows sit side by side.
            show_children = config.get(
                "show_episodes", library["type"] in ("show", "artist")
            )
            if (library.get("child_count") and show_children
                    and self.panels["episodes"]):
                is_music = library["type"] == "artist"
                child_name = self._label(
                    "tracks" if is_music else "episodes",
                    "Tracks" if is_music else "Episodes",
                )
                self._add_stat(
                    embed, self._nowrap(child_name),
                    self._gap(self._box(format_count(library["child_count"]))))
                shown += 1
        if hidden:
            logger.warning(
                "%d libraries hidden -- Discord allows only 25 embed fields. "
                "Narrow plex_sections.sections in config.json.", hidden,
            )
        self._pad_row(embed, shown)

        self._divider(embed)

        # --- now playing ---
        sessions = activity.get("sessions") or []
        if self._card_sessions:
            # The poster cards below the dashboard carry the stream detail;
            # streams beyond the current page are reachable via the pager
            # buttons, so nothing needs listing here.
            pass
        elif sessions:
            chunks = self._chunk_within_limit(
                [self._stream_line(s) for s in sessions]
            )
            for index, chunk in enumerate(chunks):
                is_last = index == len(chunks) - 1
                embed.add_field(
                    name=self._label("now_playing", "Now Playing") if index == 0 else "\u200b",
                    value=self._gap(chunk) if is_last else chunk,
                    inline=False,
                )
        elif self.stream_cards["enabled"]:
            # The cards ARE the now-playing display. Left as-is this field
            # only ever appeared when nothing was playing -- a section whose
            # entire content was "nothing here", and with the cards on their
            # own channel it was the only thing it ever said.
            pass
        else:
            self._titled_field(
                embed, "now_playing", "Now Playing",
                self._gap(f"{self._emoji('idle')} *Nothing playing right now*".strip()))

        self._divider(embed)

        # --- downloads (queue speed folded in as the last line) ---
        if self.panels["downloads"] and self._sab_enabled():
            queue = downloads.get("downloads") or []
            if queue:
                total_queued = downloads.get("queued_total") or len(queue)
                body = self._join_within_limit([self._download_line(d) for d in queue])
                tail: List[str] = []
                if self.panels["queue_speed"] and downloads.get("speed"):
                    tail.append(f"{self._emoji('queue_speed')} `{downloads['speed']}` total".strip())
                if self.panels["total_space"] and downloads.get("diskspacetotal1"):
                    tail.append(f"`{downloads['diskspacetotal1']}` disk")
                if tail:
                    # The body is already packed to the field budget, so the
                    # tail has to be accounted for rather than just appended.
                    suffix = "\n" + " · ".join(tail)
                    room = FIELD_LIMIT - self.GAP_ALLOWANCE - len(suffix)
                    if len(body) > room:
                        body = body[: max(0, room - 1)].rstrip() + "…"
                    body += suffix
                self._titled_field(
                    embed, "downloading",
                    f"Downloading — {total_queued}"
                    + (f" (showing {len(queue)})" if total_queued > len(queue)
                       else ""),
                    self._gap(body))
            else:
                self._titled_field(
                    embed, "downloading", "Downloading",
                    self._gap(f"{self._emoji('idle')} *Queue is empty*".strip()))
        self._emit_recent_field(embed)

        self._divider(embed)

        # --- reliability: uptime and last outage, then the windows ---
        #
        # Rendered as Discord relative timestamps rather than baked text, so
        # the client counts them itself -- the same mechanism the stream
        # cards use for "ends in 12 minutes". The value is right between
        # edits instead of only at the moment one lands, which matters more
        # now that edits are driven by events rather than a clock.
        self._section_title(embed, "uptime", self._titles["uptime"])
        self._begin_stats()
        # A Discord relative timestamp cannot live inside a code block --
        # the client only substitutes it in plain text, so a boxed one would
        # print "<t:1234:R>" literally. With boxed values that makes Total
        # Uptime the one unboxed figure in a row of boxes, so this exists to
        # trade the live counter for visual consistency.
        if not getattr(self, "_uptime_in_live_row", False):
            uptime_stat = self._total_uptime_stat()
            if uptime_stat:
                self._add_stat(embed, *uptime_stat)
        if self.panels.get("all_time_uptime", True):
            # UptimeRobot's all-time ratio, returned on every call and
            # discarded until now. Sits naturally beside Total Uptime: one
            # is how long the current streak is, the other is the record it
            # belongs to.
            all_time = (uptime or {}).get("uptime_1y")
            if all_time is not None:
                self._add_stat(
                    embed, self._nowrap(self._label("all_time", "All Time")),
                    self._box(f"{float(all_time):.2f}%"))
        if self.panels.get("response", True):
            ms = (uptime or {}).get("response_ms")
            if ms is not None:
                try:
                    self._add_stat(
                        embed,
                        self._nowrap(self._label("response", "Response")),
                        self._box(f"{int(ms)} ms"))
                except (TypeError, ValueError):
                    pass
        # Off by default: Total Uptime, All Time and Response already fill
        # the row exactly, and a fourth would sit alone on a line of its own.
        if self.panels.get("bot_uptime", False):
            # How long THIS process has run. Distinct from server uptime and
            # worth having: a dashboard that looks wrong reads very
            # differently once you can see the bot restarted ten minutes ago.
            self._add_stat(
                embed, self._nowrap(self._label("bot_uptime", "Bot Up")),
                discord_timestamp(self._started_at, "R"))
        if self.panels.get("last_downtime", False):
            # HOW LONG it was down, not when. "When" is the same instant as
            # the start of the current uptime -- an outage ends exactly
            # where the uptime begins -- so a timestamp here would have sat
            # beside Total Uptime saying the same thing twice.
            outage = ""
            if self.state.last_down and self.state.up_since:
                seconds = (self.state.up_since
                           - self.state.last_down).total_seconds()
                if seconds > 0:
                    outage = self._box(format_duration_long(int(seconds)))
            if not outage and self.state.last_down:
                # Down now, or a recovery we never saw: fall back to when.
                outage = discord_timestamp(self.state.last_down, "R")
            self._add_stat(
                embed,
                # NOT the "offline" emoji: that one is animated in this
                # config, and Discord renders an animated tag as raw text in
                # a field NAME on some clients.
                self._nowrap(self._label("last_downtime", "Last Outage")),
                outage or "*None recorded*")
        self._pad_row(embed)
        if self.panels["reliability"]:
            self._add_reliability_columns(embed, uptime)

        self._add_legend(embed)

    def _add_offline_fields(self, embed, uptime) -> None:
        snapshot = self.state.snapshot()
        down_for = self.state.downtime_seconds()
        value = f"`{format_duration(down_for) if down_for else 'Just now'}`"
        if snapshot["down_since"]:
            value += f" · since {discord_timestamp(snapshot['down_since'], 'f')}"
        # Not the "offline" emoji: it is an animated custom emoji in this
        # config, and field NAMES show animated tags as raw text.
        embed.add_field(name=self._label("last_downtime", "Offline For"),
                        value=self._gap(value), inline=False)
        self._add_reliability_columns(embed, uptime)
        self._add_legend(embed)

    def _add_reliability_columns(self, embed, uptime) -> None:
        """The three uptime windows as side-by-side columns."""
        if not uptime:
            return
        windows = [("24h", "uptime_24h"), ("7d", "uptime_7d"), ("30d", "uptime_30d")]
        available = [
            (label, uptime.get(key)) for label, key in windows
            if uptime.get(key) is not None
        ]
        self._begin_stats()
        for label, value in available:
            # Built through _label like every other stat, so the emoji lands
            # on the configured side instead of always leading.
            self._add_stat(
                embed,
                self._nowrap(self._label("uptime_stats", f"Uptime {label}")),
                self._box(f"{value:.2f}%"))
        self._pad_row(embed)

    def _add_legend(self, embed) -> None:
        """One subtext line decoding the colors: dots and bar fills."""
        if not self.panels.get("legend", True):
            return
        parts = [
            f"{self._emoji('online')} Online",
            f"{self._emoji('offline')} Offline",
        ]
        # The green/amber squares decode a stream card's progress bar fill.
        # Since the channel split those cards are not on this message, so
        # here the key explained something the reader cannot see. Default off
        # for that reason; turn it on if the cards ever move back.
        if self.panels.get("legend_bar_colours", False):
            parts += ["🟩 Direct Play", "🟨 Transcoding"]
        embed.add_field(
            name="\u200b",
            value="-# " + "  ·  ".join(p.strip() for p in parts if p.strip()),
            inline=False,
        )

    def _section_title(self, embed, key: str, text: str) -> None:
        """A named heading for a group, the way "Downloading" already reads.

        Replaces the rule. A divider is a horizontal object competing with a
        layout Discord will not let us control the width of -- it either
        falls short of the columns or wraps on a phone. A title has no width
        to get wrong: it says what the group is, and the group starting is
        the separation.
        """
        if not self.section_titles:
            return
        if len(embed.fields) >= MAX_EMBED_FIELDS - 2:
            return  # never spend the last slots on decoration
        label = self._title_label(key, text)
        if not self._title_prefix:
            embed.add_field(name=label, value="​", inline=False)
            return
        # A field NAME renders no markdown at all, so a title written there
        # comes out in exactly the same bold as "Stream" and "Transcoding"
        # underneath it -- a heading indistinguishable from its own contents.
        # Markdown only works in a VALUE, so the title goes there and the
        # name becomes the empty line. That blank also does useful work:
        # it is the space between the previous group and this heading.
        embed.add_field(
            name="​",
            value=f"{self._title_prefix}{label}{self._title_suffix}",
            inline=False)

    def _title_label(self, key: str, text: str) -> str:
        """Section heading text: emoji placement, plus optional uppercasing.

        Uppercase is what stops a subtext heading reading as a stray line of
        body text -- at subtext size, sentence case just looks like a small
        caption.
        """
        return self._label(key, text.upper() if self._title_upper else text)

    def _titled_field(self, embed, key: str, title: str, body: str) -> None:
        """A section that owns its content, heading folded into the value.

        Stat groups need the title as a field of its own because their
        content is a row of inline fields. A prose section does not, and
        must not: a separate title field would put Discord's field padding
        between the heading and the text it introduces, which is the gap
        that made titles look like they were floating loose. Heading
        and body in ONE value sit on consecutive lines.
        """
        if not (self.section_titles and self._title_prefix):
            embed.add_field(name=self._label(key, title), value=body,
                            inline=False)
            return
        label = self._title_label(key, title)
        head = f"{self._title_prefix}{label}{self._title_suffix}\n"
        if len(head) + len(body) > FIELD_LIMIT:
            body = body[: max(0, FIELD_LIMIT - len(head) - 1)].rstrip() + "…"
        embed.add_field(name="​", value=head + body, inline=False)

    def _divider(self, embed) -> None:
        """A thin rule between field groups, if enabled and there is room."""
        if not self.section_divider:
            return
        if len(embed.fields) >= MAX_EMBED_FIELDS - 2:
            return  # never spend the last slots on decoration
        if not embed.fields:
            return  # nothing above it to divide from
        if embed.fields[-1].name == self._divider_text:
            # The section between them rendered nothing -- with the cards on
            # their own message the now-playing block is skipped entirely,
            # which would otherwise stack two rules together.
            return
        # Rule on the NAME line, not the value. A field renders its name
        # above its value, so putting the rule in the value left a blank
        # name line sitting above it and the mark floated in the middle of
        # the gap instead of closing the section.
        embed.add_field(name=self._divider_text, value="\u200b", inline=False)

    def _begin_stats(self) -> None:
        """Start a run of inline stat fields."""
        self._stat_emitted = 0
        self._compact_parts = []

    def _flush_compact(self, embed, heading: str = "\u200b") -> None:
        """Emit the pending compact group as one subtext line."""
        if not getattr(self, "_compact_parts", None):
            return
        if self._tight:
            # Full-size text, not subtext: the group should still read as the
            # same row of pills it was in columns mode, just without the
            # thirds. NBSP rather than plain spaces -- Discord collapses a
            # run of ordinary spaces, so the separation would vanish.
            value = "   ".join(self._compact_parts)
        else:
            value = "-# " + " · ".join(self._compact_parts)
        embed.add_field(name=heading, value=value, inline=False)
        self._compact_parts = []

    def _add_stat(self, embed, name: str, value: str) -> None:
        """One inline stat, honouring stat_columns.

        Discord always packs three inline fields per row and offers no
        column control, so two-column mode is produced by slipping a blank
        field in after every pair -- the blank takes the third slot and the
        row breaks where we want it.
        """
        if self._compact:
            # Strip the box so a run of them reads as one line rather than
            # a row of pills.
            self._compact_parts = getattr(self, "_compact_parts", [])
            part = f"{name} {value}".replace("\n\u200b", "").strip()
            if self._tight:
                # Bind each pill's own words so a group wraps BETWEEN items
                # rather than through the middle of one.
                part = part.replace(" ", "\u00a0")
            self._compact_parts.append(part)
            return
        embed.add_field(name=name, value=value, inline=True)
        self._stat_emitted += 1
        if self._stat_columns == 2 and self._stat_emitted % 3 == 2:
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            self._stat_emitted += 1

    def _pad_row(self, embed, count: int = 0) -> None:
        """Fill the current row out so the next group starts on a new one."""
        if self._compact:
            self._flush_compact(embed)
            return
        while self._stat_emitted % 3:
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            self._stat_emitted += 1

    # ----------------------------------------------------------------- loops

    async def _warm_posters(self, sessions) -> None:
        try:
            await asyncio.gather(
                *[self._get_poster(s.get("poster", "")) for s in sessions],
                return_exceptions=True,
            )
        except Exception:
            pass

    async def _prefetch_posters(self, paths: List[str]) -> None:
        """Pull off-page artwork into the cache, quietly."""
        wanted = [p for p in dict.fromkeys(paths)
                  if p and p not in self._poster_cache]
        if not wanted:
            return
        try:
            await asyncio.gather(*(self._get_poster(p) for p in wanted),
                                 return_exceptions=True)
            logger.debug("Prefetched %d off-page poster(s)", len(wanted))
        except Exception:  # pragma: no cover - best effort by design
            pass

    async def _get_poster(self, path: str) -> Optional[bytes]:
        if not path or not self.tautulli:
            return None
        if path in self._poster_cache:
            return self._poster_cache[path]
        data = await self.tautulli.get_image(path)
        if data:
            # Modest LRU: posters are ~20-40 KB. Sized to hold every page's
            # art at once -- an entry evicted between flips is a flip that
            # goes back to Tautulli, which is what the prefetch is for.
            if len(self._poster_cache) >= 80:
                self._poster_cache.pop(next(iter(self._poster_cache)))
            self._poster_cache[path] = data
        return data

    async def _remote_poster(self, session) -> Optional[str]:
        """Public poster URL for a session, or None.

        Sonarr is consulted for series, Radarr for films, both as a
        fallback. A miss simply means no artwork on that card -- falling
        back to an attachment would put files back on the message, which is
        the exact cost this mode exists to remove.
        """
        is_series = bool(session.get("series"))
        name = session.get("series") or session.get("title") or ""
        keys = self._poster_keys(session)
        if not keys:
            return None
        ordered = sorted(self._arr,
                         key=lambda c: c.kind != ("sonarr" if is_series
                                                  else "radarr"))
        for client in ordered:
            if not client.enabled:
                continue
            try:
                posters = await client.poster_map()
            except Exception:
                continue
            for key in keys:
                url = posters.get(key)
                if url:
                    return url
        # Once per title, not per tick: the miss is a library/spelling
        # question the operator may want to answer, not a fault to spam.
        misses = self.__dict__.setdefault("_poster_misses", set())
        if name not in misses:
            misses.add(name)
            logger.info("No remote poster for %r (tried %s)", name,
                        ", ".join(keys[:6]))
        return None

    @staticmethod
    def _poster_keys(session) -> List[str]:
        """Lookup keys for a session's poster, most reliable first.

        Provider IDs lead: Tautulli hands over Plex's own guids
        ("tmdb://1234", "tvdb://5678", "imdb://tt..."), and Radarr/Sonarr
        index their libraries by the same numbers, so an ID hit is immune
        to every spelling difference two systems can have. Titles follow,
        in every variant the *arr side might use, year-qualified first.
        """
        is_series = bool(session.get("series"))
        keys: List[str] = []
        if str(session.get("media_type") or "").lower() == "track":
            return keys             # music has no Radarr/Sonarr poster
        guids = session.get("grandparent_guids" if is_series else "guids") \
            or []
        if isinstance(guids, str):
            guids = [guids]
        for guid in guids:
            text = str(guid or "")
            provider, sep, ident = text.partition("://")
            if not sep or not ident:
                continue
            provider = provider.lower()
            if provider in ("tmdb", "tvdb", "imdb"):
                keys.append(f"id:{provider}:{ident.strip()}")
        # An episode's "year" is when THAT EPISODE aired, not when the show
        # began, so pairing it with the show title found the wrong series
        # (or none). Only films get a year-qualified key.
        year = None if is_series else session.get("year")
        titles = [session.get("series") or session.get("title"),
                  session.get("original_title")]
        seen = set(keys)
        for title in titles:
            for variant in arrapi.ArrClient._title_variants(title):
                for key in ((f"{variant}{year}",) if year else ()) + (variant,):
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
        return keys

    async def _build_stream_cards(self):
        """Build card embeds plus the message's desired attachment set.

        Returns (cards, desired): desired is (filename, key, bytes) for every
        attachment the message should carry. _compose diffs keys against the
        previous tick so a file re-uploads only when its content changed;
        everything else keeps the CDN URL Discord already assigned (no
        re-upload, no flicker).
        """
        cards: List[discord.Embed] = []
        desired: List[tuple] = []
        self._refs = []

        use_icon = self.use_builtin_icon and os.path.exists(self._icon_path)
        if use_icon:
            with open(self._icon_path, "rb") as handle:
                desired.append(("plexicon.png", "icon", handle.read()))

        # Fetch this page's posters concurrently (cache hits are instant).
        # Bind the session list ONCE. Re-reading self._card_sessions after the
        # await below would let a page flip landing mid-fetch pair the new
        # page's sessions with the old page's poster bytes -- and that wrong
        # pairing then gets cached as correct.
        card_sessions = list(self._card_sessions)
        bar_sessions: Dict[str, tuple] = {}
        # Only the attach mode uploads Tautulli's art. In remote/off mode
        # these downloads were fetched every tick and thrown away.
        posters = await asyncio.gather(
            *[self._get_poster(s.get("poster", "")) for s in card_sessions],
            return_exceptions=True,
        ) if card_sessions and self._artwork == "attach" else []

        for index, session in enumerate(card_sessions):
            colour = (
                STATUS_COLOURS["degraded"]
                if session.get("decision") == "transcode"
                else STATUS_COLOURS["online"]
            )

            if self._bar_images:
                # Image mode: a fixed-width rendered bar anchors the card at
                # an exact pixel width on every client, so titles need no
                # truncation -- a genuinely huge title wraps, never cut.
                lines = self._stream_line(
                    session, headline_budget=300, include_bar=False
                ).split("\n")
                lines[0] = f"{self._card_heading}{lines[0]}"
            elif self._layout == "clean":
                # The heading marker rides inside _clean_lines now, applied
                # per wrapped line -- prefixing lines[0] here styled only
                # the first line of a wrapped title.
                lines = self._clean_lines(session,
                                          heading=self._card_heading)
            elif self._classic_layout:
                # The image-bar layout, with the bar drawn from text or
                # emoji instead of uploaded. include_bar=False gives the
                # exact line ordering the rendered-bar era used; the bar is
                # then appended with no branch, so it starts flush left the
                # way a full-width image did.
                lines = self._stream_line(
                    session, headline_budget=300, include_bar=False).split("\n")
                lines[0] = f"{self._card_heading}{lines[0]}"
                renderer = (self.bar_transcode
                            if session.get("decision") == "transcode"
                            else self.bar)
                bar_line = renderer.render(
                    session.get("progress", 0), show_percent=False)
                if self._center_bar and (self._card_anchor or self._bar_images):
                    caps = bool(renderer.cap_left) + bool(renderer.cap_right)
                    width = textwidth.bar_px(renderer.segments,
                                             emoji=renderer.is_emoji, caps=caps)
                    # NOT `poster` -- that is assigned further down the
                    # loop, so reading it here would size this card against
                    # the PREVIOUS card's artwork.
                    art = posters[index] if index < len(posters) else None
                    has_art = (self._artwork == "remote"
                               or (bool(art)
                                   and not isinstance(art, Exception)))
                    column = EMBED_CONTENT_PX - (THUMBNAIL_PX if has_art else 0.0)
                    pad = int(max(0.0, (column - width) / 2) / NBSP_PX)
                    if pad:
                        bar_line = "\u00a0" * pad + bar_line
                lines.extend([""] * self._bar_gap)
                lines.append(bar_line)
            else:
                # Tree mode: the bar is the closing branch of the card.
                lines = self._stream_line(session).split("\n")
                lines[0] = f"{self._card_heading}{lines[0]}"

            card = discord.Embed(description="\n".join(lines), colour=colour)

            if self._artwork == "off":
                poster = None
            elif self._artwork == "remote":
                poster = None
                url = await self._remote_poster(session)
                if url:
                    card.set_thumbnail(url=url)
            else:
                poster = posters[index] if index < len(posters) else None
            if isinstance(poster, Exception):
                poster = None
            if poster:
                # Named by IDENTITY, not by slot. Positional names
                # (poster0.jpg = "whatever is in slot 0") meant one stream
                # ending shifted every slot and invalidated every file, so a
                # single join or leave re-uploaded the whole page -- at 10
                # concurrent sessions with normal churn that alone rivals
                # the upload volume that originally exhausted the server's
                # attachment budget. Named by content, an unchanged movie's
                # poster stays valid whatever slot it lands in, and a churn
                # event uploads exactly the art that is actually new.
                digest = hashlib.md5(
                    str(session.get("poster", "")).encode()).hexdigest()[:10]
                name = f"p_{digest}.jpg"
                if name not in {d[0] for d in desired}:
                    desired.append(
                        (name, ("poster", session.get("poster")), poster))
                self._refs.append((card, "thumbnail", name))

            if self._bar_images:
                q = self._bar_quantum
                quant = int(max(0.0, min(100.0, session.get("progress", 0))) / q)
                name = f"bar{index}.png"
                desired.append(
                    (name, ("bar", colour, quant),
                     cardimage.bar_png(quant * q, colour))
                )
                self._refs.append((card, "image", name))
                # Which stream this bar belongs to, and its runtime -- the
                # per-item repaint clock is derived from both.
                bar_sessions[name] = (
                    str(session.get("session_key") or session.get("poster")
                        or index),
                    session.get("duration") or 0,
                )

            elif self._card_anchor:
                if self._anchor_style in ("rule", "strip"):
                    # Colour is part of the identity, so a transcode flip
                    # re-tints that one card and nothing else.
                    thin = self._anchor_style == "rule"
                    name = f"anchor{index}_{'r' if thin else 's'}{colour:06x}.png"
                    data = (cardimage.rule_png(colour) if thin
                            else cardimage.strip_png(colour))
                    key = ("anchor", self._anchor_style, colour)
                else:
                    name = f"anchor{index}.png"
                    data = cardimage.anchor_png()
                    key = ("anchor",)
                desired.append((name, key, data))
                self._refs.append((card, "image", name))

            cards.append(card)

        self._pending_bar_sessions = bar_sessions
        return cards, desired

    async def _compose(self, use_cache: bool = False):
        """Assemble embeds, resolve the attachment diff, sync the pager."""
        embed = await self.build_embed(use_cache=use_cache)
        cards, desired = await self._build_stream_cards()

        for name, _key, _data in desired:
            if name == "plexicon.png":
                self._refs.append((embed, "thumbnail", name))
        if self._width_strip:
            online = self.state.last_status is True
            strip_colour = STATUS_COLOURS["online" if online else "offline"]
            # Named by state, so presence on the message proves currency:
            # restarts re-upload nothing, and a status flip is a NEW name --
            # exempt from all holds, so the colour change lands immediately.
            strip_name = f"strip_{'on' if online else 'off'}.png"
            desired.append(
                (strip_name, ("strip", online),
                 cardimage.strip_png(strip_colour))
            )
            self._refs.append((embed, "image", strip_name))

        embeds = [embed] + cards
        # Discord's 6000-character limit is PER MESSAGE. In split mode the
        # dashboard and the cards are two messages with a budget each;
        # summing them shed cards that would have fitted on their own.
        # Only when the cards channel RESOLVES: in fallback mode everything
        # goes out as one message and must share one budget.
        split = bool(self.cards_channel_id) and \
            self.bot.get_channel(self.cards_channel_id) is not None

        def _drop_last_card():
            nonlocal desired
            dropped = embeds.pop()
            names = {n for (e, _k, n) in self._refs if e is dropped}
            self._refs = [r for r in self._refs if r[0] is not dropped]
            desired = [d for d in desired if d[0] not in names]

        # Ten embeds per message. The page size assumes the cards have a
        # message of their own whenever a cards channel is configured; if
        # that channel doesn't resolve (deleted, no access), everything
        # goes out as one message and the dashboard takes one of the ten.
        if not split:
            while len(embeds) > 10:
                _drop_last_card()
            if self.cards_channel_id and not self._cards_fallback_warned:
                self._cards_fallback_warned = True
                logger.warning("Cards channel %s isn't reachable -- showing "
                               "the cards under the dashboard instead",
                               self.cards_channel_id)
        else:
            self._cards_fallback_warned = False

        def _card_chars():
            return sum(len(e) for e in (embeds[1:] if split else embeds))
        while len(embeds) > 1 and _card_chars() > 5900:
            _drop_last_card()
        # With no cards to shed (text mode, plexapi fallback) -- or in split
        # mode, where the dashboard is its own message -- the dashboard
        # embed itself can breach 6000 on a very heavy night. Trim its own
        # trailing fields rather than let Discord reject the whole message.
        while ((split or len(embeds) == 1) and len(embeds[0]) > 5900
               and embeds[0].fields):
            embeds[0].remove_field(len(embeds[0].fields) - 1)
            logger.warning("Trimmed a dashboard field to honor the "
                           "6000-character embed budget")

        # Absolute backstop for the 10-attachment message limit: shed cards
        # from the tail until the attachment set fits.
        #
        # With the cards on their own message the two surfaces have a limit
        # each, so the dashboard's strip and icon must not count against the
        # cards' ten. Counting them together was silently costing a card.
        def _card_attachments(items):
            if not split:
                return len(items)
            card_names = {n for (e, _k, n) in self._refs if e is not embeds[0]}
            return len([d for d in items if d[0] in card_names])

        while _card_attachments(desired) > 10 and len(embeds) > 1:
            _drop_last_card()
            logger.warning("Dropped a stream card to honor the 10-attachment limit")

        to_upload = [
            (name, data) for name, key, data in desired
            if self._file_keys.get(name) != key or name not in self._attachment_urls
        ]
        # Identity-named files (p_<hash> posters, strip_<state>) encode their
        # content in their name, so presence on the message PROVES currency
        # -- even right after a restart, when the key store is empty. This is
        # what makes deploys upload nothing.
        to_upload = [
            x for x in to_upload
            if not (x[0] in self._attachment_urls
                    and x[0].startswith(("p_", "strip_", "anchor")))
        ]

        # Text and images run on SEPARATE clocks, and this is the single most
        # important rule in the file.
        #
        # An edit carrying no files is cheap; an edit carrying files spends
        # from Discord's attachment-upload budget, which is small. Measured
        # live: text-only edits ran 18 minutes without a single rate limit,
        # and the first two minutes of image refreshes brought one back.
        #
        # So a file that is merely STALE (a bar that advanced) waits for the
        # image clock, while a file that is MISSING from the message (a new
        # stream's poster) uploads immediately -- otherwise that card would
        # render with no art at all.
        # Only ever hold a picture of the SAME item. Filenames are positional
        # (poster0.jpg is "whatever is in slot 0"), so after a page flip or a
        # stream starting/stopping, slot 0 holds different media entirely --
        # and "the name is already on the message" stops meaning "this is
        # merely a staler version of the same thing". Holding then leaves the
        # card showing the previous item's cover art.
        layout = tuple(s.get("session_key") or s.get("poster") or ""
                       for s in self._card_sessions)
        self._pending_layout = layout
        same_layout = layout == self._rendered_layout

        refreshes = [x for x in to_upload if x[0] in self._attachment_urls]
        if refreshes and not same_layout:
            # A layout change means slots now show DIFFERENT streams, so the
            # old images are another item's art -- everything ships in full.
            logger.debug("Card layout changed -- uploading %d image(s) now "
                         "rather than holding them", len(refreshes))
        elif refreshes:
            # QUANTUM-EXACT REPAINTS. A bar's key changes exactly when its
            # stream crosses a bar_quantum boundary (10% of ITS runtime), so
            # "key changed" IS the 10% crossing -- no estimation involved.
            # When healthy, that crossing repaints on the next tick. The
            # per-item timer (~runtime/8) exists only as a brake: it engages
            # solely while Discord has pushed back (_image_throttle > 1),
            # trading exactness for recovery until edits flow again.
            # Non-bar refreshes are always due: holding those would show a
            # wrong state, not an old picture.
            now = time.monotonic()
            # Spacing between image-carrying edits. The 10% crossing remains
            # the trigger -- nothing repaints early or on an estimate -- but
            # a crossing landing inside the gap waits for the next open
            # slot. Measured necessity: exact repaints with no spacing drew
            # 15 rate limits in 6 hours; the spaced cadence drew zero.
            # Zero means "no image edit yet this run" -- always open. (The
            # subtraction alone misreads that as recent right after a host
            # reboot, when the monotonic clock itself is still small.)
            slot_open = (not self._last_image_edit
                         or (now - self._last_image_edit) >= self._image_gap)
            due, held = [], []
            for x in refreshes:
                info = self._pending_bar_sessions.get(x[0])
                if info is None:
                    due.append(x)
                    continue
                if not slot_open:
                    held.append(x)
                    continue
                if self._image_throttle <= 1:
                    due.append(x)      # healthy: repaint at the crossing
                    continue
                skey, duration = info
                last = self._bar_painted.get(skey) or self._adopted_at
                if now - last >= self._item_interval(duration):
                    due.append(x)
                else:
                    held.append(x)

            # BURST CONTROL -- the trigger for the shared-scope 429s.
            # Measured live: text-only edits ran 18 minutes clean; a repaint
            # replacing 8 files in one edit drew a rate limit within 2
            # minutes. Repaints ride the regular edits a few files at a
            # time; the rest stay due and drain on following ticks.
            batch = due[: self._image_batch]
            skipped = {x[0] for x in refreshes} - {x[0] for x in batch}
            if skipped:
                to_upload = [x for x in to_upload if x[0] not in skipped]
                logger.debug(
                    "Holding %d image(s): %d not yet due, %d staggered onto "
                    "later edits", len(skipped), len(held),
                    max(0, len(due) - len(batch)),
                )

        upload_names = {name for name, _ in to_upload}
        # A file we chose NOT to re-upload still carries its OLD content, so
        # its recorded key must stay old too -- otherwise the diff would
        # believe the message is current and never refresh that image.
        self._pending_keys = {
            name: (key if name in upload_names
                   else self._file_keys.get(name, key))
            for name, key, _ in desired
        }
        self._keep_names = {name for name, _k, _d in desired} - upload_names

        now_pin = time.monotonic()
        for target, kind, name in self._refs:
            if name not in upload_names and name not in self._attachment_urls:
                continue  # file neither uploading nor present on the message
            # THE POSTER FLICKER.
            #
            # attachment://name is stable in the payload the bot sends, but
            # it is not what the client receives: Discord resolves it and
            # signs the result (?ex=...&is=...&hm=...) fresh on EVERY edit.
            # So an unchanged poster arrived at the client under a new url
            # each tick, and a changed url is a cache miss -- the image
            # reloads, which is the flash.
            #
            # A file that is genuinely uploading has to use attachment://,
            # because its url does not exist yet. Everything else reuses the
            # url Discord already issued, byte-for-byte, so the client sees
            # no change and never re-fetches. Re-resolved before the 24h
            # expiry so a pinned url cannot go stale.
            pinned = self._attachment_urls.get(name)
            fresh = (now_pin - self._attachment_pinned.get(name, 0.0)
                     < ATTACHMENT_URL_TTL)
            if name in upload_names or not pinned or not fresh:
                url = f"attachment://{name}"
            else:
                url = pinned
            if kind == "thumbnail":
                target.set_thumbnail(url=url)
            else:
                target.set_image(url=url)

        files = [
            discord.File(io.BytesIO(data), filename=name)
            for name, data in to_upload
        ] or None

        view: Optional[discord.ui.View] = None
        if self._total_pages > 1:
            self.pager.sync(self._page_rendered, self._total_pages,
                            self._stream_total)
            view = self.pager
        return embeds, files, view

    def _note_stall(self, reason: str, held: Optional[float] = None,
                    force: bool = False) -> None:
        """Record a rate-limited edit, back off, and slow the whole loop.

        Backing off alone was not enough: each pause ended, three or four
        refreshes went out at the configured interval, and the budget was
        gone again. So a stall also widens the refresh interval and switches
        to attachment-free edits, and both stay in force until a long clean
        run earns them back.
        """
        rate = self._rate
        info = rate.last_429 if rate else None
        observed = None
        if held is not None:
            # Measured: how long Discord actually made us wait.
            observed = held
        elif info and info.get("retry_after"):
            # Only trust a 429 that belongs to THIS stall. A stale record
            # from minutes ago decays to observed=0.0, which the brief-
            # penalty branch would read as "1-second blip, cadence
            # unchanged" -- silently disabling every escalation for real
            # 400009s that arrive without their own fresh 429 entry.
            elapsed = time.monotonic() - info["at"]
            retry = float(info["retry_after"])
            if elapsed <= retry + 30.0:
                observed = max(0.0, retry - elapsed)

        # A one-second penalty is not a crisis. Discord shapes traffic with
        # brief 429s all the time -- especially scope=shared ones, which are
        # not even charged against this bot. Pause the requested moment and
        # leave the cadence alone; escalating on these is what walked the
        # refresh interval from 60s up to 240s over two harmless blips.
        if not force and observed is not None and observed < SHORT_PENALTY_SECONDS:
            self._stalled_until = time.monotonic() + max(1.0, observed)
            self._soft_limits += 1
            self._clean_refreshes = 0
            logger.info(
                "Brief rate limit (%s, %.1fs, scope=%s) -- waiting that long, "
                "cadence unchanged", reason, observed,
                (info or {}).get("scope") or "?",
            )
            return

        self._stall_count += 1
        self._stall_total += 1
        self._clean_refreshes = 0
        wait = min(STALL_BACKOFF_MAX,
                   STALL_BACKOFF_BASE * (2 ** (self._stall_count - 1)))
        if observed is not None:
            # Wait comfortably LONGER than the penalty we just served.
            # Returning the instant it expires is what escalated 25s -> 120s
            # -> 153s; Discord widens the penalty for repeat offenders inside
            # its window, so the only way out is to give it real room.
            wait = max(STALL_BACKOFF_BASE / 5, observed * 2)
            logger.info(
                "Held %.0fs by Discord (scope=%s) -- waiting %.0fs, twice the "
                "penalty, so we do not return the moment it expires",
                observed, (info or {}).get("scope") or "?", wait,
            )
        self._stalled_until = time.monotonic() + wait

        # The refresh cadence must also exceed the penalty, or the next tick
        # simply walks back into it.
        if observed and self._effective_interval() < observed:
            while (self._effective_interval() < observed
                   and self._throttle < self._throttle_max()):
                self._throttle *= 2
            logger.info("Refresh interval raised to %.0fs to clear the %.0fs "
                        "penalty window", self._effective_interval(), observed)

        previous = self._throttle
        self._throttle = min(self._throttle_max(),
                             max(2, self._throttle * 2))
        # Images are the expensive part, so they slow down harder.
        self._image_throttle = min(IMAGE_THROTTLE_MAX,
                                   max(2, self._image_throttle * 2))
        # Push every per-item repaint out to the new, wider interval.
        now_stall = time.monotonic()
        self._bar_painted = {k: now_stall for k in self._bar_painted}
        logger.warning(
            "Rate limited (%s) -- pausing edits for %.0f min; text refresh "
            "now %.0fs, image refresh now %.0f min (stall #%d)",
            reason, wait / 60, self._effective_interval(),
            self._image_interval() / 60, self._stall_count,
        )
        if self._throttle != previous:
            self._apply_interval()

    @property
    def _rate(self):
        """The bot's rate-limit observer, if instrumentation is available."""
        return getattr(self.bot, "rate_limits", None)

    def _rate_wait(self) -> float:
        rate = self._rate
        return rate.edit_wait() if rate else 0.0

    def _rate_describe(self) -> str:
        rate = self._rate
        return rate.describe() if rate else "no rate-limit data"

    def _rate_headroom(self) -> Optional[int]:
        rate = self._rate
        return rate.edit_headroom() if rate else None

    def _item_interval(self, duration_ms: float) -> float:
        """Repaint spacing for ONE item, derived from its own runtime.

        ~IMAGE_UPDATES_PER_ITEM visible advances across the item, floored so
        very short content cannot drive uploads hot, ceilinged by the
        configured maximum, and stretched by the throttle when Discord has
        pushed back.
        """
        base = self.image_refresh_seconds
        if duration_ms:
            base = min(base, (duration_ms / 1000.0) / IMAGE_UPDATES_PER_ITEM)
        base = max(IMAGE_REFRESH_MIN, base)
        return min(IMAGE_REFRESH_MAX, base * self._image_throttle)

    def _image_interval(self) -> float:
        """How often bar images may repaint, scaled to what is playing.

        A fixed clock is wrong for short content: at 20 minutes, a 22-minute
        episode would get a single bar update for the entire show. So the
        interval is derived from the SHORTEST thing currently streaming, so
        every item gets roughly the same number of visible advances whether
        it runs 20 minutes or three hours. The configured value acts as a
        ceiling, and the floor keeps the upload rate bounded.
        """
        base = self.image_refresh_seconds
        if self._shortest_ms:
            per_item = (self._shortest_ms / 1000.0) / IMAGE_UPDATES_PER_ITEM
            base = min(base, per_item)
        base = max(IMAGE_REFRESH_MIN, base)
        return min(IMAGE_REFRESH_MAX, base * self._image_throttle)

    def _throttle_max(self) -> int:
        """Highest multiplier worth applying at the current base clock.

        Derived from the seconds ceiling rather than fixed, so a 10s base
        can still reach the same wall-clock cadence a 60s base could.
        """
        base = max(1.0, float(self.refresh_seconds))
        return max(THROTTLE_MAX, int(THROTTLE_CEILING_SECONDS / base) or 1)

    def _effective_interval(self) -> float:
        """Refresh cadence, tightened while something is actually playing.

        Progress cannot go in the change signature -- it advances on every
        poll, and including it would make the event-driven design a fixed
        loop wearing a disguise. But that leaves the bar and the clock
        moving only on the scheduled tick, so at refresh_seconds=300 a card
        could sit five minutes stale while its stream ran.

        The honest fix is not a faster signature, it is a shorter clock WHEN
        THERE IS SOMETHING TO SHOW. Idle, the backstop stays long and costs
        nothing; streaming, it tightens to bound how far the bar can drift.
        """
        base = self.refresh_seconds
        if self.active_refresh_seconds and self._streams_active:
            base = min(base, self.active_refresh_seconds)
        return min(THROTTLE_CEILING_SECONDS, base * self._throttle)

    def _apply_interval(self) -> None:
        try:
            self.update_dashboard.change_interval(
                seconds=self._effective_interval())
        except Exception:
            logger.debug("Could not change refresh interval", exc_info=True)

    def _note_soft_limit(self, elapsed: float, why: str = "") -> None:
        """An edit that landed, but only after Discord made it wait.

        This is the early-warning signal the bot was missing: a short 429
        means the budget is nearly gone, even though nothing failed. It must
        never count toward earning a faster cadence.
        """
        self._clean_refreshes = 0
        self._soft_limits += 1
        self._soft_limit_total += 1
        logger.info(
            "Edit took %.1fs and was throttled (%s) -- soft limit %d/%d",
            elapsed, why or "timing", self._soft_limits,
            SOFT_LIMITS_BEFORE_THROTTLE,
        )
        if self._soft_limits < SOFT_LIMITS_BEFORE_THROTTLE:
            return
        self._soft_limits = 0
        if self._throttle < self._throttle_max():
            self._throttle = min(self._throttle_max(),
                                 max(2, self._throttle * 2))
            self._escalation_total += 1
            logger.warning(
                "Repeated soft limits -- slowing text refresh to %.0fs "
                "before a hard stall happens", self._effective_interval(),
            )
            self._apply_interval()
        elif self._image_throttle < IMAGE_THROTTLE_MAX:
            self._image_throttle = min(IMAGE_THROTTLE_MAX,
                                       self._image_throttle * 2)
            logger.warning("Repeated soft limits -- image refresh now %.0f min",
                           self._image_interval() / 60)

    def _note_success(self) -> None:
        """A completed edit means the bucket has room again."""
        self._soft_limits = 0
        if self._stall_count:
            logger.info("Dashboard edits are flowing again after %d stall(s)",
                        self._stall_count)
        self._stall_count = 0
        self._stalled_until = 0.0

        if self._throttle == 1 and self._image_throttle == 1:
            return

        # Earn the normal cadence back gradually, one step per sustained
        # clean run. Restoring everything at once is what produced the
        # sawtooth: healthy, a couple of image refreshes, throttled again.
        self._clean_refreshes += 1
        if self._clean_refreshes < CLEAN_RUN_TO_RECOVER:
            return
        self._clean_refreshes = 0

        # Text cadence recovers FIRST -- it is nearly free. The image clock
        # is relaxed only once text is already back to normal.
        if self._throttle > 1:
            rate = self._rate
            # Halving is a guess about a bucket we cannot see. When Discord
            # tells us outright that the edit bucket is untouched, the
            # throttle has outlived its cause and the ladder is just lost
            # minutes -- so drop the whole way in one step. The check is
            # strict: unknown or stale state reads as "not full" and falls
            # back to halving.
            # getattr, not a bare call: an older or stubbed observer
            # without this method must degrade to halving, not raise.
            bucket_full = getattr(rate, "edit_bucket_full", None)
            if callable(bucket_full) and bucket_full():
                self._throttle = 1
                logger.info(
                    "Steady for %d refreshes and the edit bucket is full "
                    "(%s) -- text refresh straight back to %.0fs",
                    CLEAN_RUN_TO_RECOVER, self._rate_describe(),
                    self._effective_interval())
            else:
                self._throttle = max(1, self._throttle // 2)
                logger.info("Steady for %d refreshes -- text refresh now %.0fs",
                            CLEAN_RUN_TO_RECOVER, self._effective_interval())
            self._apply_interval()
            return
        if self._image_throttle > 1:
            self._image_throttle = max(1, self._image_throttle // 2)
            logger.info("Steady for %d refreshes -- image refresh now %.0f min",
                        CLEAN_RUN_TO_RECOVER, self._image_interval() / 60)

    async def _push(self, channel, message: Optional[discord.Message],
                    embeds, files, view, editor=None) -> bool:
        """Edit the dashboard message, or post a fresh one.

        On edits with new files, unchanged attachments are passed through as
        their existing Attachment objects -- Discord keeps them (same CDN
        URL, so no image reloads), while only the changed files upload.

        Returns True only if this content actually REACHED Discord. Callers
        use that to decide whether to record the payload as delivered; a
        stalled or rejected edit must stay eligible for retry, or the
        payload-dedupe check would suppress the same content forever.
        """
        if message is not None:
            # `editor` routes the edit through the interaction webhook
            # (PATCH /webhooks/{app}/{token}/messages/@original) instead of
            # the channel message route. Different major parameter, so a
            # different bucket -- a page flip can still land while the
            # channel's edit bucket is exhausted.
            do_edit = editor or message.edit
            started = time.monotonic()
            # Stamped at the START of the attempt: the request leaves now, so
            # that is the moment the next one must be spaced from.
            self._last_edit_at = started
            rate = self._rate
            # Ground truth for "were we actually throttled", captured before
            # the call. Elapsed time alone cannot tell a rate limit apart from
            # a slow attachment upload.
            before_429 = rate.count_429 if rate else 0
            before_room = rate.edit_headroom() if rate else None
            try:
                if files is None:
                    # Re-supplying the existing attachments keeps their
                    # identity (no re-upload) while giving Discord the
                    # resolution context for attachment:// references.
                    await asyncio.wait_for(do_edit(
                        embeds=embeds,
                        attachments=list(message.attachments),
                        view=view,
                    ), timeout=EDIT_TIMEOUT)
                    self._last_push_had_files = False
                else:
                    keep = [a for a in message.attachments
                            if a.filename in self._keep_names]
                    message = await asyncio.wait_for(do_edit(
                        embeds=embeds, attachments=keep + files, view=view
                    ), timeout=EDIT_TIMEOUT)
                    # Merge, do not replace. Every attachment comes back
                    # freshly signed, but only the ones that actually
                    # uploaded NEED a new pin -- re-pinning the untouched
                    # ones would change their embed url next tick and
                    # reintroduce the flicker this is here to stop. The old
                    # signature stays valid until it expires.
                    sent = {f.filename for f in files}
                    for a in message.attachments:
                        if a.filename in sent or a.filename not in self._attachment_urls:
                            self._attachment_urls[a.filename] = a.url
                            self._attachment_pinned[a.filename] = time.monotonic()
                    self._last_push_had_files = bool(files)
                    self._file_keys = dict(self._pending_keys)
                    self._rendered_layout = self._pending_layout
                    # Stamp each shipped bar's per-item clock -- only bars
                    # that actually LANDED, so a failed edit leaves them due.
                    now_m = time.monotonic()
                    # Any file-carrying edit opens a new spacing window --
                    # posters and flips included, so bar repaints queue
                    # BEHIND content-critical uploads rather than alongside.
                    self._last_image_edit = now_m
                    for shipped in files:
                        info = self._pending_bar_sessions.get(shipped.filename)
                        if info:
                            self._bar_painted[info[0]] = now_m
                # Was this edit actually throttled? Two honest signals:
                # a 429 arrived, or the bucket was already empty going in
                # (discord.py then waits for the refill before sending).
                # Elapsed time on its own is NOT a signal -- an edit carrying
                # half a megabyte of posters is slow because it is uploading,
                # and treating that as throttling made the bot slow itself
                # down for no reason.
                elapsed = time.monotonic() - started
                if rate is not None:
                    hit_429 = rate.count_429 > before_429
                    # An empty bucket is an EARLY WARNING, not an event --
                    # and it is only a warning when 429s are actually
                    # happening. With limit=5 and two messages on a 10-30s
                    # clock the bucket drains to zero as a matter of course
                    # and refills a second later; measured after the
                    # zero-attachment change, that produced 3808 cadence
                    # escalations against ZERO 429s. The bot was throttling
                    # itself toward a 600s refresh to avoid a limit Discord
                    # had stopped applying. So headroom only counts while a
                    # real 429 is recent enough to still mean something.
                    last = rate.last_429
                    warm = bool(last) and (
                        time.monotonic() - last["at"] < HEADROOM_MEMORY)
                    was_empty = (before_room is not None and before_room <= 0
                                 and warm)
                    throttled = hit_429 or was_empty
                    why = ("429 received" if hit_429
                           else "bucket empty after a recent 429")
                else:
                    # No instrumentation: fall back to timing, but only for
                    # edits with no files, where slowness has no other cause.
                    throttled = files is None and elapsed > SOFT_LIMIT_SECONDS
                    why = "slow text-only edit"

                if throttled and elapsed < SOFT_LIMIT_SECONDS:
                    # A 429 that cost nothing.
                    #
                    # Measured live: five of these in six hours, every one
                    # retry_after=1.0s with the bot's OWN bucket reading
                    # remaining=4/5. That is Discord's shared per-channel
                    # constraint being brushed when a webhook poke and the
                    # scheduled tick land in the same second -- discord.py
                    # absorbs it, waits its second, and the edit lands.
                    #
                    # Counting that as a soft limit is how a one-second
                    # hiccup escalated into an eight-minute refresh
                    # interval: two of them doubled the cadence, and the
                    # ladder did the rest. What matters is how long we were
                    # actually held, and we were held for a second.
                    self._absorbed_429 += 1
                    logger.info(
                        "Absorbed a 429 in %.1fs (%d so far) -- edit landed, "
                        "cadence unchanged", elapsed, self._absorbed_429)
                    self._note_success()
                elif throttled and elapsed >= HARD_STALL_SECONDS:
                    # How long we were actually held is ground truth. The
                    # Retry-After header reported 1.0s while discord.py was
                    # waiting 153s -- for shared-scope limits that header
                    # describes a different bucket than the one that bit us.
                    self._note_stall(f"held {elapsed:.0f}s", held=elapsed)
                elif throttled and elapsed >= SHORT_PENALTY_SECONDS:
                    # Held 15-90s and the edit STILL LANDED: shared-scope
                    # penalties typically run 15-68s and
                    # discord.py rides every one of them out successfully.
                    # Treating a landed edit as a hard stall is what took a
                    # dashboard that was merely a minute late and made it
                    # ten minutes stale -- the cure doing more damage than
                    # the disease. The soft ladder still slows the cadence
                    # if these keep coming; it just does not panic on one.
                    self._note_soft_limit(elapsed, f"{why}, edit landed")
                elif throttled:
                    self._note_soft_limit(elapsed, why)
                else:
                    self._note_success()
                    if elapsed > SOFT_LIMIT_SECONDS:
                        logger.debug(
                            "Edit took %.1fs uploading %d file(s) -- not "
                            "throttled", elapsed, len(files or []),
                        )
                headroom = self._rate_headroom()
                if headroom is not None and headroom <= 1:
                    logger.info("Edit budget nearly spent (%s)",
                                self._rate_describe())
                return True
            except asyncio.TimeoutError:
                # discord.py swallows 429s: it sleeps out the penalty and
                # retries internally, so a rate-limited edit never raises --
                # it just never returns.
                #
                # Reposting does NOT help here. Discord buckets message
                # routes by their major parameter, which for
                # /channels/{id}/messages/{id} is the CHANNEL. Measured on a
                # live server: brand-new messages were rejected on their
                # first edit 27s after being created. A fresh message buys
                # nothing and only churns the channel.
                #
                # The only real remedy is to stop editing for a while and
                # let the channel's bucket refill.
                # A timeout means the edit never landed at all -- always
                # worse than one that merely arrived late.
                self._note_stall("edit stalled >%.0fs" % EDIT_TIMEOUT,
                                 held=EDIT_TIMEOUT, force=True)
                return False
            except discord.NotFound:
                # The message we tried to edit no longer exists -- a repost
                # replaced it, or someone deleted it. Clients can keep
                # showing a deleted message (with live buttons) until they
                # redraw, so presses on the ghost land here as 10008s.
                logger.warning(
                    "Dashboard message %s no longer exists -- replacing it",
                    message.id,
                )
                if message.id == self.dashboard_message_id:
                    self.dashboard_message_id = None
                await self._replace_message(channel, None)
                return False
            except discord.HTTPException as exc:
                code = getattr(exc, "code", None)
                if code == 30046:
                    # "Edits to messages older than 1 hour" -- genuinely a
                    # per-message ceiling, so a new message does clear it.
                    logger.warning("Message too old to keep editing (30046) "
                                   "-- replacing it")
                    await self._replace_message(channel, message)
                    return False
                if code == 400009 or getattr(exc, "status", None) == 429:
                    # "This SERVER has exceeded the message edit attachment
                    # upload limit" -- server-wide, so reposting cannot help.
                    self._note_stall(f"Discord returned {code or 429}")
                    return False
                raise

        message = await channel.send(embeds=embeds, files=files or [], view=view)
        self._attachment_urls = {a.filename: a.url for a in message.attachments}
        stamped = time.monotonic()
        self._attachment_pinned = {n: stamped for n in self._attachment_urls}
        self._file_keys = dict(self._pending_keys)
        self._rendered_layout = self._pending_layout
        self.dashboard_message_id = message.id
        self._write_json(self.message_id_file, {"message_id": message.id})
        logger.info("Created dashboard message %s", message.id)
        return True

    async def _sweep_duplicates(self, channel) -> None:
        """Delete stray dashboard messages left behind by failed reposts.

        A repost posts the new message before deleting the old one, so a
        delete that fails (or a restart landing between the two) strands a
        duplicate. Only this bot's own embed messages in the dashboard
        channel are considered, and never the one currently tracked.
        """
        try:
            async for old in channel.history(limit=30):
                if old.author.id != self.bot.user.id:
                    continue
                if old.id in (self.dashboard_message_id,
                              self.recent_message_id,
                              self.cards_message_id):
                    continue
                if not old.embeds or not_ours(old):
                    continue
                await old.delete()
                logger.info("Swept stray dashboard message %s", old.id)
        except discord.HTTPException as exc:
            logger.warning("Could not sweep old dashboard messages: %s", exc)
        except Exception:
            logger.exception("Duplicate sweep failed")

    async def _replace_message(self, channel, old_message) -> None:
        """Retire the dashboard message and post a fresh replacement.

        A new message carries a fresh edit budget: 30046 (edits to messages
        older than an hour) is per-message, and a new message's initial
        attachments are ordinary uploads, not the edit-uploads that 400009
        meters. The new message is posted FIRST so the channel never sits
        empty; the old one is deleted right after.
        """
        now = time.monotonic()
        if self._last_repost and now - self._last_repost < REPOST_COOLDOWN:
            logger.info("Repost suppressed -- last one was %.0fs ago",
                        now - self._last_repost)
            return
        self._last_repost = now

        # A fresh message starts with zero attachments, so every file the
        # embeds reference must upload again. Clearing the diff state makes
        # the next compose treat everything as new.
        self._file_keys = {}
        self._pending_keys = {}
        self._attachment_urls = {}
        self._attachment_pinned = {}
        self._keep_names = set()
        self._last_payload = None
        previous_id = self.dashboard_message_id
        self.dashboard_message_id = None

        embeds, files, view = await self._compose(use_cache=True)
        cards_channel = (self.bot.get_channel(self.cards_channel_id)
                         if self.cards_channel_id else None)
        if cards_channel is not None:
            # The cards own a message in their own channel. Posting the full
            # render here put every card, poster and the pager into the
            # status channel on each repost; only the dashboard surface belongs in this one.
            embeds, files, _card_e, _card_f = self._split_surfaces(
                embeds, files)
            view = None
            self._cards_payload = None      # cards re-render next tick
        try:
            delivered = await self._push(channel, None, embeds, files, view)
        except discord.HTTPException as exc:
            logger.warning("Could not post the replacement dashboard (%s) -- "
                           "keeping the current message", exc)
            delivered = False
        if not delivered:
            # Nothing new landed, so the old message is still THE dashboard.
            # Forgetting its id here made the next tick post another one and
            # leave this one behind as a frozen duplicate.
            self.dashboard_message_id = previous_id
            return

        if old_message is not None:
            try:
                await old_message.delete()
                logger.info("Deleted old dashboard message %s", old_message.id)
            except discord.HTTPException as exc:
                logger.warning("Could not delete the old dashboard "
                               "message: %s", exc)

    async def _flip_page(self, interaction: discord.Interaction, delta: int) -> None:
        """Button press: change page and re-render."""
        target = max(0, min(self._card_page + delta, self._total_pages - 1))

        # A press that cannot move (already at an end, or only one page)
        # used to fall straight through into a full re-render: one edit and
        # every file on the page re-uploaded, for no visible change. That is
        # pure rate-limit spend, so it stops here.
        if target == self._card_page and target == self._rendered_page:
            logger.info("Flip ignored: already on page %d/%d (%s)",
                        target + 1, self._total_pages, interaction.user)
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return

        if target == self._card_page:
            # Committed but never drawn -- the previous render was deferred
            # behind a rate limit, so the user is looking at page
            # _rendered_page and pressing again is a RETRY, not a no-op.
            # Swallowing it here is what made the pager feel dead.
            logger.info("Flip retry: page %d committed but %d is on screen (%s)",
                        target + 1, self._rendered_page + 1, interaction.user)
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                return
            # Retries obey the same burst coalescing as first presses --
            # someone leaning on the button must not turn into N uploads.
            since = time.monotonic() - self._last_flip_render
            if self._last_flip_render and since < FLIP_COALESCE:
                self._schedule_flip(interaction.channel, FLIP_COALESCE - since)
                return
            await self._render_flip(interaction.channel, interaction.message,
                                    interaction)
            return

        logger.info("Page flip: %d -> %d (%s)", self._card_page + 1,
                    target + 1, interaction.user)
        # Committed BEFORE anything that can fail: even if this interaction
        # dies, the next refresh tick renders the requested page.
        self._card_page = target
        try:
            # Poster fetches can exceed the 3-second interaction window on a
            # cold cache, so acknowledge first, then edit normally.
            await interaction.response.defer()
        except discord.HTTPException as exc:
            # Expired or double-acknowledged interaction. Nothing to render
            # against -- the refresh loop picks the page up within a minute.
            logger.warning("Could not acknowledge page flip: %s", exc)
            return

        # Coalesce a burst: if a flip rendered moments ago, let the presses
        # settle and render once. Every intermediate page the user skated
        # past would otherwise cost a full upload nobody looked at.
        since = time.monotonic() - self._last_flip_render
        if self._last_flip_render and since < FLIP_COALESCE:
            self._schedule_flip(interaction.channel, FLIP_COALESCE - since)
            return

        await self._render_flip(interaction.channel, interaction.message,
                                interaction)

    def _schedule_flip(self, channel, delay: float) -> None:
        """Queue a single settled render, replacing any already pending."""
        if self._flip_task is not None and not self._flip_task.done():
            self._flip_task.cancel()
        self._flip_task = asyncio.ensure_future(
            self._deferred_flip(channel, delay))

    async def _deferred_flip(self, channel, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            logger.info("Rendering settled page %d/%d",
                        self._card_page + 1, self._total_pages)
            await self._render_flip(channel, None)
        except asyncio.CancelledError:
            pass  # superseded by a newer press, or by the refresh loop
        except Exception:
            logger.exception("Deferred page render failed")

    async def _render_flip(self, channel, message,
                           interaction=None) -> None:
        """Draw the current page onto the live dashboard message.

        Serialised against the refresh loop: _compose() writes shared diff
        state that _push() consumes afterwards, so two renders interleaving
        would commit one render's file keys against the other's uploads and
        leave the message permanently mismatched.
        """
        if self._closed:
            return  # unloaded mid-press; the replacement cog owns the message
        # Bounded wait, not `async with`. The lock is held for the whole of
        # an edit, and an edit parked inside discord.py's rate limiter can
        # hold it for minutes -- during which every press queued behind it,
        # which is exactly what made flips look dead while the interaction
        # route sat unused. Better to give up quickly and let the press be
        # retried than to block a user behind a refresh they cannot see.
        try:
            await asyncio.wait_for(self._render_lock.acquire(),
                                   timeout=FLIP_LOCK_WAIT)
        except asyncio.TimeoutError:
            logger.info(
                "Flip could not start: a refresh has held the render lock "
                "for over %.0fs. Page %d is committed and draws on the next "
                "tick; press again to retry sooner.",
                FLIP_LOCK_WAIT, self._card_page + 1)
            return
        try:
            if self._closed:
                return
            await self._render_flip_locked(channel, message, interaction)
        finally:
            self._render_lock.release()

    async def _render_flip_locked(self, channel, message,
                                  interaction=None) -> None:
        # A flip is a direct answer to a button press, so it gets a rescue
        # route the periodic refresh does not: the interaction webhook. That
        # endpoint is keyed by the interaction token rather than the channel,
        # so it is a separate bucket and is exempt from the global limit --
        # which is why the page can move while the refresh loop is stalled.
        # ALWAYS prefer the interaction route for a flip, not just as a
        # rescue when already blocked.
        #
        # A flip is the expensive kind of edit: with anchors on it uploads a
        # whole new page of posters and anchors. Sending that through
        # PATCH /channels/{id}/messages/{id} spends the same per-channel
        # budget the minute refresh needs, so a few quick presses can push
        # the dashboard into a backoff. The interaction webhook is keyed by
        # the interaction token instead -- a different bucket, exempt from
        # the global limit -- so flips stop competing with refreshes
        # entirely.
        # getattr, not a bare attribute: a stub or an older discord.py
        # without this method must fall back to the channel route rather
        # than raise inside a button press.
        editor = (getattr(interaction, "edit_original_response", None)
                  if interaction is not None else None)
        blocked = ""
        remaining = self._stalled_until - time.monotonic()
        if remaining > 0:
            blocked = f"rate limited for another {remaining:.0f}s"
        elif self._rate_wait() > 0:
            blocked = f"edit budget refills in {self._rate_wait():.1f}s"
        if blocked and interaction is None:
            # No press behind this render (a settled/deferred redraw), so
            # there is no token to borrow. The refresh loop draws it.
            logger.info("Flip render deferred -- %s", blocked)
            return
        if blocked:
            logger.info("Flip riding the interaction webhook past a block -- "
                        "%s", blocked)
        self._last_flip_render = time.monotonic()
        pressed_id = getattr(message, "id", None)

        # --- split mode: the pager lives on the CARDS message ---
        cards_channel = (self.bot.get_channel(self.cards_channel_id)
                         if self.cards_channel_id else None)
        if cards_channel is not None:
            # Borrow the interaction route only when the press came from the
            # cards message we track. A press on a ghost copy (a client still
            # showing a deleted message) would otherwise edit the ghost, 404,
            # and the 404 would be read as "our message is gone" -- nulling
            # the live id so the next change SENT a duplicate (a ping).
            if pressed_id is not None and pressed_id != self.cards_message_id:
                logger.info("Flip pressed on stale cards message %s -- "
                            "editing %s instead", pressed_id,
                            self.cards_message_id)
                editor = None
            try:
                embeds, files, view = await self._compose(use_cache=True)
                _m, _mf, card_e, card_f = self._split_surfaces(embeds, files)
                if await self._push_cards(cards_channel, card_e, card_f,
                                          view, editor=editor):
                    self._rendered_page = self._card_page
                    self._cards_payload = None
            except Exception:
                logger.exception(
                    "Page flip failed -- page %d renders on the next refresh",
                    self._card_page + 1)
            return

        # --- single-message mode (or the cards channel is unresolvable):
        # the pager lives on the dashboard in the STATUS channel. Never the
        # channel the press came from: in fallback mode that is the old
        # cards channel, and replacing "the dashboard" there posted a whole
        # second dashboard into the cards channel.
        channel = self.bot.get_channel(self.channel_id) or channel
        if message is None or (
            self.dashboard_message_id
            and message.id != self.dashboard_message_id
        ):
            if message is not None:
                logger.info(
                    "Flip pressed on stale message %s -- redirecting to %s",
                    message.id, self.dashboard_message_id,
                )
            # The interaction route edits the PRESSED message; after a
            # redirect that is the ghost, so edit the live one directly --
            # unless the channel route is blocked right now, in which case
            # the page is committed and the refresh loop draws it.
            editor = None
            if blocked:
                logger.info("Flip on a stale message deferred -- %s", blocked)
                return
            message = None
            if self.dashboard_message_id:
                try:
                    message = await channel.fetch_message(
                        self.dashboard_message_id)
                except discord.NotFound:
                    message = None  # replaced below
                except discord.HTTPException as exc:
                    logger.warning("Flip could not fetch the dashboard: %s",
                                   exc)
                    return

        try:
            embeds, files, view = await self._compose(use_cache=True)
            if message is None:
                # No live message to edit: replace outright (full upload).
                await self._replace_message(channel, None)
                self._rendered_page = self._card_page
            elif await self._push(channel, message, embeds, files, view,
                                  editor=editor):
                # Only a delivered edit moves the displayed page. If this
                # failed, _rendered_page stays behind and the next press is
                # treated as a retry rather than swallowed.
                self._rendered_page = self._card_page
        except Exception:
            logger.exception(
                "Page flip failed -- page %d renders on the next refresh",
                self._card_page + 1,
            )

    @tasks.loop(minutes=1)
    async def update_dashboard(self) -> None:
        """One refresh tick. Never lets an exception out.

        tasks.loop stops PERMANENTLY on any exception it does not know how
        to retry (it only retries connection-type errors), so a single
        unexpected failure -- a 5xx on a repost, a bad config encoding --
        used to freeze the dashboard until the container restarted.
        """
        try:
            await self._update_dashboard_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dashboard refresh failed -- retrying next tick")
        finally:
            # Every tick, including the early returns during a backoff --
            # those are exactly when health.json is most worth reading.
            try:
                self._write_health()
            except Exception:
                logger.debug("health snapshot failed", exc_info=True)

    async def _update_dashboard_once(self) -> None:
        if self._closed:
            # Unloaded: an edit shielded from cancellation (a webhook poke
            # already under way) or a late tick must not race the
            # replacement cog or reopen sessions cog_unload just closed.
            return
        self._maybe_reload_config()
        # The status dot goes FIRST, ahead of every message-edit gate below.
        # Renaming a channel is PATCH /channels/{id} -- a different endpoint
        # with its own 2-per-10-minutes limit, sharing nothing with the
        # message edit bucket. Sitting after those gates meant that during a
        # rate-limit backoff (98 seconds was a real one) the dot was never
        # even evaluated, so the server could go down and come back up
        # without the channel name ever noticing. It reads last tick's
        # status, which is close enough for something Discord only lets us
        # change twice per ten minutes.
        dot_channel = self.bot.get_channel(self.channel_id)
        if dot_channel is not None:
            try:
                await self._update_channel_dot(dot_channel,
                                               self.state.last_status)
            except Exception:
                logger.exception("Channel dot update failed")

        remaining = self._stalled_until - time.monotonic()
        if remaining > 0:
            logger.info("Skipping refresh -- backing off for another %.0fm "
                        "(rate limited)", remaining / 60)
            return

        # Floor on the spacing between edit ATTEMPTS, measured from when the
        # last one STARTED. The loop's own interval already paces normal
        # ticks; this only catches the pathological case where an edit ran
        # longer than the whole interval, leaving the loop overdue so
        # discord.py fires the next iteration instantly.
        #
        # Measuring from COMPLETION was wrong: a routine 3-second upload made
        # every cycle come up 3 seconds short, so every other refresh was
        # silently dropped.
        since_last = time.monotonic() - self._last_edit_at
        if self._last_edit_at and since_last < MIN_ATTEMPT_GAP:
            logger.debug("Holding off %.0fs -- previous edit only just went "
                         "out", MIN_ATTEMPT_GAP - since_last)
            return

        # Discord reports the state of the edit bucket on every response, so
        # the bot can stop one request short instead of discovering the limit
        # by exceeding it. This is the check that prevents a 429 rather than
        # reacting to one.
        wait = self._rate_wait()
        if wait > 0:
            logger.info("Skipping refresh -- edit budget empty, refills in "
                        "%.1fs (%s)", wait, self._rate_describe())
            return
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            logger.warning("Channel %s not found", self.channel_id)
            return

        # Held across compose AND push: _compose writes the diff state that
        # _push commits, so a button press interleaving between them would
        # pair one render's uploads with the other's bookkeeping. The sweep
        # deletes messages, so it belongs under the same lock -- a flip's
        # repost running mid-sweep would race it.
        async with self._render_lock:
            # Re-check the stall now that we hold the lock. A caller queued
            # behind a render that just timed out into a stall would
            # otherwise edit straight into it -- a second forced stall,
            # doubling the throttle twice for one event. (Deliberately NOT
            # the MIN_ATTEMPT_GAP check: a refresh queued here carries a
            # real change the holder may have fetched too early to see.)
            if self._stalled_until - time.monotonic() > 0:
                logger.info("Refresh dropped -- a stall began while it "
                            "waited for the render lock")
                return
            if not self._swept:
                self._swept = True
                await self._sweep_duplicates(channel)
            await self._refresh_locked(channel)

    def _write_health(self, force: bool = False) -> None:
        """data/health.json: what the bot believes about itself, right now.

        Every diagnosis this project has run went through log greps, and
        the greps were wrong twice -- one pattern only matched the hard
        stall wording and reported "all clear" through 3808 soft
        escalations; another undercounted 429s once the log was
        summarised. A number the bot writes itself cannot drift from the
        code that produces it. tools/health.sh reads this file first and
        treats the log as supporting evidence, not the source of truth.
        """
        now = time.monotonic()
        if not force and now - self._health_written < 30.0:
            return
        self._health_written = now
        rate = self._rate
        last_429 = getattr(rate, "last_429", None) if rate else None
        arr = [{"kind": c.kind, "enabled": c.enabled}
               for c in getattr(self, "_arr", [])]
        payload = {
            "build": BUILD,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "started_at": self._started_at.isoformat(),
            "cadence": {
                "refresh_seconds": self.refresh_seconds,
                "effective_seconds": round(self._effective_interval(), 1),
                "throttle": self._throttle,
                "image_throttle": self._image_throttle,
                "stalled_for": max(0.0, round(
                    getattr(self, "_stalled_until", 0.0) - now, 1)),
                "change_poll_seconds": self.change_poll_seconds,
                "webhook_enabled": bool(self.webhook.get("enabled")),
            },
            "rate_limit": {
                "count_429": getattr(rate, "count_429", 0) if rate else 0,
                "last_429_age_s": (round(now - last_429["at"], 1)
                                   if last_429 else None),
                "last_429_scope": (last_429 or {}).get("scope"),
                "absorbed_429": self._absorbed_429,
                "soft_limits_total": self._soft_limit_total,
                "escalations_total": self._escalation_total,
                "hard_stalls_total": self._stall_total,
            },
            "streams": {
                "active": bool(self._streams_active),
                "cards_on_page": len(getattr(self, "_card_sessions", []) or []),
                "page": getattr(self, "_card_page", 0),
                "pages": getattr(self, "_total_pages", 1),
            },
            "sources": {
                "plex_online": self.state.last_status,
                "artwork": getattr(self, "_artwork", "attach"),
                "arr": arr,
                "recent_downloads": bool(getattr(self, "recent_enabled",
                                                 False)),
            },
            "messages": {
                "dashboard_id": self.dashboard_message_id,
                "cards_id": self.cards_message_id,
                "cards_channel": self.cards_channel_id or None,
            },
        }
        self._write_json(os.path.join(self.data_dir, "health.json"), payload)

    @staticmethod
    def _write_json(path: str, payload) -> None:
        """Temp file + rename (util.jsonfile): a hard kill mid-write never
        leaves a truncated message-id file, which would load as "no
        message" and make the bot post a new one (a ping)."""
        write_json(path, payload)

    def _split_surfaces(self, embeds, files):
        """Partition a composed render into (main, cards) surfaces.

        _refs already records which embed each file belongs to, so the split
        follows the render rather than guessing from filenames. Nothing is
        referenced by both sides -- the dashboard uses strip_/plexicon, the
        cards use p_/anchor -- so no file has to be uploaded twice.
        """
        card_embeds = [e for e in embeds if e is not embeds[0]]
        card_set = {id(e) for e in card_embeds}
        card_names = {name for target, _kind, name in self._refs
                      if id(target) in card_set}
        main_files, cards_files = [], []
        for handle in (files or []):
            (cards_files if handle.filename in card_names
             else main_files).append(handle)
        return ([embeds[0]], main_files or None,
                card_embeds, cards_files or None)

    async def _sweep_cards(self, channel) -> None:
        """Delete strays in the cards channel, once per process.

        The bot owns this channel, so anything it posted that is not the
        current cards message is a leftover from a failed create.
        """
        self._cards_swept = True
        try:
            async for message in channel.history(limit=30):
                # Only the bot's own embed messages: stream cards are
                # embeds, while "Now on Plex" posts and other component
                # messages (if they share this channel) are not.
                if (message.author.id == self.bot.user.id
                        and message.id != self.cards_message_id
                        and message.embeds
                        and not not_ours(message)):
                    await message.delete()
                    logger.info("Deleted stray cards message %s", message.id)
        except Exception as exc:
            logger.warning("Could not sweep the cards channel: %s", exc)

    COMPLETION_PROGRESS = 98.0

    @staticmethod
    def _fit_release(name: str, budget: float,
                     size: float = textwidth.SUBTEXT_PX) -> str:
        """Trim to a PIXEL budget, keeping the episode or year.

        Same rule as _truncate_release -- the tail carries the identity, so
        cut the head -- but measured rather than counted, and at subtext
        size with the bold weight the title is actually rendered in.
        """
        text = (name or "").strip()
        if textwidth.px(text, size=size, bold=True) <= budget:
            return text
        tail = _re.search(r"\s(S\d{1,2}E\d{1,3}|\(\d{4}\))$", text)
        suffix = tail.group(0) if tail else ""
        head = text[: len(text) - len(suffix)] if suffix else text
        reserved = textwidth.px(f"\u2026{suffix}", size=size, bold=True)
        head = textwidth.truncate(head, max(20.0, budget - reserved),
                                  size=size, bold=True, ellipsis="")
        return f"{head.rstrip()}\u2026{suffix}"

    @staticmethod
    def _truncate_release(name: str, limit: int) -> str:
        """Trim the title, never the episode or year.

        Plain truncation turned "It's Always Sunny in Philadelphia S18E01"
        and "...S18E02" into the same string -- two rows that differ only in
        the one field that says which is which. The tail carries the
        identity, so cut the head and keep it.
        """
        text = (name or "").strip()
        if len(text) <= limit:
            return text
        tail = _re.search(r"\s(S\d{1,2}E\d{1,3}|\(\d{4}\))$", text)
        if not tail:
            return text[: limit - 1].rstrip() + "\u2026"
        suffix = tail.group(0)
        head_room = limit - len(suffix) - 1
        if head_room < 4:                 # nothing meaningful would survive
            return text[: limit - 1].rstrip() + "\u2026"
        return text[:head_room].rstrip() + "\u2026" + suffix

    @staticmethod
    def _ago(timestamp) -> str:
        """Compact age -- "13m", "2h", "3d". Empty when unknown."""
        try:
            seconds = time.time() - float(timestamp or 0)
        except (TypeError, ValueError):
            return ""
        if not timestamp or seconds < 0:
            return ""
        if seconds < 60:
            return "now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h"
        return f"{int(seconds // 86400)}d"

    @staticmethod
    def _match_key(name) -> str:
        """Letters and digits only, lowercased -- a release-agnostic key."""
        return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

    @staticmethod
    def _same_release(a: str, b: str) -> bool:
        """Whether two normalised names describe the same download.

        A clean title is a PREFIX of its release name once punctuation is
        stripped: "duneparttwo2024" opens
        "duneparttwo20242160puhdblurayx265terminal". Prefix rather than
        substring, because a substring test matches far too eagerly -- any
        short title would collide with an unrelated release that happens to
        contain those letters.
        """
        if not a or not b:
            return False
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        # Guard against a very short key swallowing everything.
        return len(shorter) >= 8 and longer.startswith(shorter)

    def _observe_queue(self, downloads) -> None:
        """Notice completions by watching jobs leave the queue.

        SABnzbd's history is the obvious source and it is empty here:
        Sonarr and Radarr delete the entry the moment they import, so by the
        time anything asks, the record is gone. What cannot be deleted out
        from under us is the queue itself, which this bot already polls
        every few seconds.

        So: remember every job and how far along it was. When an id
        disappears, it either finished or was cancelled, and the last
        progress reading says which. A job pulled at 40% was abandoned; one
        last seen at ~100% completed. Cancelling something at 99% would
        register as a completion -- an acceptable error for a panel whose
        job is to say what landed.
        """
        if not self.recent_enabled:
            return
        slots = (downloads or {}).get("slots")
        if slots is None:
            return                       # queue unreadable; assume nothing
        current = {}
        for slot in slots:
            key = str(slot.get("nzo_id") or "")
            if key:
                current[key] = slot

        changed = False
        for key, last in list(self._queue_seen.items()):
            if key in current:
                continue
            self._queue_seen.pop(key, None)
            if float(last.get("progress") or 0) < self.COMPLETION_PROGRESS:
                logger.info("Job left the queue at %.0f%% -- treating as "
                            "cancelled, not completed: %s",
                            float(last.get("progress") or 0), last.get("name"))
                continue
            self._completed.append({
                "name": last.get("name") or "Unknown",
                "size": last.get("size") or "",
                "completed": time.time(),
            })
            logger.info("Download completed: %s", last.get("name"))
            changed = True

        for key, slot in current.items():
            self._queue_seen[key] = {"name": slot.get("name"),
                                     "size": slot.get("size"),
                                     "progress": slot.get("progress")}
        if changed:
            self._prune_completed()
            self._write_json(self.completed_file,
                             {"items": self._completed})
            self._recent_cache = None    # surface it on the next build

    def _prune_completed(self) -> None:
        """Age out old entries and cap the ledger."""
        if self.recent_max_age_hours > 0:
            cutoff = time.time() - self.recent_max_age_hours * 3600.0
            self._completed = [c for c in self._completed
                               if float(c.get("completed") or 0) >= cutoff]
        # Keep more than is displayed: max_age may hide some, and a bigger
        # limit later should not need data that was thrown away.
        self._completed = self._completed[-50:]

    async def _recent_items(self):
        """(items, note) for the recently-completed panel.

        Always returns a usable pair. The panel is present whenever it is
        enabled: a section that vanishes when it has nothing to report reads
        as broken rather than quiet, and its absence is indistinguishable
        from the bot being down.
        """
        # Cached, because build_embed now runs on the page-flip path too.
        # A flip is a person waiting on a button; making it wait on a
        # SABnzbd round-trip would add latency to the one interaction in
        # this bot that is measured in human patience rather than minutes.
        now = time.monotonic()
        if (self._recent_cache is not None
                and now - self._recent_cache_at < self.recent_cache_seconds):
            return self._recent_cache

        cog = self.bot.get_cog("SABnzbd")
        if cog is not None and not getattr(cog, "enabled", False):
            cog = None
        arr_enabled = any(c.enabled for c in self._arr)
        if cog is None and not arr_enabled:
            # Nothing could ever fill it: no panel at all, rather than a
            # permanent "not configured" on a Plex-only install.
            return None
        # Source order is deliberate: *arr knows the import succeeded and
        # knows the real title, SABnzbd knows the download finished, and the
        # local ledger knows only that a job left the queue. Each fills what
        # the one before it cannot see.
        done: List[Dict[str, Any]] = []
        results = await asyncio.gather(
            *[c.imported(self.recent_limit) for c in self._arr if c.enabled],
            return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Arr history failed: %s", result)
                continue
            done.extend(result)

        # With *arr configured, *arr is the ONLY source. The SAB history and
        # the queue ledger both answer "did the download finish", and a
        # download that finished and then FAILED IMPORT passes both tests --
        # it left the queue at 100% -- while producing no watchable file.
        # Only the app that performs the import knows whether it succeeded,
        # so when that app is available, weaker witnesses stay silent. They
        # remain the fallback for setups with no *arr at all.
        use_fallbacks = cog is not None and not (
            arr_enabled and self.recent_require_import)
        if use_fallbacks:
            try:
                done.extend(await cog.get_history(self.recent_limit,
                                                  self.recent_max_age_hours))
            except Exception as exc:                   # never break the loop
                logger.warning("Recent downloads lookup failed: %s", exc)

        # History first -- it is SABnzbd's own verdict, and it carries the
        # fail_message that separates "finished" from "gave up". The locally
        # observed ledger fills the gap when history has been deleted by
        # whatever imported the download.
        self._prune_completed()
        merged: List[Dict[str, Any]] = []
        seen: List[str] = []
        # Sources describe the same download differently -- Radarr says
        # "Dune: Part Two (2024)", SABnzbd says
        # "Dune.Part.Two.2024.2160p.UHD.BluRay.x265-TERMiNAL" -- so an exact
        # name match finds nothing and the panel lists everything twice.
        ledger = ([dict(c) for c in reversed(self._completed)]
                  if use_fallbacks else [])
        for item in list(done) + ledger:
            key = self._match_key(item.get("name"))
            if not key or any(self._same_release(key, other)
                              for other in seen):
                continue
            seen.append(key)
            merged.append(item)
        merged.sort(key=lambda d: float(d.get("completed") or 0), reverse=True)
        merged = merged[: self.recent_limit]

        for item in merged:
            item["display"] = (cog.clean_name(item["name"])
                               if cog is not None and hasattr(cog, "clean_name")
                               else item["name"])
        self._recent_cache = (merged, "")
        self._recent_cache_at = now
        return self._recent_cache

    async def _recent_embed(self):
        """Recently finished downloads, or None when there is nothing to say."""
        recent = await self._recent_items()
        if recent is None:
            return None, None
        done, note = recent

        # Signature over identity only. Sizes and names do not drift, so this
        # message edits when a job finishes and at no other time -- which is
        # what keeps a second message in the dashboard's channel cheap.
        signature = json.dumps(
            {"items": [f"{d['name']}|{int(d['completed'])}" for d in done],
             "note": note}, sort_keys=True)

        embed = discord.Embed(
            title=self.recent_title,
            colour=STATUS_COLOURS["online"],
        )
        if not done:
            embed.description = (
                f"{self._emoji('idle')} *{note or 'Nothing finished recently'}*"
                .strip())
            return embed, signature

        lines = []
        for item in done:
            name = item.get("display") or item["name"]
            bits = []
            if item.get("size"):
                bits.append(f"`{item['size']}`")
            if item.get("completed"):
                bits.append(discord_timestamp(
                    datetime.fromtimestamp(item["completed"], tz=timezone.utc),
                    "R"))
            if item.get("category"):
                bits.append(str(item["category"]))
            lines.append(f"**{self._truncate(name)}**\n-# "
                         + " · ".join(bits))
        embed.description = "\n".join(lines)[:4000]
        return embed, signature

    def _emit_recent_field(self, embed) -> None:
        """Finished downloads, as one block directly under Downloading.

        Sits with the queue rather than at the foot of the embed: the two are
        the same subsystem at two points in time, and a reader looking at
        what is downloading is the same reader who wants to know what just
        landed. Uses the prefetch stored by build_embed, because the field
        order is decided in synchronous code.
        """
        if self._recent_pending is None:
            return
        if len(embed.fields) >= MAX_EMBED_FIELDS - 1:
            return
        done, note = self._recent_pending
        if done and self.recent_style == "line":
            # One subtext line per item: title, size, age. Half the height of
            # the two-line form and none of the code block's problems -- the
            # text is proportional, so it uses the width it actually needs
            # instead of a fixed grid, and a live timestamp works here
            # because Discord only refuses to substitute <t:> inside a block.
            # Measured in PIXELS, not characters. A character count is a
            # poor proxy for proportional text -- "It's Always Sunny in
            # Philadelphia" and "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW" are the
            # same length and nothing like the same width -- which is why
            # every character-based budget so far has still wrapped.
            room = self.recent_line_px or (EMBED_CONTENT_PX
                                           - CARD_WIDTH_MARGIN)
            lines = []
            for item in done:
                bits = []
                if item.get("size") and self.recent_show_size:
                    bits.append(str(item["size"]))
                if item.get("completed"):
                    bits.append(
                        discord_timestamp(
                            datetime.fromtimestamp(item["completed"],
                                                   tz=timezone.utc), "R")
                        if self.recent_live_age
                        else self._ago(item.get("completed")))
                tail = " · ".join(bits)
                # A live timestamp measures nothing useful as source text --
                # "<t:1755...:R>" renders as "16 minutes ago" -- so budget
                # for the rendered form instead of the tag.
                measured = tail
                if self.recent_live_age:
                    # "<t:1755...:R>" measures nothing useful as source
                    # text; budget for the longest form it renders as.
                    measured = " · ".join(
                        ([str(item["size"])]
                         if item.get("size") and self.recent_show_size
                         else []) + ["16 minutes ago"])
                spent = textwidth.px(f" · {measured}",
                                     size=textwidth.SUBTEXT_PX)
                name = self._fit_release(str(item["display"]),
                                         max(60.0, room - spent))
                lines.append(f"-# **{name}**"
                             + (f" · {tail}" if tail else ""))
            body = "\n".join(lines)
        elif done and self.recent_boxed:
            # One fenced block, columns aligned, matching the boxed values
            # everywhere else on this embed. The cost is the live timestamp:
            # Discord only substitutes <t:...> in plain text, so inside a
            # block the age has to be baked at render time. Acceptable here
            # -- "13m" is still true minutes later, unlike a countdown --
            # and the embed re-renders on every change anyway.
            rows = []
            for item in done:
                rows.append((
                    str(item["display"]),
                    str(item.get("size") or ""),
                    self._ago(item.get("completed")),
                ))
            size_col = max((len(r[1]) for r in rows), default=0)
            age_col = max((len(r[2]) for r in rows), default=0)
            # Derive the name column from the LINE budget rather than
            # setting it directly. The two numeric columns are narrow and
            # predictable; the title is the only thing that can be given
            # whatever is left, and sizing it any other way means the total
            # drifts past the wrap point whenever a size gains a digit.
            name_col = max(12, self.recent_width_chars
                           - size_col - age_col - 4)
            rows = [(self._truncate_release(name, name_col), size, age)
                    for name, size, age in rows]
            name_col = max((len(r[0]) for r in rows), default=0)
            body = "```" + "\n".join(
                f"{name:<{name_col}}  {size:>{size_col}}  {age:>{age_col}}"
                .rstrip() for name, size, age in rows) + "```"
        elif done:
            lines = []
            for item in done:
                bits = []
                if item.get("size"):
                    bits.append(f"`{item['size']}`")
                if item.get("completed"):
                    bits.append(discord_timestamp(
                        datetime.fromtimestamp(item["completed"],
                                               tz=timezone.utc), "R"))
                lines.append(f"**{self._truncate(item['display'])}**\n-# "
                             + " · ".join(bits))
            body = "\n".join(lines)
        else:
            body = (f"{self._emoji('idle')} "
                    f"*{note or 'Nothing finished recently'}*").strip()
        if len(body) > FIELD_LIMIT - self.GAP_ALLOWANCE:
            body = body[: FIELD_LIMIT - self.GAP_ALLOWANCE - 1].rstrip() + "\u2026"
        self._titled_field(embed, "completed", self.recent_title, body)

    async def _push_recent(self, channel) -> bool:
        """Own message, own lifecycle, edited only when the set changes.

        Deliberately NOT a second embed on the dashboard message. That
        message is one embed and one strip on purpose -- it is the shape that
        stopped drawing 429s -- and bolting a second embed onto it would
        change the exact variable the channel split was made to control.
        A separate message shares the channel's rate-limit bucket, but it
        edits only when a download finishes rather than every tick, so its
        cost is a handful of edits a day instead of one a minute.
        """
        embed, signature = await self._recent_embed()
        if embed is None:
            if signature is None and self.recent_message_id:
                # No source can fill it any more: take the old one down.
                try:
                    old = await channel.fetch_message(self.recent_message_id)
                    await old.delete()
                except discord.HTTPException:
                    pass
                self.recent_message_id = None
                self._recent_payload = None
                self._write_json(self.recent_id_file, {"message_id": None})
            return False
        if signature == self._recent_payload and self.recent_message_id:
            return True                       # nothing finished since last time

        message = None
        if self.recent_message_id:
            try:
                message = await channel.fetch_message(self.recent_message_id)
            except discord.NotFound:
                self.recent_message_id = None
            except discord.HTTPException as exc:
                logger.warning("Could not fetch the recent-downloads message: %s",
                               exc)
                return False

        # Same width trick as the dashboard: a 1024px image forces the embed
        # to the client's maximum, so this panel and the dashboard above it
        # line up instead of each sizing to its own longest line.
        strip_name = "strip_recent.png"
        strip = None
        if self.recent_strip and cardimage.AVAILABLE:
            existing = next((a for a in (message.attachments if message else [])
                             if a.filename == strip_name), None)
            if existing is not None:
                # Re-use the URL Discord already issued. Re-attaching would
                # re-upload identical bytes and re-sign the URL, which is what
                # made posters flicker on every edit.
                embed.set_image(url=existing.url)
            else:
                strip = discord.File(
                    io.BytesIO(cardimage.strip_png(
                        STATUS_COLOURS["online"])), filename=strip_name)
                embed.set_image(url=f"attachment://{strip_name}")
        try:
            if message is None:
                message = await channel.send(
                    embed=embed, file=strip) if strip else await channel.send(
                        embed=embed)
                self.recent_message_id = message.id
                self._write_json(self.recent_id_file,
                                 {"message_id": message.id})
                logger.info("Created recent-downloads message %s", message.id)
            elif strip is not None:
                await asyncio.wait_for(
                    message.edit(embed=embed, attachments=[strip]),
                    timeout=EDIT_TIMEOUT)
            else:
                await asyncio.wait_for(
                    message.edit(embed=embed,
                                 attachments=list(message.attachments)),
                    timeout=EDIT_TIMEOUT)
        except (asyncio.TimeoutError, discord.HTTPException) as exc:
            logger.warning("Recent-downloads edit failed: %s", exc)
            return False
        self._recent_payload = signature
        return True

    def _idle_card_embed(self):
        """The placeholder that keeps the cards message alive between streams."""
        return discord.Embed(
            description=(f"{self._emoji('idle')} "
                         f"*{self.cards_idle_text}*").strip(),
            colour=STATUS_COLOURS["offline"]
            if self.state.last_status is False else STATUS_COLOURS["online"],
        )

    async def _show_idle_card(self, channel) -> bool:
        """Park the cards message on a placeholder instead of deleting it.

        A Discord message notifies its channel when it is CREATED, never when
        it is edited. Deleting on idle therefore guaranteed a ping at the
        start of every viewing session. Holding one message forever and
        editing it between "nothing playing" and the live cards means the
        channel is silent after the very first post.

        Attachments are cleared here so the last set of posters does not sit
        under the placeholder. Only card-side files are unpinned -- the
        dashboard's own strip and icon live in the same URL map and are still
        in use on the other message.
        """
        if self._cards_idle and self.cards_message_id:
            return True  # already parked; an identical edit is pure churn

        message = None
        if self.cards_message_id:
            try:
                message = await channel.fetch_message(self.cards_message_id)
            except discord.NotFound:
                self.cards_message_id = None
            except discord.HTTPException as exc:
                logger.warning("Could not fetch the cards message: %s", exc)
                return False

        try:
            if message is None:
                message = await channel.send(embeds=[self._idle_card_embed()])
                self.cards_message_id = message.id
                self._write_json(self.cards_id_file,
                                 {"message_id": message.id})
                logger.info("Created idle cards message %s in #%s",
                            message.id, getattr(channel, "name", "?"))
            else:
                await asyncio.wait_for(
                    message.edit(embeds=[self._idle_card_embed()],
                                 attachments=[], view=None),
                    timeout=EDIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Idle cards edit timed out after %.0fs",
                           EDIT_TIMEOUT)
            return False
        except discord.HTTPException as exc:
            logger.warning("Idle cards edit failed: %s", exc)
            return False

        self._cards_idle = True
        self._cards_payload = None
        for name in [n for n in self._attachment_urls
                     if n.startswith(("p_", "anchor", "bar"))]:
            self._attachment_urls.pop(name, None)
            self._attachment_pinned.pop(name, None)
        return True

    async def _push_cards(self, channel, embeds, files, view,
                          editor=None) -> bool:
        """Edit (or create) the stream-cards message in its own channel.

        See _show_idle_card for why the empty case edits rather than deletes.

        Deliberately thinner than _push: no repost clock, no channel dot, no
        throttle bookkeeping. Those belong to the dashboard, which is the
        surface that must never go stale. If the cards fall behind, the
        numbers people actually rely on are still current.
        """
        if not embeds:
            # Nothing playing. Deleting the message here is what made the
            # channel ping on every new stream: a deleted message cannot be
            # edited, so the next playback had to SEND, and a send is a new
            # message -- which notifies. Editing a permanent placeholder in
            # place notifies nobody, so the channel stays quiet from the
            # first stream of the day to the last.
            if self.cards_idle_embed:
                return await self._show_idle_card(channel)
            if self.cards_message_id:
                try:
                    message = await channel.fetch_message(self.cards_message_id)
                    await message.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
                self.cards_message_id = None
                self._cards_payload = None
                self._write_json(self.cards_id_file, {"message_id": None})
            return True
        self._cards_idle = False

        message = None
        if self.cards_message_id:
            try:
                message = await channel.fetch_message(self.cards_message_id)
            except discord.NotFound:
                logger.info("Cards message vanished -- posting a new one")
                self.cards_message_id = None
            except discord.HTTPException as exc:
                logger.warning("Could not fetch the cards message: %s", exc)
                return False

        if message is not None and not self._attachment_urls:
            self._attachment_urls.update(
                {a.filename: a.url for a in message.attachments})
            stamped = time.monotonic()
            for name in self._attachment_urls:
                self._attachment_pinned.setdefault(name, stamped)

        try:
            if message is None:
                message = await channel.send(embeds=embeds,
                                             files=files or [], view=view)
                self.cards_message_id = message.id
                self._write_json(self.cards_id_file, {"message_id": message.id})
                logger.info("Created cards message %s in #%s",
                            message.id, getattr(channel, "name", "?"))
            else:
                do_edit = editor or message.edit
                if files is None:
                    await asyncio.wait_for(do_edit(
                        embeds=embeds,
                        attachments=list(message.attachments),
                        view=view), timeout=EDIT_TIMEOUT)
                else:
                    keep = [a for a in message.attachments
                            if a.filename in self._keep_names]
                    message = await asyncio.wait_for(do_edit(
                        embeds=embeds, attachments=keep + files, view=view),
                        timeout=EDIT_TIMEOUT)
                    sent = {f.filename for f in files}
                    for a in message.attachments:
                        if (a.filename in sent
                                or a.filename not in self._attachment_urls):
                            self._attachment_urls[a.filename] = a.url
                            self._attachment_pinned[a.filename] = time.monotonic()
        except asyncio.TimeoutError:
            logger.warning("Cards edit timed out after %.0fs", EDIT_TIMEOUT)
            return False
        except discord.NotFound:
            self.cards_message_id = None
            return False
        except discord.HTTPException as exc:
            logger.warning("Cards edit failed: %s", exc)
            return False
        return True

    async def _adopt_card_attachments(self) -> None:
        """Pin the cards message's existing files before anything composes.

        Only runs while the url map has nothing for the cards -- i.e. once,
        on the first pass after a restart. Failure is not fatal: the worst
        case is the re-upload we were already doing.
        """
        if not self.cards_channel_id or not self.cards_message_id:
            return
        if any(n.startswith(("p_", "anchor", "bar"))
               for n in self._attachment_urls):
            return                      # already pinned this process
        channel = self.bot.get_channel(self.cards_channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(self.cards_message_id)
        except (discord.NotFound, discord.HTTPException) as exc:
            logger.debug("Could not adopt card attachments: %s", exc)
            return
        stamped = time.monotonic()
        adopted = 0
        for attachment in message.attachments:
            if attachment.filename in self._attachment_urls:
                continue
            self._attachment_urls[attachment.filename] = attachment.url
            self._attachment_pinned[attachment.filename] = stamped
            adopted += 1
        if adopted:
            self._adopted_at = stamped
            logger.info("Adopted %d card attachment(s) -- no poster "
                        "re-upload on this restart", adopted)

    async def _refresh_locked(self, channel) -> None:
        # Fetch the message and adopt its attachments BEFORE composing.
        # _compose decides what to upload from _attachment_urls, so running
        # adoption afterwards (as an earlier build did) meant the first tick
        # after every restart had already committed to a full re-upload --
        # the adoption block never saved anything.
        message = None
        if self.dashboard_message_id:
            try:
                message = await channel.fetch_message(self.dashboard_message_id)
            except discord.NotFound:
                logger.warning("Dashboard message vanished -- posting a new one")
                self.dashboard_message_id = None
            except discord.HTTPException as exc:
                logger.warning("Could not fetch the dashboard message: %s", exc)
                return

        # Retire the message before Discord starts refusing to edit it.
        #
        # Measured on a live server: a message edited ~5,000 times over 3.5
        # days drew steadily MORE shared-scope 429s each hour while the
        # bot's own bucket sat at 4/5 and its request rate was the lowest it
        # had ever been. Discord meters edits against the message as it
        # ages (error 30046 is the documented form; shared-scope 429s appear
        # to be another), so no amount of pacing fixes an old message --
        # only a new one does. A repost costs one upload set, once a day.
        if message is not None and self.repost_seconds:
            age = (discord.utils.utcnow() - message.created_at).total_seconds()
            if age >= self.repost_seconds:
                logger.info(
                    "Dashboard message is %.1fh old -- reposting for a fresh "
                    "edit budget", age / 3600,
                )
                await self._replace_message(channel, message)
                return

        if message is not None and not self._attachment_urls:
            self._attachment_urls = {
                a.filename: a.url for a in message.attachments
            }
            stamped = time.monotonic()
            self._attachment_pinned = {n: stamped
                                       for n in self._attachment_urls}
            if self._attachment_urls:
                # Bar content is unknown after a restart; under throttle
                # their first repaint measures from this moment.
                self._adopted_at = time.monotonic()
                logger.info(
                    "Adopted %d existing attachment(s) -- no re-upload "
                    "needed on this restart", len(self._attachment_urls),
                )

        # The CARDS message needs the same treatment, and did not get it.
        # Its adoption lived inside _push_cards, which runs AFTER _compose --
        # by which point the upload set is already decided, so every restart
        # re-uploaded every poster. That is a burst of attachment-carrying
        # edits in the first minutes after boot, which is exactly the shape
        # of the 429 storms: they start ~90s after startup and stop once the
        # posters are back on the message.
        await self._adopt_card_attachments()

        try:
            embeds, files, view = await self._compose()
        except Exception:
            logger.exception("Failed to build dashboard embed")
            return

        await self._update_channel_dot(channel, self.state.last_status)

        try:
            # The embed carries a footer timestamp set to "now", which would
            # make every fingerprint unique and defeat the whole check. Drop
            # it before hashing so this compares CONTENT, not clock.
            payloads = []
            for embed in embeds:
                data = embed.to_dict()
                data.pop("timestamp", None)
                payloads.append(data)
            fingerprint = json.dumps(payloads, sort_keys=True, default=str)
            if (
                files is None
                and fingerprint == self._last_payload
                and self.dashboard_message_id
            ):
                # Nothing changed. Push anyway if the heartbeat is due, so
                # the footer timestamp proves the bot is alive; otherwise
                # skip and bank the headroom. (A small tolerance keeps a
                # 60s heartbeat from slipping to every OTHER tick when the
                # loop lands a moment early.)
                since = time.monotonic() - self._last_delivered
                if not self.heartbeat_seconds:
                    return
                if self._last_delivered and since < self.heartbeat_seconds - 5:
                    return

            cards_channel = None
            if self.cards_channel_id:
                cards_channel = self.bot.get_channel(self.cards_channel_id)
                if cards_channel is None:
                    logger.warning(
                        "Cards channel %s not found -- keeping the cards on "
                        "the dashboard message", self.cards_channel_id)

            if cards_channel is not None:
                main_e, main_f, card_e, card_f = self._split_surfaces(
                    embeds, files)
                delivered = await self._push(channel, message, main_e,
                                             main_f, None)
                # Cards get their own fingerprint: the dashboard changes
                # every tick (uptime, clock) while a card can sit unchanged
                # for minutes, and editing it anyway is spend for nothing.
                # The pager is part of the cards message. Fingerprinting only
                # the embeds meant a change that moved JUST the buttons (the
                # 11th stream ending while page 1 sat unchanged) was skipped,
                # leaving "Page 1/2 · 11 streams" and a live Next button.
                card_print = json.dumps(
                    {"embeds": [e.to_dict() for e in card_e],
                     "view": view.to_components() if view else None},
                    sort_keys=True, default=str)
                if card_f is not None or card_print != self._cards_payload:
                    if not self._cards_swept:
                        await self._sweep_cards(cards_channel)
                    if await self._push_cards(cards_channel, card_e,
                                              card_f, view):
                        self._cards_payload = card_print
                        self._rendered_page = self._card_page
            else:
                delivered = await self._push(channel, message, embeds,
                                             files, view)
                if delivered:
                    self._rendered_page = self._card_page
            if self.recent_enabled and not self.recent_in_main:
                # In-main mode already rendered it as a field; pushing a
                # second copy would say the same thing twice.
                await self._push_recent(channel)
            # Recorded only when the content actually reached Discord. A
            # stalled or rejected edit must stay eligible for retry, or the
            # dedupe check would suppress the failed content forever.
            if delivered:
                self._last_payload = fingerprint
                self._last_delivered = time.monotonic()
            # This render already shows whatever page a pending flip wanted,
            # so drop it rather than edit the same content again.
            if self._flip_task is not None and not self._flip_task.done():
                self._flip_task.cancel()
            self._last_flip_render = time.monotonic()
        except discord.HTTPException as exc:
            logger.error("Discord rejected the dashboard update: %s", exc)

    @update_dashboard.before_loop
    async def _before_dashboard(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_channel_dot(self, channel, online) -> None:
        """Keep a status dot at the front of the channel name.

        Discord hard-limits channel renames to 2 per 10 minutes, so this
        edits only when the leading dot is actually wrong -- i.e. on status
        transitions and on first adoption, never on routine refreshes.
        """
        cfg = self.channel_dot
        if not cfg["enabled"] or online is None:
            return
        want = cfg["online"] if online else cfg["offline"]
        if not want:
            return

        # Normalisation-proof short-circuit. The old test compared the WHOLE
        # rebuilt name against channel.name, which fails permanently the
        # moment Discord stores something different from what was sent --
        # text-channel names get lower-cased and spaces become dashes, so a
        # name with any capital in it never compares equal. That turned a
        # rename-on-transition into a rename EVERY tick, which burns the
        # 2-per-10-minutes budget, and the rename that actually mattered --
        # the status flip -- then had nothing left and silently timed out.
        # The only thing worth testing is whether the right dot is already
        # on the front.
        if channel.name.startswith(want):
            return

        # Protect the rename budget: at most one attempt per window, so a
        # rejected or normalised rename cannot spin.
        now = time.monotonic()
        if self._last_rename_try and now - self._last_rename_try < RENAME_COOLDOWN:
            logger.debug("Channel dot deferred -- renamed %.0fs ago",
                         now - self._last_rename_try)
            return

        base = channel.name
        # Strip the configured pair AND any common status dot, repeatedly --
        # otherwise changing the dot emoji in config leaves the previous one
        # permanently glued to the channel name behind the new one.
        known_dots = {cfg["online"], cfg["offline"],
                      "🟢", "🔴", "🟡", "🟠", "🔵", "🟣", "⚪", "⚫", "🟤"}
        stripped = True
        while stripped:
            stripped = False
            for known in known_dots:
                if known and base.startswith(known):
                    base = base[len(known):]
                    stripped = True
        new_name = f"{want}{base}"[:100]
        self._last_rename_try = now
        logger.info("Channel dot is wrong (%s) -- renaming to %s",
                    channel.name, new_name)

        try:
            # Bounded: renames are limited to 2 per 10 minutes, and on a 429
            # discord.py would otherwise sleep out a penalty of many minutes
            # INSIDE this call -- while we hold the render lock, freezing
            # refreshes and button presses alike. The dot can wait a tick.
            await asyncio.wait_for(
                channel.edit(name=new_name, reason="Plex server status changed"),
                timeout=15.0,
            )
            logger.info("Channel renamed to %s", new_name)
        except asyncio.TimeoutError:
            logger.warning(
                "Channel rename is rate limited (2 per 10 min) -- the status "
                "dot will catch up on a later status transition"
            )
        except discord.Forbidden:
            if not self._warned_channel_perms:
                self._warned_channel_perms = True
                logger.warning(
                    "Cannot rename the channel -- give the bot the Manage Channels "
                    "permission (or set channel_dot.enabled to false)."
                )
        except discord.HTTPException as exc:
            logger.warning("Channel rename failed: %s", exc)

    def _seek_token(self, sessions) -> str:
        """Bumps when a viewer scrubs, and only then.

        Position cannot go in the change signature directly -- it advances
        on every poll, which would make this a fixed-interval refresh
        wearing a disguise. But a SEEK is distinguishable from ordinary
        playback: between two polls a playing stream should advance by
        exactly the wall-clock time elapsed, and a paused one not at all.
        Anything that disagrees by more than the threshold was a jump, not
        a second passing.

        The threshold has to absorb honest error -- poll jitter, Tautulli
        rounding, a client that reports position lazily -- which is why it
        is seconds rather than milliseconds. Skipping a 30-second intro
        registers; drift does not.
        """
        now = time.monotonic()
        tolerance = self.seek_threshold_seconds * 1000.0
        for stream in sessions:
            key = str(stream.get("session_key") or "")
            if not key:
                continue
            try:
                offset = float(stream.get("view_offset") or 0)
            except (TypeError, ValueError):
                continue
            previous = self._seek_positions.get(key)
            self._seek_positions[key] = (offset, now, bool(stream.get("paused")))
            if previous is None:
                continue
            last_offset, last_at, was_paused = previous
            # A paused stream should not have moved at all; a playing one
            # should have moved by exactly the elapsed wall clock.
            expected = last_offset + (
                0.0 if was_paused else (now - last_at) * 1000.0)
            if abs(offset - expected) > tolerance:
                self._seek_serial += 1
                logger.info(
                    "Seek detected on session %s (%+.0fs) -- refreshing",
                    key, (offset - expected) / 1000.0)
        # Forget streams that ended, so a recycled session key cannot be
        # compared against a position from a different piece of media.
        live = {str(s.get("session_key") or "") for s in sessions}
        for gone in [k for k in self._seek_positions if k not in live]:
            self._seek_positions.pop(gone, None)
        return str(self._seek_serial)

    def _change_signature(self, activity, downloads) -> str:
        """What counts as "something happened".

        Stream identity and state, the download queue's shape, and seeks.
        Not raw progress, elapsed time or bandwidth -- those change on every
        poll and would turn this into a fixed-interval refresh with extra
        steps.
        """
        sessions = (activity or {}).get("sessions") or []
        was_active = self._streams_active
        self._streams_active = bool(sessions)
        if was_active != self._streams_active:
            # Cadence changes with it, or the tightened interval would only
            # take effect on whatever tick happened to come next.
            self._apply_interval()
            logger.info(
                "Streams %s -- refresh cadence now %.0fs",
                "started" if self._streams_active else "stopped",
                self._effective_interval())
        streams = sorted(
            f"{s.get('session_key')}|{s.get('title')}|{s.get('series')}"
            f"|{1 if s.get('paused') else 0}|{s.get('decision')}"
            for s in sessions
        )
        seek = self._seek_token(sessions) if self.seek_detection else "0"
        queue = (downloads or {}).get("downloads") or []
        jobs = sorted(f"{d.get('name')}|{d.get('status')}" for d in queue)
        return json.dumps({
            "streams": streams,
            "seek": seek,
            "jobs": jobs,
            "queued": (downloads or {}).get("queued_total"),
            "online": (activity is not None),
        }, sort_keys=True)

    @tasks.loop(seconds=20)
    async def watch_changes(self) -> None:
        """Refresh the moment the world changes, not when the clock says to.

        Playback starting, stopping or pausing; a download appearing or
        finishing; Plex going away. The periodic loop stays as the floor,
        so this only ever pulls an edit FORWARD -- it never adds one for
        content that has not moved.
        """
        if not self.change_poll_seconds:
            return
        try:
            activity, downloads = await asyncio.gather(
                self._fetch_activity(), self._fetch_downloads(),
                return_exceptions=False,
            )
        except Exception:
            return  # a probe failing is not an event
        try:
            signature = self._change_signature(activity, downloads)
        except Exception:
            logger.debug("change signature failed", exc_info=True)
            return
        if self._last_change_sig is None:
            self._last_change_sig = signature
            return
        if signature == self._last_change_sig:
            return
        self._last_change_sig = signature
        logger.info("State changed -- refreshing now")
        try:
            await self.update_dashboard()
        except Exception:
            logger.exception("Change-driven refresh failed")

    @watch_changes.before_loop
    async def _before_watch(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def sync_uptime_history(self) -> None:
        """Reconcile the uptime counter against Uptime Kuma's transition log.

        Runs at startup and then rarely. Kuma's login rate limiter is a
        single global bucket of 20 per minute, and logging in makes the
        server push its entire monitor list, so this is deliberately
        infrequent rather than per-tick.
        """
        if self.uptimerobot:
            # The transition log rides along with the uptime call, so this
            # costs nothing extra -- but it needs one fetch to have landed.
            try:
                if not self.uptimerobot._last_logs:
                    await self._fetch_uptime()
                result = self.uptimerobot.transitions()
            except Exception:
                logger.exception("UptimeRobot history sync failed")
                return
            if result is None:
                logger.info("UptimeRobot has no transition log yet; "
                            "using locally tracked uptime")
                return
            last_down, up_since = result
            if last_down is None and self.state.up_since is not None:
                # No outage in UptimeRobot's log means its up_since is only
                # the monitor's creation date (or its first "up" entry) --
                # not evidence against what this bot has tracked itself.
                logger.info("UptimeRobot log has no outage; keeping locally "
                            "tracked uptime")
                return
            self.state.seed(up_since, last_down)
            logger.info("Total Uptime seeded from UptimeRobot: up since %s"
                        "%s", up_since,
                        f", last outage {last_down}" if last_down else
                        " (no recorded outage)")
            return

        if not self.kuma_history:
            return
        try:
            result = await asyncio.to_thread(self.kuma_history.fetch_transitions)
        except Exception:
            logger.exception("Uptime history sync failed")
            return
        if result is None:
            return

        last_down, up_since = result
        if up_since is None and last_down is None:
            # No transitions recorded at all -- either a brand new monitor, or
            # one that has never changed state within the retention window.
            logger.info(
                "Uptime Kuma has no recorded transitions for monitor %s; "
                "using locally tracked uptime",
                self.kuma_history.monitor_id,
            )
            return
        self.state.seed(up_since, last_down)

    @sync_uptime_history.before_loop
    async def _before_history(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def update_presence(self) -> None:
        """The floor for the bot's status. Normally the dashboard tick sets
        it from the same reading it renders (see build_embed), so the two
        always agree; this covers a dashboard that isn't ticking, and the
        periodic re-send below."""
        try:
            snap = self._snap
            if snap and (datetime.now(timezone.utc)
                         - snap["at"]).total_seconds() < 90:
                activity, online = snap["activity"], snap["online"]
                libraries = snap["libraries"]
            else:
                activity = await self._fetch_activity()
                online = activity is not None
                libraries = (await self._fetch_libraries()
                             if online and not activity.get("count") else [])
            await self._sync_presence(activity, online, libraries)
        except Exception:
            logger.exception("Failed to update presence")

    def _presence_for(self, activity: Optional[Dict[str, Any]], online: bool,
                      libraries) -> tuple:
        """(text, status) for the bot's status, from one reading."""
        presence = self.config.get("presence", {})
        if not online:
            return (presence.get("offline_text", "Server offline")[:128],
                    discord.Status.dnd)
        count = (activity or {}).get("count", 0)
        if count:
            text = presence.get("stream_text", "{count} streaming").format(
                count=count, s="s" if count != 1 else "")
            return text[:128], discord.Status.online
        by_name = {lib["name"]: lib for lib in libraries or []}
        parts = []
        for entry in presence.get("sections", []):
            library = by_name.get(entry.get("section_title", ""))
            if library:
                label = entry.get("display_name", library["name"])
                parts.append(f"{format_count(library['count'])} {label} "
                             f"{entry.get('emoji', '')}".strip())
        text = " | ".join(parts) if parts else presence.get("idle_text", "Idle")
        return text[:128], discord.Status.online

    async def _sync_presence(self, activity, online: bool, libraries) -> None:
        wanted = self._presence_for(activity, online, libraries)
        now = time.monotonic()
        # Nothing changed: skip the gateway call -- but not forever. Another
        # program sharing the bot token resets the status when it connects
        # (discord.js identifies with an empty "online" presence). Re-sending
        # now and then puts it back.
        if (wanted == self._last_presence
                and now - self._last_presence_at < PRESENCE_REFRESH):
            return
        # Discord throttles status changes; a burst of starts and stops is
        # caught up on the next tick instead.
        if now - self._last_presence_at < PRESENCE_MIN_GAP:
            return
        await self.bot.change_presence(
            activity=discord.CustomActivity(name=wanted[0]), status=wanted[1])
        self._last_presence = wanted
        self._last_presence_at = now

    @update_presence.before_loop
    async def _before_presence(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dashboard(bot))
