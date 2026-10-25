"""Event logging: individually toggleable, per-event channels for edits,
deletions, bulk deletes, member joins/leaves/updates, bans, channels, roles,
emojis, voice, invites and server updates.

The ``moderation`` event deliberately shares the ``mod_log_channel`` setting
with :mod:`utils.moderation` so moderation cases and this cog stay in sync.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.core import SetupCategory, register_setup_category
from database import db
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import format_ts, parse_id, truncate

log = logging.getLogger("cogs.logs")

# Event key -> human readable label (also used to build the slash-command choices)
EVENT_LABELS: dict[str, str] = {
    "message_edit": "Message edits",
    "message_delete": "Message deletes",
    "bulk_delete": "Bulk message deletes",
    "member_join": "Member joins",
    "member_leave": "Member leaves",
    "member_update": "Member updates",
    "ban": "Bans",
    "unban": "Unbans",
    "channel": "Channel changes",
    "role": "Role changes",
    "emoji": "Emoji changes",
    "voice": "Voice states",
    "invite": "Invites",
    "server": "Server updates",
    "moderation": "Moderation cases",
}
EVENT_KEYS: list[str] = list(EVENT_LABELS)
# Every event except ``moderation`` lives in the ``log_channels`` mapping.
DIRECT_EVENTS: list[str] = [key for key in EVENT_KEYS if key != "moderation"]
EVENT_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name=label, value=key) for key, label in EVENT_LABELS.items()
]
IGNORE_ACTIONS = ("add_channel", "remove_channel", "add_user", "remove_user", "list")


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
async def _log_channels(guild: discord.Guild) -> dict[str, int]:
    """Return the event -> channel mapping, tolerating bad stored data."""
    raw = await db.get(guild.id, "log_channels", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


async def _event_channel(guild: discord.Guild, event: str) -> Optional[discord.TextChannel]:
    """Resolve the configured channel for one event, if any."""
    if event == "moderation":
        channel_id = await db.get(guild.id, "mod_log_channel")
    else:
        channel_id = (await _log_channels(guild)).get(event)
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


async def _emit(
    guild: discord.Guild,
    event: str,
    embed: discord.Embed,
    *,
    channel: Optional[discord.abc.GuildChannel] = None,
    user: Optional[discord.abc.User] = None,
) -> None:
    """Deliver an event embed, honouring ignore lists, toggles and permissions."""
    if event in await db.get(guild.id, "log_disabled", []):
        return
    if channel is not None and channel.id in await db.get(guild.id, "log_ignore_channels", []):
        return
    if user is not None and user.id in await db.get(guild.id, "log_ignore_users", []):
        return
    target = await _event_channel(guild, event)
    if target is None:
        return
    me = guild.me
    if me is None:
        return
    permissions = target.permissions_for(me)
    if not (permissions.view_channel and permissions.send_messages):
        return
    try:
        await target.send(embed=embed)
    except discord.HTTPException:
        log.warning("Could not send %s log in guild %s", event, guild.id)


# ---------------------------------------------------------------------------
# /setup wizard step
# ---------------------------------------------------------------------------
async def _apply_logs_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save logging settings from wizard values."""
    lines: list[str] = []
    channel_id = parse_id(values.get("log_channel", ""))
    if channel_id:
        channels = {event: channel_id for event in DIRECT_EVENTS}
        await db.set(guild.id, "log_channels", channels)
        lines.append(f"Log channel (all events): <#{channel_id}>")
    mod_id = parse_id(values.get("mod_log", ""))
    if mod_id:
        await db.set(guild.id, "mod_log_channel", mod_id)
        lines.append(f"Moderation log channel: <#{mod_id}>")
    ignore_channels = [
        cid
        for part in values.get("ignore_channels", "").replace(",", " ").split()
        if (cid := parse_id(part))
    ]
    if ignore_channels:
        await db.set(guild.id, "log_ignore_channels", ignore_channels)
        lines.append("Ignored channels: " + ", ".join(f"<#{cid}>" for cid in ignore_channels))
    ignore_users = [
        uid
        for part in values.get("ignore_users", "").replace(",", " ").split()
        if (uid := parse_id(part))
    ]
    if ignore_users:
        await db.set(guild.id, "log_ignore_users", ignore_users)
        lines.append("Ignored users: " + ", ".join(f"<@{uid}>" for uid in ignore_users))
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="logs",
        label="Logging",
        emoji="📜",
        description="Event log channels, moderation log and ignore lists.",
        fields=[
            ("Log channel (all events)", "#logs (optional)", False, "log_channel"),
            ("Moderation log channel", "#mod-logs (optional)", False, "mod_log"),
            ("Ignore channels", "#spam #memes (optional)", False, "ignore_channels"),
            ("Ignore users", "@user or IDs (optional)", False, "ignore_users"),
        ],
        handler=_apply_logs_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Logs(commands.Cog):
    """Server event logging with per-event channels and toggles."""

    logs_group = app_commands.Group(
        name="logs",
        description="Configure and inspect server event logging.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- commands -----------------------------------------------------------
    @logs_group.command(name="set", description="Set the log channel for one event.")
    @app_commands.describe(event="The event to configure", channel="The channel to log it in")
    @app_commands.choices(event=EVENT_CHOICES)
    async def logs_set(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        """Store the channel for one event key."""
        guild = interaction.guild
        assert guild is not None
        if event.value == "moderation":
            await db.set(guild.id, "mod_log_channel", channel.id)
        else:
            channels = await _log_channels(guild)
            channels[event.value] = channel.id
            await db.set(guild.id, "log_channels", channels)
        await interaction.response.send_message(
            embed=success_embed(f"**{event.name}** will be logged in {channel.mention}."), ephemeral=True
        )

    @logs_group.command(name="all", description="Send every event to a single channel.")
    @app_commands.describe(channel="The channel that will receive every event")
    async def logs_all(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Point every event channel at one channel."""
        guild = interaction.guild
        assert guild is not None
        channels = {event: channel.id for event in DIRECT_EVENTS}
        await db.set(guild.id, "log_channels", channels)
        await db.set(guild.id, "mod_log_channel", channel.id)
        await interaction.response.send_message(
            embed=success_embed(f"All logging events now go to {channel.mention}."), ephemeral=True
        )

    @logs_group.command(name="toggle", description="Enable or disable one logging event.")
    @app_commands.describe(event="The event to toggle", enabled="Whether the event is logged")
    @app_commands.choices(event=EVENT_CHOICES)
    async def logs_toggle(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        """Add or remove an event from the disabled list."""
        guild = interaction.guild
        assert guild is not None
        disabled = await db.get(guild.id, "log_disabled", [])
        if not isinstance(disabled, list):
            disabled = []
        disabled = [str(x) for x in disabled]
        if enabled:
            disabled = [x for x in disabled if x != event.value]
        elif event.value not in disabled:
            disabled.append(event.value)
        await db.set(guild.id, "log_disabled", disabled)
        await interaction.response.send_message(
            embed=success_embed(
                f"**{event.name}** logging is now **{'enabled' if enabled else 'disabled'}**."
            ),
            ephemeral=True,
        )

    @logs_group.command(name="ignore", description="Manage ignored channels and users for logging.")
    @app_commands.describe(action="What to do", target="The channel or user id/mention")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add channel", value="add_channel"),
            app_commands.Choice(name="Remove channel", value="remove_channel"),
            app_commands.Choice(name="Add user", value="add_user"),
            app_commands.Choice(name="Remove user", value="remove_user"),
            app_commands.Choice(name="Show lists", value="list"),
        ]
    )
    async def logs_ignore(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        target: Optional[str] = None,
    ) -> None:
        """Add/remove ignored channels and users, or list them."""
        guild = interaction.guild
        assert guild is not None
        if action.value == "list":
            channels = [int(x) for x in await db.get(guild.id, "log_ignore_channels", [])]
            users = [int(x) for x in await db.get(guild.id, "log_ignore_users", [])]
            await interaction.response.send_message(
                embed=info_embed(
                    "**Ignored channels:** "
                    + (", ".join(f"<#{cid}>" for cid in channels) or "None")
                    + "\n**Ignored users:** "
                    + (", ".join(f"<@{uid}>" for uid in users) or "None"),
                    title="Logging ignore lists",
                ),
                ephemeral=True,
            )
            return
        target_id = parse_id(target)
        if target_id is None:
            await interaction.response.send_message(
                embed=error_embed("Provide a channel or user id/mention."), ephemeral=True
            )
            return
        if action.value in {"add_channel", "remove_channel"}:
            if not isinstance(guild.get_channel(target_id), discord.TextChannel):
                await interaction.response.send_message(
                    embed=error_embed("That is not a text channel on this server."), ephemeral=True
                )
                return
            key = "log_ignore_channels"
            mention = f"<#{target_id}>"
            verb = "added to" if action.value == "add_channel" else "removed from"
        else:
            key = "log_ignore_users"
            mention = f"<@{target_id}>"
            verb = "added to" if action.value == "add_user" else "removed from"
        current = [int(x) for x in await db.get(guild.id, key, [])]
        if action.value.startswith("add"):
            if target_id not in current:
                current.append(target_id)
        else:
            current = [x for x in current if x != target_id]
        await db.set(guild.id, key, current)
        await interaction.response.send_message(
            embed=success_embed(f"{mention} was {verb} the logging ignore list."), ephemeral=True
        )

    @logs_group.command(name="config", description="Show the current logging configuration.")
    async def logs_config(self, interaction: discord.Interaction) -> None:
        """Display every event, its channel and the ignore lists."""
        guild = interaction.guild
        assert guild is not None
        channels = await _log_channels(guild)
        disabled = [str(x) for x in await db.get(guild.id, "log_disabled", [])]
        mod_id = await db.get(guild.id, "mod_log_channel")
        embed = base_embed(title="📜 Logging configuration")
        for event, label in EVENT_LABELS.items():
            if event in disabled:
                status = "Disabled"
            else:
                cid = mod_id if event == "moderation" else channels.get(event)
                status = f"<#{int(cid)}>" if cid else "Not set"
            embed.add_field(name=label, value=status, inline=True)
        ignore_channels = [int(x) for x in await db.get(guild.id, "log_ignore_channels", [])]
        ignore_users = [int(x) for x in await db.get(guild.id, "log_ignore_users", [])]
        embed.add_field(
            name="Ignored channels",
            value=truncate(", ".join(f"<#{cid}>" for cid in ignore_channels) or "None", 1024),
            inline=False,
        )
        embed.add_field(
            name="Ignored users",
            value=truncate(", ".join(f"<@{uid}>" for uid in ignore_users) or "None", 1024),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- message events ------------------------------------------------------
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Log message content or attachment edits."""
        try:
            if before.guild is None or self.bot.user is None:
                return
            if before.author.id == self.bot.user.id:
                return
            content_changed = before.content != after.content
            attachments_changed = [a.filename for a in before.attachments] != [
                a.filename for a in after.attachments
            ]
            if not content_changed and not attachments_changed:
                return
            embed = base_embed(
                title="✏️ Message edited",
                color=config.WARNING_COLOR,
                author=after.author,
                fields=[
                    ("Channel", before.channel.mention if before.channel else "Unknown", True),
                    ("Jump", f"[Go to message]({after.jump_url})", True),
                    ("Before", truncate(before.content or "*empty*", 1024), False),
                    ("After", truncate(after.content or "*empty*", 1024), False),
                ],
            )
            embed.set_footer(text=f"Message id: {after.id}")
            await _emit(before.guild, "message_edit", embed, channel=before.channel, user=before.author)
        except Exception:
            log.exception("on_message_edit logging failed")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Log a deleted message (content may be missing without cache)."""
        try:
            if message.guild is None or self.bot.user is None:
                return
            if message.author.id == self.bot.user.id:
                return
            attachments = ", ".join(a.filename for a in message.attachments) or "None"
            embed = base_embed(
                title="🗑️ Message deleted",
                color=config.ERROR_COLOR,
                author=message.author,
                fields=[
                    ("Channel", message.channel.mention if message.channel else "Unknown", True),
                    ("Attachments", truncate(attachments, 1024), True),
                    ("Content", truncate(message.content or "*content unavailable*", 1024), False),
                ],
            )
            embed.set_footer(text=f"Author id: {message.author.id} • message id: {message.id}")
            await _emit(message.guild, "message_delete", embed, channel=message.channel, user=message.author)
        except Exception:
            log.exception("on_message_delete logging failed")

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        """Log a bulk deletion with a per-author breakdown."""
        try:
            if not messages or messages[0].guild is None:
                return
            channel = messages[0].channel
            guild = messages[0].guild
            ignored_users = [int(x) for x in await db.get(guild.id, "log_ignore_users", [])]
            counts: dict[int, int] = {}
            for message in messages:
                if message.author.id in ignored_users:
                    continue
                counts[message.author.id] = counts.get(message.author.id, 0) + 1
            top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:5]
            breakdown = "\n".join(f"<@{uid}> — {count}" for uid, count in top) or "None"
            embed = base_embed(
                title="🧹 Bulk message delete",
                color=config.ERROR_COLOR,
                fields=[
                    ("Channel", channel.mention if channel else "Unknown", True),
                    ("Messages", str(len(messages)), True),
                    ("Top authors", truncate(breakdown, 1024), False),
                ],
            )
            await _emit(guild, "bulk_delete", embed, channel=channel)
        except Exception:
            log.exception("on_bulk_message_delete logging failed")

    # -- member events -------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Log a joining member."""
        try:
            age = (discord.utils.utcnow() - member.created_at).days
            embed = base_embed(
                title="📥 Member joined",
                color=config.SUCCESS_COLOR,
                author=member,
                thumbnail=member.display_avatar.url,
                fields=[
                    ("Member", f"{member.mention} (`{member.id}`)", False),
                    ("Account created", f"{format_ts(int(member.created_at.timestamp()))} ({age} days ago)", True),
                    ("Member count", str(member.guild.member_count), True),
                    ("Roles", str(max(0, len(member.roles) - 1)), True),
                ],
            )
            await _emit(member.guild, "member_join", embed, user=member)
        except Exception:
            log.exception("on_member_join logging failed")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Log a leaving member."""
        try:
            age = (discord.utils.utcnow() - member.created_at).days
            joined = (
                format_ts(int(member.joined_at.timestamp())) if member.joined_at else "Unknown"
            )
            embed = base_embed(
                title="📤 Member left",
                color=config.WARNING_COLOR,
                author=member,
                thumbnail=member.display_avatar.url,
                fields=[
                    ("Member", f"{member.mention} (`{member.id}`)", False),
                    ("Joined", joined, True),
                    ("Account age", f"{age} days", True),
                    ("Member count", str(member.guild.member_count), True),
                ],
            )
            await _emit(member.guild, "member_leave", embed, user=member)
        except Exception:
            log.exception("on_member_remove logging failed")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Log nickname and role changes for a member."""
        try:
            added = [role for role in after.roles if role not in before.roles]
            removed = [role for role in before.roles if role not in after.roles]
            if not added and not removed and before.nick == after.nick:
                return
            fields: list[tuple[str, str, bool]] = []
            if before.nick != after.nick:
                fields.append(("Nickname", f"{before.nick or '—'} → {after.nick or '—'}", False))
            if added:
                fields.append(("Roles added", truncate(", ".join(r.mention for r in added), 1024), False))
            if removed:
                fields.append(("Roles removed", truncate(", ".join(r.mention for r in removed), 1024), False))
            embed = base_embed(
                title="👤 Member updated",
                color=config.INFO_COLOR,
                author=after,
                fields=[("Member", f"{after.mention} (`{after.id}`)", False)] + fields,
            )
            await _emit(after.guild, "member_update", embed, user=after)
        except Exception:
            log.exception("on_member_update logging failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Log a ban."""
        try:
            embed = base_embed(
                title="🔨 Member banned",
                color=config.ERROR_COLOR,
                author=user,
                fields=[("User", f"{user.mention} (`{user.id}`)", False)],
            )
            await _emit(guild, "ban", embed, user=user)
        except Exception:
            log.exception("on_member_ban logging failed")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        """Log an unban."""
        try:
            embed = base_embed(
                title="♻️ Member unbanned",
                color=config.SUCCESS_COLOR,
                author=user,
                fields=[("User", f"{user.mention} (`{user.id}`)", False)],
            )
            await _emit(guild, "unban", embed, user=user)
        except Exception:
            log.exception("on_member_unban logging failed")

    # -- channel events ------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """Log a created channel."""
        try:
            embed = base_embed(
                title="📁 Channel created",
                color=config.SUCCESS_COLOR,
                fields=[
                    ("Name", channel.mention, True),
                    ("Type", str(channel.type).title(), True),
                    ("Category", channel.category.name if channel.category else "None", True),
                    ("Position", str(channel.position), True),
                ],
            )
            await _emit(channel.guild, "channel", embed, channel=channel)
        except Exception:
            log.exception("on_guild_channel_create logging failed")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Log a deleted channel."""
        try:
            embed = base_embed(
                title="📁 Channel deleted",
                color=config.ERROR_COLOR,
                fields=[
                    ("Name", f"#{channel.name}", True),
                    ("Type", str(channel.type).title(), True),
                    ("Category", channel.category.name if channel.category else "None", True),
                    ("Position", str(channel.position), True),
                ],
            )
            await _emit(channel.guild, "channel", embed, channel=channel)
        except Exception:
            log.exception("on_guild_channel_delete logging failed")

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        """Log a channel change with a short summary."""
        try:
            changes: list[str] = []
            if before.name != after.name:
                changes.append(f"**Name:** {before.name} → {after.name}")
            if before.category != after.category:
                before_cat = before.category.name if before.category else "None"
                after_cat = after.category.name if after.category else "None"
                changes.append(f"**Category:** {before_cat} → {after_cat}")
            if before.position != after.position:
                changes.append(f"**Position:** {before.position} → {after.position}")
            if before.overwrites != after.overwrites:
                changes.append("**Permission overwrites changed**")
            if not changes:
                return
            embed = base_embed(
                title="📁 Channel updated",
                description=truncate("\n".join(changes), 4000),
                color=config.INFO_COLOR,
                fields=[("Channel", after.mention, True)],
            )
            await _emit(after.guild, "channel", embed, channel=after)
        except Exception:
            log.exception("on_guild_channel_update logging failed")

    # -- role events ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        """Log a created role."""
        try:
            embed = base_embed(
                title="🎭 Role created",
                color=config.SUCCESS_COLOR,
                fields=[
                    ("Role", f"{role.mention} (`{role.id}`)", False),
                    ("Colour", str(role.colour), True),
                    ("Position", str(role.position), True),
                ],
            )
            await _emit(role.guild, "role", embed)
        except Exception:
            log.exception("on_guild_role_create logging failed")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Log a deleted role."""
        try:
            embed = base_embed(
                title="🎭 Role deleted",
                color=config.ERROR_COLOR,
                fields=[
                    ("Role", f"{role.name} (`{role.id}`)", False),
                    ("Colour", str(role.colour), True),
                    ("Position", str(role.position), True),
                ],
            )
            await _emit(role.guild, "role", embed)
        except Exception:
            log.exception("on_guild_role_delete logging failed")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        """Log a role change with a permission diff summary."""
        try:
            changes: list[str] = []
            if before.name != after.name:
                changes.append(f"**Name:** {before.name} → {after.name}")
            if before.colour != after.colour:
                changes.append(f"**Colour:** {before.colour} → {after.colour}")
            if before.hoist != after.hoist:
                changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
            if before.mentionable != after.mentionable:
                changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
            gained = [
                name for name, value in after.permissions if value and not getattr(before.permissions, name)
            ]
            lost = [
                name for name, value in before.permissions if value and not getattr(after.permissions, name)
            ]
            if gained:
                changes.append("**Permissions gained:** " + ", ".join(gained))
            if lost:
                changes.append("**Permissions lost:** " + ", ".join(lost))
            if not changes:
                return
            embed = base_embed(
                title="🎭 Role updated",
                description=truncate("\n".join(changes), 4000),
                color=config.INFO_COLOR,
                fields=[("Role", after.mention, True)],
            )
            await _emit(after.guild, "role", embed)
        except Exception:
            log.exception("on_guild_role_update logging failed")

    # -- emoji / voice / invite / server ------------------------------------
    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: list[discord.Emoji], after: list[discord.Emoji]
    ) -> None:
        """Log added and removed emojis."""
        try:
            before_ids = {emoji.id for emoji in before}
            after_ids = {emoji.id for emoji in after}
            added = [emoji for emoji in after if emoji.id not in before_ids]
            removed = [emoji for emoji in before if emoji.id not in after_ids]
            if not added and not removed:
                return
            fields: list[tuple[str, str, bool]] = []
            if added:
                fields.append(("Added", truncate(", ".join(str(e) for e in added), 1024), False))
            if removed:
                fields.append(("Removed", truncate(", ".join(e.name for e in removed), 1024), False))
            embed = base_embed(title="😀 Emojis updated", color=config.INFO_COLOR, fields=fields)
            await _emit(guild, "emoji", embed)
        except Exception:
            log.exception("on_guild_emojis_update logging failed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Log voice joins, leaves, moves and mute/deafen changes."""
        try:
            if before.channel != after.channel:
                if before.channel is None:
                    title = "🔊 Joined voice"
                elif after.channel is None:
                    title = "🔊 Left voice"
                else:
                    title = "🔊 Moved voice"
                embed = base_embed(
                    title=title,
                    color=config.INFO_COLOR,
                    author=member,
                    fields=[
                        ("Member", f"{member.mention} (`{member.id}`)", False),
                        ("Before", before.channel.mention if before.channel else "—", True),
                        ("After", after.channel.mention if after.channel else "—", True),
                    ],
                )
            else:
                changes: list[str] = []
                if before.self_mute != after.self_mute:
                    changes.append(f"**Self mute:** {before.self_mute} → {after.self_mute}")
                if before.self_deaf != after.self_deaf:
                    changes.append(f"**Self deafen:** {before.self_deaf} → {after.self_deaf}")
                if before.self_stream != after.self_stream:
                    changes.append(f"**Streaming:** {before.self_stream} → {after.self_stream}")
                if before.mute != after.mute:
                    changes.append(f"**Server mute:** {before.mute} → {after.mute}")
                if before.deaf != after.deaf:
                    changes.append(f"**Server deafen:** {before.deaf} → {after.deaf}")
                if not changes:
                    return
                embed = base_embed(
                    title="🔊 Voice state updated",
                    description=truncate("\n".join(changes), 4000),
                    color=config.INFO_COLOR,
                    author=member,
                    fields=[
                        ("Member", f"{member.mention} (`{member.id}`)", False),
                        ("Channel", after.channel.mention if after.channel else "—", True),
                    ],
                )
            await _emit(member.guild, "voice", embed, channel=after.channel, user=member)
        except Exception:
            log.exception("on_voice_state_update logging failed")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        """Log a created invite."""
        try:
            if invite.guild is None:
                return
            embed = base_embed(
                title="🔗 Invite created",
                color=config.SUCCESS_COLOR,
                author=invite.inviter,
                fields=[
                    ("Code", f"`{invite.code}`", True),
                    ("Channel", invite.channel.mention if invite.channel else "Unknown", True),
                    ("Uses", f"{invite.uses or 0}/{invite.max_uses or '∞'}", True),
                    ("Expires", format_ts(int(invite.expires_at.timestamp())) if invite.expires_at else "Never", True),
                ],
            )
            await _emit(invite.guild, "invite", embed, channel=invite.channel, user=invite.inviter)
        except Exception:
            log.exception("on_invite_create logging failed")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        """Log a deleted invite."""
        try:
            if invite.guild is None:
                return
            embed = base_embed(
                title="🔗 Invite deleted",
                color=config.ERROR_COLOR,
                author=invite.inviter,
                fields=[
                    ("Code", f"`{invite.code}`", True),
                    ("Channel", invite.channel.mention if invite.channel else "Unknown", True),
                    ("Uses", f"{invite.uses or 0}/{invite.max_uses or '∞'}", True),
                ],
            )
            await _emit(invite.guild, "invite", embed, channel=invite.channel, user=invite.inviter)
        except Exception:
            log.exception("on_invite_delete logging failed")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        """Log server name, icon and owner changes."""
        try:
            changes: list[str] = []
            if before.name != after.name:
                changes.append(f"**Name:** {before.name} → {after.name}")
            if before.icon != after.icon:
                changes.append("**Icon changed**")
            if before.owner_id != after.owner_id:
                changes.append(f"**Owner:** <@{before.owner_id}> → <@{after.owner_id}>")
            if not changes:
                return
            embed = base_embed(
                title="🏠 Server updated",
                description=truncate("\n".join(changes), 4000),
                color=config.INFO_COLOR,
            )
            await _emit(after, "server", embed)
        except Exception:
            log.exception("on_guild_update logging failed")


async def setup(bot: commands.Bot) -> None:
    """Load the logs cog."""
    await bot.add_cog(Logs(bot))
