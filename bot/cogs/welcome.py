"""Welcome & leave system: customizable messages (text/embed/image-card),
optional DM welcome, auto-roles for humans and bots, and a button/captcha
verification system."""
from __future__ import annotations

import io
import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import parse_id, placeholder_format, truncate
from utils.imagecard import render_welcome_card

log = logging.getLogger("cogs.welcome")

DEFAULT_WELCOME = "Welcome to **{server}**, {user}! You are member **{membercount}**."
DEFAULT_LEAVE = "Goodbye **{user}**, thanks for being part of **{server}**."

# pending captcha challenges: (guild_id, user_id) -> answer
_CAPTCHAS: dict[tuple[int, int], int] = {}


# ---------------------------------------------------------------------------
# Settings helpers (shared with the /setup wizard)
# ---------------------------------------------------------------------------
async def _apply_welcome_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save welcome/leave settings from wizard/modal values."""
    lines: list[str] = []
    channel_id = parse_id(values.get("welcome_channel", ""))
    if channel_id:
        await database.db.set(guild.id, "welcome_channel", channel_id)
        await database.db.set(guild.id, "welcome_enabled", True)
        lines.append(f"Welcome channel: <#{channel_id}>")
    message = values.get("welcome_message", "").strip()
    if message:
        await database.db.set(guild.id, "welcome_message", message)
        lines.append(f"Welcome message: {truncate(message, 100)}")
    leave_channel_id = parse_id(values.get("leave_channel", ""))
    if leave_channel_id:
        await database.db.set(guild.id, "leave_channel", leave_channel_id)
        await database.db.set(guild.id, "leave_enabled", True)
        lines.append(f"Leave channel: <#{leave_channel_id}>")
    leave_message = values.get("leave_message", "").strip()
    if leave_message:
        await database.db.set(guild.id, "leave_message", leave_message)
        lines.append(f"Leave message: {truncate(leave_message, 100)}")
    autorole_id = parse_id(values.get("autorole", ""))
    if autorole_id:
        await database.db.set(guild.id, "autorole_human", autorole_id)
        lines.append(f"Auto-role: <@&{autorole_id}>")
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the welcome step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="welcome",
            label="Welcome & Leave",
            emoji="👋",
            description="Welcome/goodbye channels, messages and auto-role.",
            fields=[
                ("Welcome channel", "#welcome", False, "welcome_channel"),
                ("Welcome message", "Welcome {user} to {server}!", False, "welcome_message"),
                ("Leave channel", "#goodbye", False, "leave_channel"),
                ("Leave message", "Goodbye {user}!", False, "leave_message"),
                ("Auto-role (humans)", "@Member", False, "autorole"),
            ],
            handler=_apply_welcome_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def _accent_color(guild: discord.Guild) -> tuple[int, int, int]:
    """Accent colour used by the welcome card (theme colour, per-guild override)."""
    return config.THEME_COLOR.to_rgb()


async def build_welcome_content(member: discord.Member) -> tuple[Optional[str], Optional[discord.Embed], Optional[discord.File]]:
    """Build the configured welcome (content, embed, file) triple for a member."""
    guild = member.guild
    message = await database.db.get(guild.id, "welcome_message", DEFAULT_WELCOME)
    text = placeholder_format(message, member=member, guild=guild)
    style = await database.db.get(guild.id, "welcome_style", "embed")
    accent = _accent_color(guild)

    if style == "image":
        avatar = await member.display_avatar.read()
        card = await render_welcome_card(
            avatar,
            member.display_name,
            guild.name,
            guild.member_count,
            accent=accent,
            title="Welcome!",
            subtitle=truncate(f"Enjoy your stay in {guild.name}", 40),
        )
        file = discord.File(io.BytesIO(card), filename="welcome.png")
        embed = base_embed(color=member.color if member.color != discord.Color.default() else None)
        embed.set_image(url="attachment://welcome.png")
        return member.mention, embed, file

    if style == "embed":
        embed = base_embed(
            title=f"Welcome, {member.display_name}!",
            description=truncate(text, 4000),
            color=member.color if member.color != discord.Color.default() else None,
            thumbnail=member.display_avatar.url,
        )
        return None, embed, None

    return text, None, None


# ---------------------------------------------------------------------------
# Verification UI
# ---------------------------------------------------------------------------
class VerifyModal(discord.ui.Modal):
    """Captcha modal: solve a simple arithmetic question."""

    def __init__(self, first: int, second: int) -> None:
        super().__init__(title="Verification", timeout=120)
        self.expected = first + second
        self.code_input = discord.ui.TextInput(
            label=f"What is {first} + {second}?",
            placeholder="Type the result…",
            required=True,
            max_length=6,
        )
        self.add_item(self.code_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
            return
        expected = _CAPTCHAS.pop((guild.id, member.id), None)
        try:
            given = int(str(self.code_input.value).strip())
        except ValueError:
            given = -1
        if expected is None or given != expected:
            await interaction.response.send_message(
                embed=error_embed("That answer is incorrect. Please press the verify button again."), ephemeral=True
            )
            return
        await _grant_verified(interaction, guild, member)


async def _grant_verified(interaction: discord.Interaction, guild: discord.Guild, member: discord.Member) -> None:
    """Give the verification role and confirm."""
    role_id = await database.db.get(guild.id, "verify_role")
    role = guild.get_role(int(role_id)) if role_id else None
    if role is None:
        await interaction.response.send_message(
            embed=error_embed("The verification role is not configured. Ask an admin to run `/verification setup`."),
            ephemeral=True,
        )
        return
    if role in member.roles:
        await interaction.response.send_message(embed=info_embed("You are already verified."), ephemeral=True)
        return
    try:
        await member.add_roles(role, reason="Verification passed")
    except discord.HTTPException:
        await interaction.response.send_message(
            embed=error_embed("I could not give you the role. Please contact a staff member."), ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=success_embed(f"You have been verified — welcome to **{guild.name}**!"), ephemeral=True
    )
    log_channel_id = await database.db.get(guild.id, "verify_log_channel")
    if log_channel_id:
        channel = guild.get_channel(int(log_channel_id))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=base_embed(title="✅ Member verified", description=f"{member.mention} passed verification."))
            except discord.HTTPException:
                pass


class VerifyButton(discord.ui.Button):
    """Verification button (simple click or captcha, depending on mode)."""

    def __init__(self) -> None:
        super().__init__(label="Verify", emoji="✅", style=discord.ButtonStyle.success, custom_id="verify:click")

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
            return
        mode = await database.db.get(guild.id, "verify_mode", "button")
        if mode == "captcha":
            first = random.randint(3, 40)
            second = random.randint(3, 40)
            _CAPTCHAS[(guild.id, member.id)] = first + second
            await interaction.response.send_modal(VerifyModal(first, second))
            return
        await _grant_verified(interaction, guild, member)


class VerificationView(discord.ui.View):
    """Persistent verification panel view."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(VerifyButton())


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Welcome(commands.Cog):
    """Welcome, leave, auto-role and verification commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- welcome ------------------------------------------------------------
    welcome_group = app_commands.Group(
        name="welcome", description="Welcome message configuration.", guild_only=True
    )
    leave_group = app_commands.Group(name="leave", description="Leave message configuration.", guild_only=True)
    autorole_group = app_commands.Group(name="autorole", description="Auto-role configuration.", guild_only=True)
    verification_group = app_commands.Group(
        name="verification", description="Verification system configuration.", guild_only=True
    )

    @welcome_group.command(name="channel", description="Set the welcome channel and enable welcome messages.")
    @app_commands.describe(channel="Where welcome messages are posted")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Set the welcome channel."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "welcome_channel", channel.id)
        await database.db.set(interaction.guild.id, "welcome_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(f"Welcome messages will be posted in {channel.mention}."), ephemeral=True
        )

    @welcome_group.command(name="message", description="Set the welcome message text.")
    @app_commands.describe(
        message="Use {user}, {server}, {membercount} placeholders",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_message(self, interaction: discord.Interaction, message: str) -> None:
        """Store the welcome message template."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "welcome_message", truncate(message, 1500))
        await interaction.response.send_message(
            embed=success_embed(f"Welcome message saved:\n{truncate(message, 400)}"), ephemeral=True
        )

    @welcome_group.command(name="style", description="Choose how welcome messages are displayed.")
    @app_commands.describe(style="Plain text, themed embed or generated image card")
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Embed", value="embed"),
            app_commands.Choice(name="Text", value="text"),
            app_commands.Choice(name="Image card", value="image"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_style(self, interaction: discord.Interaction, style: app_commands.Choice[str]) -> None:
        """Set the welcome style."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "welcome_style", style.value)
        await interaction.response.send_message(
            embed=success_embed(f"Welcome style set to **{style.name}**."), ephemeral=True
        )

    @welcome_group.command(name="toggle", description="Enable or disable welcome messages.")
    @app_commands.describe(enabled="Whether welcome messages are sent")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle welcome messages."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "welcome_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Welcome messages are now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    @welcome_group.command(name="dm", description="Also send the welcome message as a DM.")
    @app_commands.describe(enabled="Whether to DM new members")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_dm(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle DM welcome."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "welcome_dm", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Welcome DMs are now **{'enabled' if enabled else 'disabled'}**."), ephemeral=True
        )

    @welcome_group.command(name="preview", description="Preview the welcome message in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_preview(self, interaction: discord.Interaction) -> None:
        """Send a preview of the configured welcome message."""
        guild = interaction.guild
        assert guild is not None
        content, embed, file = await build_welcome_content(interaction.user)  # type: ignore[arg-type]
        kwargs: dict = {"content": content, "allowed_mentions": discord.AllowedMentions(users=True)}
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        await interaction.response.send_message(**kwargs)

    # -- leave -------------------------------------------------------------
    @leave_group.command(name="channel", description="Set the leave channel and enable leave messages.")
    @app_commands.describe(channel="Where leave messages are posted")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Set the leave channel."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "leave_channel", channel.id)
        await database.db.set(interaction.guild.id, "leave_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(f"Leave messages will be posted in {channel.mention}."), ephemeral=True
        )

    @leave_group.command(name="message", description="Set the leave message text.")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_message(self, interaction: discord.Interaction, message: str) -> None:
        """Store the leave message template."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "leave_message", truncate(message, 1500))
        await interaction.response.send_message(
            embed=success_embed(f"Leave message saved:\n{truncate(message, 400)}"), ephemeral=True
        )

    @leave_group.command(name="toggle", description="Enable or disable leave messages.")
    @app_commands.describe(enabled="Whether leave messages are sent")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle leave messages."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "leave_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Leave messages are now **{'enabled' if enabled else 'disabled'}**."), ephemeral=True
        )

    @leave_group.command(name="preview", description="Preview the leave message in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_preview(self, interaction: discord.Interaction) -> None:
        """Send a preview of the configured leave message."""
        guild = interaction.guild
        assert guild is not None
        template = await database.db.get(guild.id, "leave_message", DEFAULT_LEAVE)
        text = placeholder_format(template, member=interaction.user, guild=guild)  # type: ignore[arg-type]
        await interaction.response.send_message(text)

    # -- autorole -----------------------------------------------------------
    @autorole_group.command(name="human", description="Set the role given automatically to human members.")
    @app_commands.describe(role="Role to give on join")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole_human(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Set the human auto-role."""
        assert interaction.guild is not None
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                embed=error_embed("That role is higher than my highest role."), ephemeral=True
            )
            return
        await database.db.set(interaction.guild.id, "autorole_human", role.id)
        await interaction.response.send_message(
            embed=success_embed(f"New human members will receive {role.mention}."), ephemeral=True
        )

    @autorole_group.command(name="bot", description="Set the role given automatically to bot members.")
    @app_commands.describe(role="Role to give bots on join")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole_bot(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Set the bot auto-role."""
        assert interaction.guild is not None
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                embed=error_embed("That role is higher than my highest role."), ephemeral=True
            )
            return
        await database.db.set(interaction.guild.id, "autorole_bot", role.id)
        await interaction.response.send_message(
            embed=success_embed(f"New bot members will receive {role.mention}."), ephemeral=True
        )

    @autorole_group.command(name="disable", description="Disable both auto-roles.")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole_disable(self, interaction: discord.Interaction) -> None:
        """Remove the configured auto-roles."""
        assert interaction.guild is not None
        await database.db.delete(interaction.guild.id, "autorole_human")
        await database.db.delete(interaction.guild.id, "autorole_bot")
        await interaction.response.send_message(embed=success_embed("Auto-roles disabled."), ephemeral=True)

    # -- verification --------------------------------------------------------
    @verification_group.command(name="setup", description="Configure the verification system.")
    @app_commands.describe(
        role="Role given after verification",
        mode="Simple button click or math captcha",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Button", value="button"),
            app_commands.Choice(name="Captcha (math)", value="captcha"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    async def verification_setup(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        mode: app_commands.Choice[str],
    ) -> None:
        """Enable verification with the given role and mode."""
        assert interaction.guild is not None
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                embed=error_embed("That role is higher than my highest role."), ephemeral=True
            )
            return
        await database.db.set(interaction.guild.id, "verify_role", role.id)
        await database.db.set(interaction.guild.id, "verify_mode", mode.value)
        await database.db.set(interaction.guild.id, "verify_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(
                f"Verification enabled — mode **{mode.name}**, role {role.mention}.\n"
                "Use `/verification post` in a channel to place the panel."
            ),
            ephemeral=True,
        )

    @verification_group.command(name="post", description="Post the verification panel in a channel.")
    @app_commands.describe(channel="Where to post the panel")
    @app_commands.default_permissions(manage_guild=True)
    async def verification_post(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Send the persistent verification panel."""
        guild = interaction.guild
        assert guild is not None
        channel = channel or interaction.channel  # type: ignore[assignment]
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Pick a text channel."), ephemeral=True)
            return
        role_id = await database.db.get(guild.id, "verify_role")
        if not role_id:
            await interaction.response.send_message(
                embed=error_embed("Run `/verification setup` first."), ephemeral=True
            )
            return
        mode = await database.db.get(guild.id, "verify_mode", "button")
        description = (
            f"Click the button below to gain access to the server.\n"
            + ("*You will be asked a quick question first.*" if mode == "captcha" else "*One click and you're in.*")
        )
        embed = base_embed(title="🔐 Verification required", description=description, color=config.WARNING_COLOR)
        await channel.send(embed=embed, view=VerificationView())
        await database.db.set(guild.id, "verify_channel", channel.id)
        await interaction.response.send_message(
            embed=success_embed(f"Verification panel posted in {channel.mention}."), ephemeral=True
        )

    @verification_group.command(name="disable", description="Disable the verification system.")
    @app_commands.default_permissions(manage_guild=True)
    async def verification_disable(self, interaction: discord.Interaction) -> None:
        """Turn verification off."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "verify_enabled", False)
        await interaction.response.send_message(embed=success_embed("Verification disabled."), ephemeral=True)

    # -- listeners -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Send the welcome message, DM and auto-role on join."""
        guild = member.guild
        # Auto-roles
        try:
            if member.bot:
                role_id = await database.db.get(guild.id, "autorole_bot")
            else:
                role_id = await database.db.get(guild.id, "autorole_human")
            if role_id:
                role = guild.get_role(int(role_id))
                if role is not None and role < guild.me.top_role:
                    await member.add_roles(role, reason="Auto-role")
        except discord.HTTPException:
            log.warning("Auto-role failed for %s in guild %s", member, guild.id)

        # Welcome message
        if await database.db.get_bool(guild.id, "welcome_enabled", False):
            channel_id = await database.db.get(guild.id, "welcome_channel")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                try:
                    content, embed, file = await build_welcome_content(member)
                    kwargs: dict = {"content": content}
                    if embed is not None:
                        kwargs["embed"] = embed
                    if file is not None:
                        kwargs["file"] = file
                    await channel.send(**kwargs)
                except discord.HTTPException:
                    log.warning("Welcome message failed in guild %s", guild.id)

        # Optional DM welcome
        if await database.db.get_bool(guild.id, "welcome_dm", False):
            template = await database.db.get(guild.id, "welcome_message", DEFAULT_WELCOME)
            text = placeholder_format(template, member=member, guild=guild)
            try:
                await member.send(
                    embed=base_embed(
                        title=f"Welcome to {guild.name}!", description=truncate(text, 4000),
                        thumbnail=guild.icon.url if guild.icon else None,
                    )
                )
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Send the leave message."""
        guild = member.guild
        if not await database.db.get_bool(guild.id, "leave_enabled", False):
            return
        channel_id = await database.db.get(guild.id, "leave_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        template = await database.db.get(guild.id, "leave_message", DEFAULT_LEAVE)
        text = placeholder_format(template, member=member, guild=guild)
        try:
            await channel.send(text)
        except discord.HTTPException:
            log.warning("Leave message failed in guild %s", guild.id)


async def setup(bot: commands.Bot) -> None:
    """Load the welcome cog."""
    await bot.add_cog(Welcome(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [VerificationView()]
