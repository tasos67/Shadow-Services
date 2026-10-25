"""Shared moderation helpers used by the moderation and security cogs.

Responsibilities:
  * sequential per-guild case ids stored in the database
  * mod-log delivery (``mod_log_channel`` setting)
  * punishment DMs (``dm_punishments`` setting)
  * warning counters and warning-based escalation
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Optional

import discord

import config
import database
from utils.embeds import base_embed

log = logging.getLogger("moderation")

ACTION_COLORS: dict[str, discord.Color] = {
    "ban": config.ERROR_COLOR,
    "tempban": config.ERROR_COLOR,
    "unban": config.SUCCESS_COLOR,
    "kick": config.DANGER_COLOR,
    "softban": config.DANGER_COLOR,
    "timeout": config.WARNING_COLOR,
    "untimeout": config.SUCCESS_COLOR,
    "warn": config.WARNING_COLOR,
    "clearwarnings": config.SUCCESS_COLOR,
    "purge": config.INFO_COLOR,
    "note": config.INFO_COLOR,
}

ACTION_EMOJIS: dict[str, str] = {
    "ban": "🔨",
    "tempban": "⏳",
    "unban": "♻️",
    "kick": "👢",
    "softban": "🧹",
    "timeout": "⏱️",
    "untimeout": "🔊",
    "warn": "⚠️",
    "clearwarnings": "🧽",
    "purge": "🗑️",
    "note": "📝",
    "automod": "🤖",
}

# Default warning escalation: 3 warnings -> 1h timeout, 5 -> kick, 7 -> ban
DEFAULT_ESCALATION: list[dict] = [
    {"warnings": 3, "action": "timeout", "duration": 3600},
    {"warnings": 5, "action": "kick", "duration": 0},
    {"warnings": 7, "action": "ban", "duration": 0},
]


def action_color(action: str) -> discord.Color:
    """Colour matching a moderation action."""
    return ACTION_COLORS.get(action, config.THEME_COLOR)


def action_emoji(action: str) -> str:
    """Emoji matching a moderation action."""
    return ACTION_EMOJIS.get(action, "🛡️")


async def create_case(
    guild: discord.Guild,
    *,
    user_id: int,
    moderator_id: int,
    action: str,
    reason: Optional[str] = None,
    duration: Optional[str] = None,
    expires_at: Optional[int] = None,
) -> int:
    """Store a moderation case and return its per-guild case id."""
    case_id = await database.db.next_case_id(guild.id)
    await database.db.execute(
        "INSERT INTO cases (guild_id, case_id, user_id, mod_id, action, reason, duration, expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild.id, case_id, user_id, moderator_id, action, reason, duration, expires_at, int(time.time())),
    )
    return case_id


def case_embed(
    case_id: int,
    *,
    user: discord.abc.User,
    moderator: discord.abc.User,
    action: str,
    reason: Optional[str],
    duration: Optional[str] = None,
    extra: Optional[list[tuple[str, str, bool]]] = None,
) -> discord.Embed:
    """Standard embed for a moderation case."""
    fields: list[tuple[str, str, bool]] = [
        ("User", f"{user.mention} (`{user.id}`)", True),
        ("Moderator", f"{moderator.mention} (`{moderator.id}`)", True),
        ("Action", action.title(), True),
    ]
    if duration:
        fields.append(("Duration", duration, True))
    fields.append(("Reason", str(reason or "No reason provided"), False))
    for item in extra or ():
        fields.append(item)
    return base_embed(
        title=f"{action_emoji(action)} Case #{case_id} — {action.title()}",
        color=action_color(action),
        fields=fields,
    )


async def send_mod_log(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send a moderation embed to the configured mod-log channel."""
    channel_id = await database.db.get(guild.id, "mod_log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Could not deliver mod-log in guild %s", guild.id)


