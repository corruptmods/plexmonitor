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
from typing import List, Optional

import discord

logger = logging.getLogger("plexmonitor.auth")


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
