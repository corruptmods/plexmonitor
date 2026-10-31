"""Who counts as a bot admin, and who counts as one of the server's people.

Admins: DISCORD_AUTHORIZED_USERS (comma-separated Discord user IDs) when
set; otherwise the bot's owner only (or its team members, for a team-owned
app). Never "everyone".

The home server is the one CHANNEL_ID (the dashboard) is in. Features that
spend money or show who is watching what are for its members only.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import discord

logger = logging.getLogger("plexmonitor.auth")

# How long a "member or not" answer is trusted before asking Discord again.
MEMBER_TTL = 600.0
# user id -> (member, when it was checked)
_members: Dict[int, Tuple[bool, float]] = {}


def authorized_ids() -> List[int]:
    return [int(part) for part in
            os.getenv("DISCORD_AUTHORIZED_USERS", "").replace(" ", "")
            .split(",") if part.isdigit()]


async def is_admin(client: discord.Client, user: discord.abc.User) -> bool:
    listed = authorized_ids()
    if listed:
        return user.id in listed
    try:
        return await client.is_owner(user)
    except Exception:
        return False


async def admin_users(client: discord.Client) -> List[discord.User]:
    """The people is_admin() accepts, for DMs that need one of them."""
    ids = authorized_ids()
    if not ids:
        try:
            info = await client.application_info()
        except discord.HTTPException as exc:
            logger.warning("Could not look up the bot's owner: %s", exc)
            return []
        if info.team is not None:
            ids = [m.id for m in info.team.members]
        elif info.owner is not None:
            ids = [info.owner.id]
    users = []
    for ident in ids:
        user = client.get_user(ident)
        if user is None:
            try:
                user = await client.fetch_user(ident)
            except discord.HTTPException:
                continue
        users.append(user)
    return users


def home_guild(client: discord.Client) -> Optional[discord.Guild]:
    """The server the dashboard channel (CHANNEL_ID) is in."""
    raw = os.getenv("CHANNEL_ID", "").strip()
    channel = client.get_channel(int(raw)) if raw.isdigit() else None
    return getattr(channel, "guild", None)


async def home_member(client: discord.Client,
                      user_id: int) -> Optional[bool]:
    """Whether this person is in the home server. None when Discord
    couldn't say (the server isn't known yet, or the lookup failed)."""
    guild = home_guild(client)
    if guild is None:
        return None
    if guild.get_member(user_id) is not None:
        return True
    try:
        await guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as exc:
        logger.info("Could not check whether %s is in %s: %s", user_id,
                    guild, exc)
        return None


async def is_member(client: discord.Client, user: discord.abc.User,
                    guild: Optional[discord.Guild] = None) -> bool:
    """Whether someone may use what's for the home server's people: the
    assistant, Seerr searches, link requests. A bot can be DMed, or added
    to other servers, by anyone; answers spend the AI key, searches use the
    Seerr admin key, and both can say who is watching what.

    `guild` is where they asked from; inside the home server they're a
    member without any lookup. When Discord can't say (the server isn't
    loaded yet right after a reconnect), they're let in rather than
    refusing everyone."""
    home = home_guild(client)
    if home is None:
        return True
    if guild is not None and guild.id == home.id:
        return True
    now = time.monotonic()
    seen = _members.get(user.id)
    if seen is not None and now - seen[1] < MEMBER_TTL:
        return seen[0]
    allowed = await home_member(client, user.id)
    if allowed is None:
        return True
    if len(_members) > 10000:
        for uid in [u for u, v in _members.items() if now - v[1] >= MEMBER_TTL]:
            _members.pop(uid, None)
    _members[user.id] = (allowed, now)
    return allowed
