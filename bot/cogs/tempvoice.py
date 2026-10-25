"""Temporary voice channels: a join-to-create lobby where each member gets
their own voice channel with owner controls."""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, success_embed
from utils.helpers import parse_id, placeholder_format, truncate

log = logging.getLogger("cogs.tempvoice")

DEFAULT_NAME = "🔊 {user}'s channel"
MAX_NAME_LENGTH = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _channel_name(text: str, limit: int = MAX_NAME_LENGTH) -> str:
    """Sanitise an arbitrary string into a valid voice channel name."""
    cleaned = re.sub(r"[<>@#`]", "", text).strip()
    return cleaned[:limit] or "voice"


async def _fetch_temp_row(channel_id: int) -> Optional[dict]:
    """Return the temp_voice row for a channel, if any."""
    row = await database.db.fetchone("SELECT * FROM temp_voice WHERE channel_id = ?", (channel_id,))
    return dict(row) if row else None


def _owner_overwrite() -> discord.PermissionOverwrite:
    """Permission overwrite given to the owner of a temporary channel."""
    return discord.PermissionOverwrite(
        connect=True, speak=True, manage_channels=True, move_members=True, mute_members=True
    )


async def _temp_context(
    interaction: discord.Interaction,
) -> tuple[Optional[dict], Optional[discord.VoiceChannel], Optional[str]]:
    """Resolve the temp channel the invoker is in and verify they may control it.

    Returns ``(row, channel, error)`` where ``error`` is set on failure.
    """
    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        return None, None, "You must be connected to a temporary voice channel first."
    channel = member.voice.channel
    row = await _fetch_temp_row(channel.id)
    if row is None:
        return None, None, "You are not in a temporary voice channel."
    if int(row["owner_id"]) != member.id and not member.guild_permissions.manage_channels:
        return None, None, "Only the channel owner can control this channel."
    return row, channel, None


async def _delete_temp(bot: commands.Bot, channel_id: int, guild_id: int) -> None:
    """Delete a temporary channel (best-effort) and forget it in the database."""
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(channel_id) if guild else bot.get_channel(channel_id)
    if isinstance(channel, discord.VoiceChannel):
        try:
            await channel.delete(reason="Temporary voice channel is no longer needed")
        except discord.HTTPException:
            log.warning("Could not delete temporary channel %s", channel_id)
    await database.db.execute("DELETE FROM temp_voice WHERE channel_id = ?", (channel_id,))