async def dm_punishment(guild: discord.Guild, user: discord.abc.User, embed: discord.Embed) -> bool:
    """DM a punishment notice when ``dm_punishments`` is enabled (default on)."""
    if not await database.db.get_bool(guild.id, "dm_punishments", True):
        return False
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def log_action(
    guild: discord.Guild,
    *,
    user: discord.abc.User,
    moderator: discord.abc.User,
    action: str,
    reason: Optional[str],
    duration: Optional[str] = None,
    expires_at: Optional[int] = None,
    dm: bool = True,
    extra: Optional[list[tuple[str, str, bool]]] = None,
) -> int:
    """Create a case, log it to the mod-log channel and optionally DM the user."""
    case_id = await create_case(
        guild,
        user_id=user.id,
        moderator_id=moderator.id,
        action=action,
        reason=reason,
        duration=duration,
        expires_at=expires_at,
    )
    await send_mod_log(
        guild,
        case_embed(case_id, user=user, moderator=moderator, action=action, reason=reason, duration=duration, extra=extra),
    )
    if dm and action not in {"unban", "untimeout", "clearwarnings"}:
        await dm_punishment(
            guild,
            user,
            base_embed(
                title=f"You were {action.replace('temp', 'temporarily ')} in {guild.name}",
                description=(
                    f"**Reason:** {reason or 'No reason provided'}"
                    + (f"\n**Duration:** {duration}" if duration else "")
                ),
                color=action_color(action),
            ),
        )
    return case_id


async def get_warning_count(guild_id: int, user_id: int) -> int:
    """Number of active warnings for a member."""
    row = await database.db.fetchone(
        "SELECT COUNT(*) AS c FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    return int(row["c"]) if row else 0


async def get_escalation_rules(guild_id: int) -> list[dict]:
    """Return the configured (or default) warning escalation rules."""
    rules = await database.db.get(guild_id, "escalation_rules", DEFAULT_ESCALATION)
    if not isinstance(rules, list) or not rules:
        return DEFAULT_ESCALATION
    cleaned: list[dict] = []
    for rule in rules:
        try:
            cleaned.append(
                {
                    "warnings": int(rule["warnings"]),
                    "action": str(rule["action"]),
                    "duration": int(rule.get("duration") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return cleaned or DEFAULT_ESCALATION


async def apply_escalation(
    guild: discord.Guild, member: discord.Member, bot: discord.abc.User
) -> Optional[str]:
    """Apply the escalation step matching the member's warning count.

    Returns a human readable description of the applied action, or None.
    """
    count = await get_warning_count(guild.id, member.id)
    rules = await get_escalation_rules(guild.id)
    matching = [rule for rule in rules if rule["warnings"] == count]
    if not matching:
        return None
    rule = matching[0]
    action = rule["action"]
    reason = f"Automatic escalation: reached {count} warnings"
    try:
        if action == "timeout" and guild.me.guild_permissions.moderate_members:
            duration = max(60, int(rule.get("duration") or 3600))
            await member.timeout(discord.utils.utcnow() + timedelta(seconds=duration), reason=reason)
            await log_action(
                guild,
                user=member,
                moderator=bot,
                action="timeout",
                reason=reason,
                duration=f"{duration // 60} minutes",
            )
            return f"timed out for {duration // 60} minutes"
        if action == "kick" and guild.me.guild_permissions.kick_members:
            await member.kick(reason=reason)
            await log_action(guild, user=member, moderator=bot, action="kick", reason=reason)
            return "kicked"
        if action == "ban" and guild.me.guild_permissions.ban_members:
            await member.ban(reason=reason, delete_message_days=0)
            await log_action(guild, user=member, moderator=bot, action="ban", reason=reason)
            return "banned"
    except discord.HTTPException:
        log.exception("Escalation action %s failed for %s", action, member)
        return None
    return None


async def timeout_member(member: discord.Member, seconds: int, reason: str) -> None:
    """Apply a timeout using a relative number of seconds."""
    until = discord.utils.utcnow() + timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
