"""Moderation system: bans (temporary and soft), kicks, timeouts, warnings with
automatic escalation, message purges, channel locks, nickname changes and full
case tracking with configurable staff roles and mod-log channel."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.core import SetupCategory, register_setup_category
from database import db
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import format_duration, format_ts, parse_duration, parse_id, truncate
from utils.moderation import (
    DEFAULT_ESCALATION,
    apply_escalation,
    case_embed,
    get_escalation_rules,
    get_warning_count,
    log_action,
    timeout_member,
)
from utils.paginator import paginate

log = logging.getLogger("cogs.moderation")

MAX_TIMEOUT_SECONDS = 28 * 24 * 3600  # Discord rejects timeouts longer than 28 days
WARNINGS_PER_PAGE = 6
CASES_PER_PAGE = 8


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _hierarchy_error(moderator: discord.Member, target: discord.Member) -> Optional[str]:
    """Return a reason why ``moderator`` may not act on ``target``, else None."""
    guild = target.guild
    if target.id == moderator.id:
        return "You cannot target yourself."
    if target.id == guild.owner_id:
        return "You cannot target the server owner."
    if target.id == guild.me.id:
        return "You cannot target me."
    if target.top_role >= guild.me.top_role:
        return "That member's highest role is equal to or higher than mine."
    if (
        target.top_role >= moderator.top_role
        and not moderator.guild_permissions.administrator
        and moderator.id != guild.owner_id
    ):
        return "That member's highest role is equal to or higher than yours."
    return None


async def _fetch_user(bot: commands.Bot, user_id: int) -> Optional[discord.User]:
    """Resolve a user by id from the cache or the API."""
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def _resolve_channel(
    interaction: discord.Interaction, channel: Optional[discord.TextChannel]
) -> Optional[discord.TextChannel]:
    """Return the given channel or the current one when it is a text channel."""
    target = channel or interaction.channel
    if isinstance(target, discord.TextChannel):
        return target
    return None


# ---------------------------------------------------------------------------
# /setup wizard step
# ---------------------------------------------------------------------------
async def _apply_moderation_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save moderation settings from wizard values."""
    lines: list[str] = []
    role_ids = [
        rid
        for part in values.get("roles", "").replace(",", " ").split()
        if (rid := parse_id(part))
    ]
    if role_ids:
        await db.set(guild.id, "mod_role_ids", role_ids)
        lines.append("Staff roles: " + ", ".join(f"<@&{rid}>" for rid in role_ids))
    log_id = parse_id(values.get("mod_log", ""))
    if log_id:
        await db.set(guild.id, "mod_log_channel", log_id)
        lines.append(f"Mod log channel: <#{log_id}>")
    dm_raw = values.get("dm", "").strip().lower()
    if dm_raw:
        enabled = dm_raw in {"yes", "y", "true", "1", "on", "enabled"}
        await db.set(guild.id, "dm_punishments", enabled)
        lines.append(f"DM punishments: {'enabled' if enabled else 'disabled'}")
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="moderation",
        label="Moderation",
        emoji="🛡️",
        description="Staff roles, mod-log channel and punishment DMs.",
        fields=[
            ("Staff roles", "@Moderator @Helper (comma separated)", False, "roles"),
            ("Mod log channel", "#mod-logs (optional)", False, "mod_log"),
            ("DM punishments (yes/no)", "yes", False, "dm"),
        ],
        handler=_apply_moderation_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Moderation(commands.Cog):
    """Moderation commands, case tracking and warning escalation."""

    modrole_group = app_commands.Group(
        name="modrole",
        description="Roles that may use moderation commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    modlog_group = app_commands.Group(
        name="modlog",
        description="Configure the moderation log channel.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    escalation_group = app_commands.Group(
        name="escalation",
        description="Automatic warning escalation rules.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user_id -> unix time until which on_member_unban is ignored
        self._unban_suppress: dict[int, float] = {}

    def cog_unload(self) -> None:
        self.tempban_loop.cancel()

    # -- helpers ------------------------------------------------------------
    def _suppress_unban(self, user_id: int, seconds: float = 30.0) -> None:
        """Ignore the next ``on_member_unban`` for this user (bot-initiated)."""
        self._unban_suppress[user_id] = time.time() + seconds

    # -- bans ----------------------------------------------------------------
    @app_commands.command(description="Ban a member from the server.")
    @app_commands.describe(
        member="The member to ban",
        reason="Why they are being banned",
        delete_messages="Delete this many days of their recent messages (0-7)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
        delete_messages: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        """Ban a member after hierarchy checks."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.ban(reason=reason, delete_message_seconds=int(delete_messages) * 86400)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Ban failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not ban that member. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="ban",
            reason=reason,
            extra=[("Deleted messages", f"{delete_messages} day(s)", True)],
        )
        await interaction.followup.send(
            embed=success_embed(f"**{member}** was banned. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Unban a user by id.")
    @app_commands.describe(user_id="The user id or mention to unban", reason="Why they are being unbanned")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(
        self, interaction: discord.Interaction, user_id: str, reason: Optional[str] = None
    ) -> None:
        """Lift a ban and record an unban case."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        uid = parse_id(user_id)
        if uid is None:
            await interaction.response.send_message(
                embed=error_embed("That is not a valid user id or mention."), ephemeral=True
            )
            return
        try:
            await guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            await interaction.response.send_message(
                embed=error_embed("That user is not banned."), ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("Could not check ban for %s in guild %s", uid, guild.id)
            await interaction.response.send_message(
                embed=error_embed("I could not check the ban list. Check my permissions."), ephemeral=True
            )
            return
        user = await _fetch_user(self.bot, uid)
        if user is None:
            await interaction.response.send_message(
                embed=error_embed("I could not resolve that user."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        case_id = await log_action(
            guild, user=user, moderator=interaction.user, action="unban", reason=reason
        )
        self._suppress_unban(user.id)
        try:
            await guild.unban(user, reason=reason or "Unbanned by moderator")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Unban failed for %s in guild %s", user.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not unban that user. Check my permissions."), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=success_embed(f"**{user}** was unbanned. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Kick a member from the server.")
    @app_commands.describe(member="The member to kick", reason="Why they are being kicked")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(
        self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None
    ) -> None:
        """Kick a member after hierarchy checks."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.kick(reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Kick failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not kick that member. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild, user=member, moderator=interaction.user, action="kick", reason=reason
        )
        await interaction.followup.send(
            embed=success_embed(f"**{member}** was kicked. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Temporarily ban a member.")
    @app_commands.describe(
        member="The member to ban",
        duration="How long the ban lasts, e.g. 1d, 12h, 30m",
        reason="Why they are being banned",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        """Ban a member until ``duration`` elapses (lifted by a background task)."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        seconds = parse_duration(duration)
        if not seconds or seconds < 60:
            await interaction.response.send_message(
                embed=error_embed("Give a valid duration of at least 1 minute, e.g. `1h`, `2d`."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        expires_at = int(time.time()) + seconds
        try:
            await member.ban(reason=f"Tempban ({format_duration(seconds)}): {reason or 'No reason'}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Tempban failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not ban that member. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="tempban",
            reason=reason,
            duration=format_duration(seconds),
            expires_at=expires_at,
        )
        await interaction.followup.send(
            embed=success_embed(
                f"**{member}** was temporarily banned for **{format_duration(seconds)}**.\n"
                f"Case **#{case_id}** • expires {format_ts(expires_at, 'R')}."
            ),
            ephemeral=True,
        )

    @app_commands.command(description="Ban then immediately unban a member (softban).")
    @app_commands.describe(
        member="The member to softban",
        reason="Why they are being softbanned",
        delete_messages="Delete this many days of their recent messages (1-7)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def softban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
        delete_messages: app_commands.Range[int, 1, 7] = 1,
    ) -> None:
        """Purge a member's recent messages and immediately unban them."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.ban(
                reason=reason, delete_message_seconds=int(delete_messages) * 86400
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Softban (ban) failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not ban that member. Check my permissions."), ephemeral=True
            )
            return
        self._suppress_unban(member.id)
        try:
            await guild.unban(member, reason="Softban")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Softban (unban) failed for %s in guild %s", member.id, guild.id)
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="softban",
            reason=reason,
            extra=[("Deleted messages", f"{delete_messages} day(s)", True)],
        )
        await interaction.followup.send(
            embed=success_embed(f"**{member}** was softbanned. Case **#{case_id}**."), ephemeral=True
        )

    # -- timeouts ------------------------------------------------------------
    @app_commands.command(description="Timeout (mute) a member for a duration.")
    @app_commands.describe(
        member="The member to timeout",
        duration="How long, e.g. 10m, 2h, 3d (max 28 days)",
        reason="Why they are being timed out",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        """Time a member out, storing the expiry in the case."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        seconds = parse_duration(duration)
        if not seconds or seconds < 1:
            await interaction.response.send_message(
                embed=error_embed("Give a valid duration, e.g. `10m`, `2h`, `3d`."), ephemeral=True
            )
            return
        if seconds > MAX_TIMEOUT_SECONDS:
            await interaction.response.send_message(
                embed=error_embed("Timeouts cannot last longer than 28 days."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await timeout_member(member, seconds, reason or "No reason provided")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Timeout failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not time that member out. Check my permissions."), ephemeral=True
            )
            return
        expires_at = int(time.time()) + seconds
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="timeout",
            reason=reason,
            duration=format_duration(seconds),
            expires_at=expires_at,
        )
        await interaction.followup.send(
            embed=success_embed(
                f"**{member}** was timed out for **{format_duration(seconds)}**.\n"
                f"Case **#{case_id}** • ends {format_ts(expires_at, 'R')}."
            ),
            ephemeral=True,
        )

    @app_commands.command(description="Remove a member's timeout.")
    @app_commands.describe(member="The member to clear the timeout for", reason="Why the timeout is removed")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None
    ) -> None:
        """Clear a member's timeout."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await member.timeout(None, reason=reason or "Timeout removed")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Untimeout failed for %s in guild %s", member.id, guild.id)
            await interaction.followup.send(
                embed=error_embed("I could not remove that timeout. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild, user=member, moderator=interaction.user, action="untimeout", reason=reason
        )
        await interaction.followup.send(
            embed=success_embed(f"Timeout removed for **{member}**. Case **#{case_id}**."), ephemeral=True
        )

    # -- warnings ------------------------------------------------------------
    @app_commands.command(description="Warn a member and apply warning escalation.")
    @app_commands.describe(member="The member to warn", reason="Why they are being warned")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        """Store a warning, log a case and run the escalation rules."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await db.execute(
            "INSERT INTO warnings (guild_id, user_id, mod_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild.id, member.id, interaction.user.id, truncate(reason, 500), int(time.time())),
        )
        case_id = await log_action(
            guild, user=member, moderator=interaction.user, action="warn", reason=reason
        )
        count = await get_warning_count(guild.id, member.id)
        escalation = await apply_escalation(guild, member, self.bot.user)  # type: ignore[arg-type]
        text = f"{member.mention} now has **{count}** warning(s). Case **#{case_id}**."
        if escalation:
            text += f"\nAutomatic escalation: **{escalation}**."
            embed = warn_embed(text, title="Member warned")
        else:
            embed = success_embed(text, title="Member warned")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(description="List a member's warnings.")
    @app_commands.describe(member="Whose warnings to show (defaults to you)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        """Paginated list of a member's warnings."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        target = member or interaction.user
        rows = await db.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild.id, target.id),
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(f"**{target}** has no warnings."), ephemeral=True
            )
            return
        total = len(rows)
        pages: list[discord.Embed] = []
        for start in range(0, total, WARNINGS_PER_PAGE):
            embed = base_embed(
                title=f"⚠️ Warnings — {target}",
                description=f"Total warnings: **{total}**",
                color=discord.Color.from_str("#FEE75C"),
            )
            for row in rows[start : start + WARNINGS_PER_PAGE]:
                embed.add_field(
                    name=f"Warning #{row['id']}",
                    value=(
                        f"**Moderator:** <@{row['mod_id']}>\n"
                        f"**Reason:** {truncate(row['reason'], 300)}\n"
                        f"**Date:** {format_ts(row['created_at'])}"
                    ),
                    inline=False,
                )
            embed.set_footer(text=f"Page {start // WARNINGS_PER_PAGE + 1}/{(total + WARNINGS_PER_PAGE - 1) // WARNINGS_PER_PAGE}")
            pages.append(embed)
        await paginate(interaction, pages)

    @app_commands.command(description="Clear every warning for a member.")
    @app_commands.describe(member="The member whose warnings are cleared", reason="Why they are being cleared")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clearwarnings(
        self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None
    ) -> None:
        """Delete all warnings for a member and log a case."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        count = await get_warning_count(guild.id, member.id)
        if count == 0:
            await interaction.response.send_message(
                embed=info_embed(f"**{member}** has no warnings to clear."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await db.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
        )
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="clearwarnings",
            reason=reason,
            extra=[("Warnings cleared", str(count), True)],
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Cleared **{count}** warning(s) for {member.mention}. Case **#{case_id}**."
            ),
            ephemeral=True,
        )

    # -- purge / channels ----------------------------------------------------
    @app_commands.command(description="Bulk delete recent messages in this channel.")
    @app_commands.describe(
        amount="How many messages to delete (1-1000)",
        member="Only delete messages from this member",
        reason="Why the messages are being purged",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        member: Optional[discord.Member] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Delete messages, skipping anything older than 14 days for large purges."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("This command only works in text channels."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        deleted = 0
        cutoff = discord.utils.utcnow() - timedelta(days=14)
        try:
            if int(amount) > 100:
                # Bulk delete is limited to 14 days: fall back to a per-message loop.
                async for message in channel.history(limit=int(amount)):
                    if member is not None and message.author.id != member.id:
                        continue
                    if message.created_at < cutoff:
                        continue
                    try:
                        await message.delete()
                        deleted += 1
                    except discord.HTTPException:
                        continue
            else:
                check = (lambda m: m.author.id == member.id) if member is not None else None
                purged = await channel.purge(limit=int(amount), check=check, reason=reason)
                deleted = len(purged)
        except discord.HTTPException:
            log.exception("Purge failed in channel %s", channel.id)
            await interaction.followup.send(
                embed=error_embed("Something went wrong while deleting messages."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=member or interaction.user,
            moderator=interaction.user,
            action="purge",
            reason=reason,
            dm=False,
            extra=[
                ("Channel", channel.mention, True),
                ("Deleted", str(deleted), True),
            ],
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{deleted}** message(s) in {channel.mention}. Case **#{case_id}**."),
            ephemeral=True,
        )

    @app_commands.command(description="Set the slowmode delay of a channel.")
    @app_commands.describe(seconds="Slowmode delay in seconds (0-21600)", channel="Channel to change")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Change a channel's slowmode delay."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        target = await _resolve_channel(interaction, channel)
        if target is None:
            await interaction.response.send_message(embed=error_embed("Pick a text channel."), ephemeral=True)
            return
        try:
            await target.edit(slowmode_delay=int(seconds), reason=f"Slowmode set by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Slowmode failed in channel %s", target.id)
            await interaction.response.send_message(
                embed=error_embed("I could not edit that channel. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=interaction.user,
            moderator=interaction.user,
            action="slowmode",
            reason=f"Slowmode set to {seconds}s in #{target.name}",
            dm=False,
            extra=[("Channel", target.mention, True)],
        )
        text = f"Slowmode for {target.mention} set to **{seconds}** seconds."
        if seconds == 0:
            text = f"Slowmode disabled in {target.mention}."
        await interaction.response.send_message(
            embed=success_embed(f"{text} Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Lock a channel so @everyone cannot send messages.")
    @app_commands.describe(channel="Channel to lock", reason="Why the channel is being locked")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Deny send_messages for @everyone in a channel."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        target = await _resolve_channel(interaction, channel)
        if target is None:
            await interaction.response.send_message(embed=error_embed("Pick a text channel."), ephemeral=True)
            return
        overwrite = target.overwrites_for(guild.default_role)
        overwrite.send_messages = False
        try:
            await target.set_permissions(
                guild.default_role, overwrite=overwrite, reason=reason or "Channel locked"
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Lock failed in channel %s", target.id)
            await interaction.response.send_message(
                embed=error_embed("I could not lock that channel. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=interaction.user,
            moderator=interaction.user,
            action="lock",
            reason=reason,
            dm=False,
            extra=[("Channel", target.mention, True)],
        )
        await interaction.response.send_message(
            embed=success_embed(f"🔒 {target.mention} was locked. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Unlock a previously locked channel.")
    @app_commands.describe(channel="Channel to unlock", reason="Why the channel is being unlocked")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Clear the send_messages deny for @everyone in a channel."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        target = await _resolve_channel(interaction, channel)
        if target is None:
            await interaction.response.send_message(embed=error_embed("Pick a text channel."), ephemeral=True)
            return
        overwrite = target.overwrites_for(guild.default_role)
        overwrite.send_messages = None
        try:
            await target.set_permissions(
                guild.default_role, overwrite=overwrite, reason=reason or "Channel unlocked"
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Unlock failed in channel %s", target.id)
            await interaction.response.send_message(
                embed=error_embed("I could not unlock that channel. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=interaction.user,
            moderator=interaction.user,
            action="unlock",
            reason=reason,
            dm=False,
            extra=[("Channel", target.mention, True)],
        )
        await interaction.response.send_message(
            embed=success_embed(f"🔓 {target.mention} was unlocked. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Change or clear a member's nickname.")
    @app_commands.describe(member="The member to rename", nickname="New nickname (omit to reset)")
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    async def nickname(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: Optional[str] = None,
    ) -> None:
        """Set or clear a member's nickname after hierarchy checks."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        problem = _hierarchy_error(interaction.user, member)
        if problem:
            await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
            return
        previous = member.nick
        try:
            await member.edit(nick=nickname or None, reason=f"Nickname changed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Nickname failed for %s in guild %s", member.id, guild.id)
            await interaction.response.send_message(
                embed=error_embed("I could not change that nickname. Check my permissions."), ephemeral=True
            )
            return
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="nickname",
            reason=f"Nickname: {previous or '—'} → {nickname or '—'}",
            dm=False,
        )
        text = f"Nickname cleared for {member.mention}." if not nickname else f"Nickname set to **{nickname}**."
        await interaction.response.send_message(
            embed=success_embed(f"{text} Case **#{case_id}**."), ephemeral=True
        )

    # -- notes / case history -------------------------------------------------
    @app_commands.command(description="Add a private note to a member's record.")
    @app_commands.describe(member="The member the note is about", text="The note contents")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def note(self, interaction: discord.Interaction, member: discord.Member, text: str) -> None:
        """Store a moderator note without warning or DMing the member."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True)
        case_id = await log_action(
            guild,
            user=member,
            moderator=interaction.user,
            action="note",
            reason=text,
            dm=False,
        )
        await interaction.followup.send(
            embed=success_embed(f"Note added to **{member}**. Case **#{case_id}**."), ephemeral=True
        )

    @app_commands.command(description="Show a single moderation case.")
    @app_commands.describe(case_id="The case number to show")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction, case_id: int) -> None:
        """Display one case with its user, moderator, action and dates."""
        guild = interaction.guild
        assert guild is not None
        row = await db.fetchone(
            "SELECT * FROM cases WHERE guild_id = ? AND case_id = ?", (guild.id, case_id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"Case **#{case_id}** does not exist."), ephemeral=True
            )
            return
        member = guild.get_member(int(row["user_id"]))
        user: discord.abc.User = member or await _fetch_user(self.bot, int(row["user_id"])) or self.bot.user  # type: ignore[assignment]
        moderator = guild.get_member(int(row["mod_id"]))
        mod: discord.abc.User = moderator or await _fetch_user(self.bot, int(row["mod_id"])) or self.bot.user  # type: ignore[assignment]
        extra = [
            ("Created", format_ts(row["created_at"]), True),
            ("Expires", format_ts(row["expires_at"]) if row["expires_at"] else "Never", True),
        ]
        embed = case_embed(
            int(row["case_id"]),
            user=user,
            moderator=mod,
            action=row["action"],
            reason=row["reason"],
            duration=row["duration"],
            extra=extra,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Show the moderation history of a member or the server.")
    @app_commands.describe(member="Only show cases for this member (optional)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def modlogs(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        """Paginated case history, newest first."""
        guild = interaction.guild
        assert guild is not None
        sql = "SELECT * FROM cases WHERE guild_id = ?"
        params: list[int] = [guild.id]
        if member is not None:
            sql += " AND user_id = ?"
            params.append(member.id)
        sql += " ORDER BY case_id DESC"
        rows = await db.fetchall(sql, params)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No cases found."), ephemeral=True
            )
            return
        total = len(rows)
        pages: list[discord.Embed] = []
        for start in range(0, total, CASES_PER_PAGE):
            title = f"🛡️ Moderation history — {member}" if member else "🛡️ Moderation history"
            embed = base_embed(title=title, description=f"Total cases: **{total}**")
            for row in rows[start : start + CASES_PER_PAGE]:
                created = format_ts(row["created_at"])
                value = (
                    f"**User:** <@{row['user_id']}>\n"
                    f"**Moderator:** <@{row['mod_id']}>\n"
                    f"**Reason:** {truncate(row['reason'] or 'No reason provided', 200)}\n"
                    f"**Date:** {created}"
                )
                if row["duration"]:
                    value += f"\n**Duration:** {row['duration']}"
                embed.add_field(
                    name=f"#{row['case_id']} • {str(row['action']).title()}", value=value, inline=False
                )
            embed.set_footer(text=f"Page {start // CASES_PER_PAGE + 1}/{(total + CASES_PER_PAGE - 1) // CASES_PER_PAGE}")
            pages.append(embed)
        await paginate(interaction, pages)

    # -- settings: staff roles ----------------------------------------------
    @modrole_group.command(name="add", description="Allow a role to use moderation commands.")
    @app_commands.describe(role="The role to add as a staff role")
    async def modrole_add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Add a role to the staff list."""
        guild = interaction.guild
        assert guild is not None
        current: list[int] = [int(x) for x in await db.get(guild.id, "mod_role_ids", [])]
        if role.id in current:
            await interaction.response.send_message(
                embed=info_embed(f"{role.mention} is already a staff role."), ephemeral=True
            )
            return
        current.append(role.id)
        await db.set(guild.id, "mod_role_ids", current)
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} can now use moderation commands."), ephemeral=True
        )

    @modrole_group.command(name="remove", description="Remove a role from the moderation staff list.")
    @app_commands.describe(role="The role to remove")
    async def modrole_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Remove a role from the staff list."""
        guild = interaction.guild
        assert guild is not None
        current: list[int] = [int(x) for x in await db.get(guild.id, "mod_role_ids", [])]
        if role.id not in current:
            await interaction.response.send_message(
                embed=info_embed(f"{role.mention} is not a staff role."), ephemeral=True
            )
            return
        current = [rid for rid in current if rid != role.id]
        await db.set(guild.id, "mod_role_ids", current)
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} can no longer use moderation commands."), ephemeral=True
        )

    @modrole_group.command(name="list", description="List the roles that may use moderation commands.")
    async def modrole_list(self, interaction: discord.Interaction) -> None:
        """Show the configured staff roles."""
        guild = interaction.guild
        assert guild is not None
        ids: list[int] = [int(x) for x in await db.get(guild.id, "mod_role_ids", [])]
        roles = [guild.get_role(rid) for rid in ids]
        names = [role.mention for role in roles if role is not None]
        await interaction.response.send_message(
            embed=info_embed(
                "\n".join(names) if names else "No staff roles configured.",
                title="Moderation staff roles",
            ),
            ephemeral=True,
        )

    # -- settings: mod-log & DMs --------------------------------------------
    @modlog_group.command(name="set", description="Set the channel where moderation cases are logged.")
    @app_commands.describe(channel="The channel to log moderation cases in")
    async def modlog_set(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Store the mod-log channel."""
        guild = interaction.guild
        assert guild is not None
        await db.set(guild.id, "mod_log_channel", channel.id)
        await interaction.response.send_message(
            embed=success_embed(f"Moderation cases will be logged in {channel.mention}."), ephemeral=True
        )

    @modlog_group.command(name="clear", description="Disable the moderation log channel.")
    async def modlog_clear(self, interaction: discord.Interaction) -> None:
        """Remove the configured mod-log channel."""
        guild = interaction.guild
        assert guild is not None
        await db.delete(guild.id, "mod_log_channel")
        await interaction.response.send_message(
            embed=success_embed("The moderation log channel has been cleared."), ephemeral=True
        )

    @app_commands.command(description="Enable or disable punishment DMs to members.")
    @app_commands.describe(enabled="Whether members receive a DM when punished")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def dm_punishments(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle direct messages for punishments."""
        guild = interaction.guild
        assert guild is not None
        await db.set(guild.id, "dm_punishments", enabled)
        await interaction.response.send_message(
            embed=success_embed(
                f"Punishment DMs are now **{'enabled' if enabled else 'disabled'}**."
            ),
            ephemeral=True,
        )

    # -- settings: escalation ------------------------------------------------
    @escalation_group.command(name="view", description="Show the current warning escalation rules.")
    async def escalation_view(self, interaction: discord.Interaction) -> None:
        """Display every escalation rule."""
        guild = interaction.guild
        assert guild is not None
        rules = await get_escalation_rules(guild.id)
        lines = []
        for rule in sorted(rules, key=lambda r: r["warnings"]):
            detail = f"{rule['warnings']} warnings → **{rule['action']}**"
            if rule["action"] == "timeout" and rule["duration"]:
                detail += f" for {format_duration(rule['duration'])}"
            lines.append(detail)
        await interaction.response.send_message(
            embed=info_embed("\n".join(lines) or "No rules configured.", title="Warning escalation"),
            ephemeral=True,
        )

    @escalation_group.command(name="set", description="Add or replace a warning escalation rule.")
    @app_commands.describe(
        warnings="Warning count that triggers the action",
        action="What happens at that warning count",
        duration="Timeout duration (e.g. 1h, 30m); ignored for kick/ban",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Warn", value="warn"),
            app_commands.Choice(name="Timeout", value="timeout"),
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Ban", value="ban"),
        ]
    )
    async def escalation_set(
        self,
        interaction: discord.Interaction,
        warnings: app_commands.Range[int, 1, 100],
        action: app_commands.Choice[str],
        duration: Optional[str] = None,
    ) -> None:
        """Store an escalation rule keyed by warning count."""
        guild = interaction.guild
        assert guild is not None
        seconds = 0
        if action.value == "timeout":
            seconds = parse_duration(duration or "") or 3600
        rules = await get_escalation_rules(guild.id)
        updated = [
            {"warnings": int(warnings), "action": action.value, "duration": seconds}
        ] + [rule for rule in rules if rule["warnings"] != int(warnings)]
        updated.sort(key=lambda r: r["warnings"])
        await db.set(guild.id, "escalation_rules", updated)
        detail = f"**{warnings}** warnings → **{action.name}**"
        if action.value == "timeout":
            detail += f" for {format_duration(seconds)}"
        await interaction.response.send_message(
            embed=success_embed(f"Escalation rule saved: {detail}."), ephemeral=True
        )

    @escalation_group.command(name="reset", description="Restore the default warning escalation rules.")
    async def escalation_reset(self, interaction: discord.Interaction) -> None:
        """Reset escalation to the built-in defaults."""
        guild = interaction.guild
        assert guild is not None
        await db.set(guild.id, "escalation_rules", DEFAULT_ESCALATION)
        await interaction.response.send_message(
            embed=success_embed("Escalation rules restored to the defaults."), ephemeral=True
        )

    # -- listeners & background task ----------------------------------------
    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        """Record unbans that were not performed through the bot's own commands."""
        try:
            if self._unban_suppress.get(user.id, 0) > time.time():
                return
            moderator = self.bot.user
            if moderator is None:
                return
            await log_action(
                guild,
                user=user,
                moderator=moderator,
                action="unban",
                reason="Unbanned outside the bot",
                dm=False,
            )
        except Exception:
            log.exception("on_member_unban failed for %s in guild %s", user.id, guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the temporary-ban expiry task after a restart."""
        if not self.tempban_loop.is_running():
            self.tempban_loop.start()

    @tasks.loop(minutes=1)
    async def tempban_loop(self) -> None:
        """Lift temporary bans whose expiry has passed."""
        now = int(time.time())
        for user_id, deadline in list(self._unban_suppress.items()):
            if deadline <= now:
                self._unban_suppress.pop(user_id, None)
        for guild in list(self.bot.guilds):
            try:
                rows = await db.fetchall(
                    "SELECT * FROM cases WHERE guild_id = ? AND action = 'tempban' "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (guild.id, now),
                )
            except Exception:
                log.exception("Tempban query failed for guild %s", guild.id)
                continue
            for row in rows:
                try:
                    await self._expire_tempban(guild, row)
                except Exception:
                    log.exception("Tempban expiry failed for case %s", row["case_id"])

    async def _expire_tempban(self, guild: discord.Guild, row) -> None:
        """Unban one expired temporary ban and log an ``unban`` case."""
        user_id = int(row["user_id"])
        existing = await db.fetchone(
            "SELECT 1 FROM cases WHERE guild_id = ? AND user_id = ? AND action = 'unban' "
            "AND created_at >= ? LIMIT 1",
            (guild.id, user_id, int(row["created_at"])),
        )
        if existing:
            return
        user = await _fetch_user(self.bot, user_id)
        if user is None:
            return
        banned = True
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            banned = False
        except discord.HTTPException:
            log.warning("Could not check tempban status for %s in guild %s", user_id, guild.id)
            return
        moderator = self.bot.user or user
        await log_action(
            guild,
            user=user,
            moderator=moderator,
            action="unban",
            reason="Temporary ban expired",
            dm=False,
        )
        if banned:
            self._suppress_unban(user.id)
            try:
                await guild.unban(user, reason="Temporary ban expired")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not lift expired tempban for %s in guild %s", user_id, guild.id)

    @tempban_loop.before_loop
    async def before_tempban_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the moderation cog."""
    await bot.add_cog(Moderation(bot))