async def _apply_tempvoice_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save the temp-voice settings coming from the setup wizard / modal."""
    lines: list[str] = []
    lobby_id = parse_id(values.get("tempvoice_lobby", ""))
    if lobby_id:
        await database.db.set(guild.id, "tempvoice_lobby", lobby_id)
        lines.append(f"Join-to-create channel: <#{lobby_id}>")
    category_id = parse_id(values.get("tempvoice_category", ""))
    if category_id:
        await database.db.set(guild.id, "tempvoice_category", category_id)
        lines.append(f"Category: <#{category_id}>")
    limit_raw = values.get("tempvoice_limit", "").strip()
    if limit_raw.isdigit():
        await database.db.set(guild.id, "tempvoice_limit", int(limit_raw))
        lines.append(f"Default user limit: {int(limit_raw)}")
    name = values.get("tempvoice_name", "").strip()
    if name:
        await database.db.set(guild.id, "tempvoice_name", truncate(name, MAX_NAME_LENGTH))
        lines.append(f"Name template: {truncate(name, 80)}")
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the temporary-voice step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="tempvoice",
            label="Temporary Voice",
            emoji="🔊",
            description="Join-to-create voice channels with owner controls.",
            fields=[
                ("Join-to-create channel", "#Join to Create", True, "tempvoice_lobby"),
                ("Category", "Voice channels (optional)", False, "tempvoice_category"),
                ("User limit", "0 (optional, 0 = unlimited)", False, "tempvoice_limit"),
                ("Channel name template", "🔊 {user}'s channel", False, "tempvoice_name"),
            ],
            handler=_apply_tempvoice_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class TempVoice(commands.Cog):
    """Join-to-create temporary voice channels."""

    tempvoice_group = app_commands.Group(
        name="tempvoice", description="Temporary voice channel controls.", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    # -- setup / config ------------------------------------------------------
    @tempvoice_group.command(name="setup", description="Configure the join-to-create voice system.")
    @app_commands.describe(
        lobby="Channel members join to have their own channel created",
        category="Category where temporary channels are created (optional)",
        limit="Default user limit, 0 = unlimited",
        name="Channel name template using {user} (optional)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def tempvoice_setup(
        self,
        interaction: discord.Interaction,
        lobby: discord.VoiceChannel,
        category: Optional[discord.CategoryChannel] = None,
        limit: app_commands.Range[int, 0, 99] = 0,
        name: Optional[str] = None,
    ) -> None:
        """Store the lobby, category, limit and name template."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "tempvoice_lobby", lobby.id)
        if category is not None:
            await database.db.set(guild.id, "tempvoice_category", category.id)
        await database.db.set(guild.id, "tempvoice_limit", int(limit))
        if name:
            await database.db.set(guild.id, "tempvoice_name", truncate(name, MAX_NAME_LENGTH))
        embed = base_embed(
            title="🔊 Temporary voice configured",
            fields=[
                ("Lobby", lobby.mention, True),
                ("Category", category.mention if category else "Lobby's category", True),
                ("User limit", str(int(limit)) if limit else "Unlimited", True),
                ("Name template", truncate(name, 80) if name else DEFAULT_NAME, False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tempvoice_group.command(name="config", description="Show the temporary voice configuration.")
    async def tempvoice_config(self, interaction: discord.Interaction) -> None:
        """Display the stored temp-voice settings."""
        guild = interaction.guild
        assert guild is not None
        lobby = await database.db.get(guild.id, "tempvoice_lobby")
        category = await database.db.get(guild.id, "tempvoice_category")
        limit = await database.db.get_int(guild.id, "tempvoice_limit", 0)
        name = await database.db.get(guild.id, "tempvoice_name", DEFAULT_NAME)
        embed = base_embed(
            title="🔊 Temporary voice settings",
            fields=[
                ("Lobby", f"<#{lobby}>" if lobby else "Not set", True),
                ("Category", f"<#{category}>" if category else "Not set", True),
                ("User limit", str(limit) if limit else "Unlimited", True),
                ("Name template", truncate(str(name), 100), False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- owner controls ------------------------------------------------------
    @tempvoice_group.command(name="lock", description="Lock your channel so nobody else can join.")
    async def tempvoice_lock(self, interaction: discord.Interaction) -> None:
        """Deny @everyone the connect permission."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        owner = interaction.user
        assert isinstance(owner, discord.Member)
        try:
            await channel.set_permissions(channel.guild.default_role, overwrite=discord.PermissionOverwrite(connect=False))
            await channel.set_permissions(owner, overwrite=_owner_overwrite())
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not lock the channel."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=success_embed("Your channel is now locked."), ephemeral=True)

    @tempvoice_group.command(name="unlock", description="Unlock your channel so others can join again.")
    async def tempvoice_unlock(self, interaction: discord.Interaction) -> None:
        """Allow @everyone to connect again."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        try:
            await channel.set_permissions(
                channel.guild.default_role, overwrite=discord.PermissionOverwrite(connect=True, speak=True)
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not unlock the channel."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=success_embed("Your channel is now unlocked."), ephemeral=True)

    @tempvoice_group.command(name="limit", description="Change the user limit of your channel.")
    @app_commands.describe(count="Maximum members (0 = unlimited)")
    async def tempvoice_limit(self, interaction: discord.Interaction, count: app_commands.Range[int, 0, 99]) -> None:
        """Set the channel's user limit."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        try:
            await channel.edit(user_limit=int(count) or None, reason=f"Limit changed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not change the user limit."), ephemeral=True
            )
            return
        text = f"The user limit is now **{int(count)}**." if count else "The user limit is now **unlimited**."
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    @tempvoice_group.command(name="rename", description="Rename your channel.")
    @app_commands.describe(name="The new channel name")
    async def tempvoice_rename(self, interaction: discord.Interaction, name: str) -> None:
        """Rename the invoker's temporary channel."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        cleaned = _channel_name(name)
        try:
            await channel.edit(name=cleaned, reason=f"Renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not rename the channel."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"Your channel is now named **{cleaned}**."), ephemeral=True
        )

    @tempvoice_group.command(name="permit", description="Allow a member to join your channel.")
    @app_commands.describe(member="Member to allow")
    async def tempvoice_permit(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Grant a member connect access."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        try:
            await channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
                reason=f"Permitted by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not update the permissions."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=success_embed(f"{member.mention} may now join."), ephemeral=True)

    @tempvoice_group.command(name="reject", description="Deny a member access to your channel.")
    @app_commands.describe(member="Member to deny")
    async def tempvoice_reject(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Revoke a member's connect access (and move them out if present)."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        try:
            await channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(connect=False),
                reason=f"Rejected by {interaction.user}",
            )
            if member in channel.members:
                await member.move_to(None)
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not update the permissions."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} may no longer join."), ephemeral=True
        )

    @tempvoice_group.command(name="kick", description="Disconnect a member from your channel.")
    @app_commands.describe(member="Member to disconnect")
    async def tempvoice_kick(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Move a member out of the temporary channel."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        if member not in channel.members:
            await interaction.response.send_message(
                embed=error_embed("That member is not in your channel."), ephemeral=True
            )
            return
        try:
            await member.move_to(None, reason=f"Kicked by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not disconnect that member."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} was disconnected."), ephemeral=True
        )

    @tempvoice_group.command(name="transfer", description="Give ownership of your channel to another member.")
    @app_commands.describe(member="The new owner (must be in your channel)")
    async def tempvoice_transfer(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Hand the channel over to another member."""
        row, channel, error = await _temp_context(interaction)
        if error or row is None or channel is None:
            await interaction.response.send_message(embed=error_embed(error or "Not a temp channel."), ephemeral=True)
            return
        if member not in channel.members:
            await interaction.response.send_message(
                embed=error_embed("That member must be in your channel to receive ownership."), ephemeral=True
            )
            return
        old_owner = interaction.user
        await database.db.execute(
            "UPDATE temp_voice SET owner_id = ? WHERE channel_id = ?", (member.id, channel.id)
        )
        try:
            await channel.set_permissions(member, overwrite=_owner_overwrite(), reason="Ownership transferred")
            if isinstance(old_owner, discord.Member) and old_owner.id != member.id:
                await channel.set_permissions(
                    old_owner, overwrite=discord.PermissionOverwrite(connect=True, speak=True)
                )
        except discord.HTTPException:
            log.warning("Could not update permissions after transfer in %s", channel.id)
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} is now the owner of this channel."), ephemeral=True
        )

    @tempvoice_group.command(name="claim", description="Claim a temporary channel whose owner has left.")
    async def tempvoice_claim(self, interaction: discord.Interaction) -> None:
        """Take ownership when the original owner is no longer in the channel."""
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                embed=error_embed("You must be connected to a temporary voice channel first."), ephemeral=True
            )
            return
        channel = member.voice.channel
        row = await _fetch_temp_row(channel.id)
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("You are not in a temporary voice channel."), ephemeral=True
            )
            return
        owner = channel.guild.get_member(int(row["owner_id"]))
        if owner is not None and owner in channel.members:
            await interaction.response.send_message(
                embed=error_embed("The current owner is still in the channel."), ephemeral=True
            )
            return
        await database.db.execute(
            "UPDATE temp_voice SET owner_id = ? WHERE channel_id = ?", (member.id, channel.id)
        )
        try:
            await channel.set_permissions(member, overwrite=_owner_overwrite(), reason="Channel claimed")
        except discord.HTTPException:
            log.warning("Could not update permissions after claim in %s", channel.id)
        await interaction.response.send_message(
            embed=success_embed("You are now the owner of this channel."), ephemeral=True
        )

    # -- listeners & background task -----------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Create channels when the lobby is joined and delete empty ones."""
        try:
            await self._handle_join(member, after)
            await self._handle_leave(before, after)
        except Exception:
            log.exception("Voice state handler failed")

    async def _handle_join(self, member: discord.Member, after: discord.VoiceState) -> None:
        """Create a temporary channel when a member joins the lobby."""
        guild = member.guild
        if member.bot or after.channel is None:
            return
        lobby_id = await database.db.get(guild.id, "tempvoice_lobby")
        if not lobby_id or after.channel.id != int(lobby_id):
            return

        category_id = await database.db.get(guild.id, "tempvoice_category")
        category = guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = after.channel.category
        limit = await database.db.get_int(guild.id, "tempvoice_limit", 0)
        template = str(await database.db.get(guild.id, "tempvoice_name", DEFAULT_NAME))
        name = _channel_name(placeholder_format(template, member=member, guild=guild, extra={"user": member.display_name}))

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            member: _owner_overwrite(),
            guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
        }
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                connect=True, manage_channels=True, move_members=True
            )
        try:
            channel = await guild.create_voice_channel(
                name=name,
                category=category,
                user_limit=limit if limit > 0 else None,
                overwrites=overwrites,
                reason=f"Temporary voice channel for {member}",
            )
        except discord.HTTPException:
            log.exception("Could not create temporary voice channel in guild %s", guild.id)
            return
        await database.db.execute(
            "INSERT INTO temp_voice (guild_id, channel_id, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (guild.id, channel.id, member.id, int(time.time())),
        )
        try:
            await member.move_to(channel, reason="Moved to their temporary channel")
        except discord.HTTPException:
            log.warning("Could not move %s into the new temporary channel", member)

    async def _handle_leave(self, before: discord.VoiceState, after: discord.VoiceState) -> None:
        """Delete a temporary channel once it becomes empty."""
        if before.channel is None or before.channel == after.channel:
            return
        row = await _fetch_temp_row(before.channel.id)
        if row is None:
            return
        if len(before.channel.members) == 0:
            await _delete_temp(self.bot, before.channel.id, int(row["guild_id"]))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the cleanup loop after a restart."""
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()

    @tasks.loop(minutes=5)
    async def cleanup_loop(self) -> None:
        """Delete tracked temporary channels that are empty or gone."""
        rows = await database.db.fetchall("SELECT * FROM temp_voice")
        for row in rows:
            data = dict(row)
            guild = self.bot.get_guild(int(data["guild_id"]))
            if guild is None:
                await database.db.execute("DELETE FROM temp_voice WHERE channel_id = ?", (data["channel_id"],))
                continue
            channel = guild.get_channel(int(data["channel_id"]))
            if not isinstance(channel, discord.VoiceChannel):
                await database.db.execute("DELETE FROM temp_voice WHERE channel_id = ?", (data["channel_id"],))
                continue
            if len(channel.members) == 0:
                await _delete_temp(self.bot, int(data["channel_id"]), int(data["guild_id"]))

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the temporary voice cog."""
    await bot.add_cog(TempVoice(bot))
