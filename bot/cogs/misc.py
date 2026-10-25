"""Misc extras: the counting game, sticky messages and channel auto-publish."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import parse_id, truncate
from utils.paginator import paginate

log = logging.getLogger("cogs.misc")

STICKY_LIMIT = 2000


# ---------------------------------------------------------------------------
# Sticky helpers
# ---------------------------------------------------------------------------
async def _repost_sticky(bot: commands.Bot, row: dict) -> None:
    """Delete the previous sticky message and post a fresh one at the bottom."""
    guild = bot.get_guild(int(row["guild_id"]))
    channel = guild.get_channel(int(row["channel_id"])) if guild else None
    if not isinstance(channel, discord.TextChannel):
        return
    if row.get("message_id"):
        try:
            previous = await channel.fetch_message(int(row["message_id"]))
            await previous.delete()
        except discord.HTTPException:
            pass
    try:
        message = await channel.send(truncate(str(row["content"]), STICKY_LIMIT))
    except discord.HTTPException:
        log.warning("Could not post the sticky message in channel %s", row["channel_id"])
        return
    await database.db.execute(
        "UPDATE sticky_messages SET message_id = ? WHERE guild_id = ? AND channel_id = ?",
        (message.id, row["guild_id"], row["channel_id"]),
    )


async def _apply_extras_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save the counting and auto-publish settings from the /setup wizard."""
    lines: list[str] = []
    counting_id = parse_id(values.get("counting_channel", ""))
    if counting_id:
        await database.db.set(guild.id, "counting_channel", counting_id)
        await database.db.set(guild.id, "counting_enabled", True)
        lines.append(f"Counting channel: <#{counting_id}>")
    channels = [
        cid
        for part in values.get("autopublish_channels", "").replace(",", " ").split()
        if (cid := parse_id(part))
    ]
    if channels:
        await database.db.set(guild.id, "autopublish_channels", channels)
        lines.append("Auto-publish channels: " + ", ".join(f"<#{cid}>" for cid in channels))
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the games & extras step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="extras",
            label="Games & Extras",
            emoji="🧩",
            description="Counting game and channel auto-publish.",
            fields=[
                ("Counting channel", "#counting (optional)", False, "counting_channel"),
                (
                    "Auto-publish channels (mentions)",
                    "#announcements (optional)",
                    False,
                    "autopublish_channels",
                ),
            ],
            handler=_apply_extras_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Misc(commands.Cog):
    """Counting game, sticky messages and auto-publish."""

    counting_group = app_commands.Group(name="counting", description="Counting game.", guild_only=True)
    sticky_group = app_commands.Group(name="sticky", description="Sticky messages.", guild_only=True)
    autopublish_group = app_commands.Group(
        name="autopublish", description="Auto-publish announcement channels.", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- counting commands ---------------------------------------------------
    @counting_group.command(name="setup", description="Set the counting channel and enable the game.")
    @app_commands.describe(channel="The channel where members count")
    @app_commands.default_permissions(manage_guild=True)
    async def counting_setup(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Enable counting in a channel."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "counting_channel", channel.id)
        await database.db.set(interaction.guild.id, "counting_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(f"Counting is now enabled in {channel.mention}. Start with **1**!"), ephemeral=True
        )

    @counting_group.command(name="toggle", description="Enable or disable the counting game.")
    @app_commands.describe(enabled="Whether counting is active")
    @app_commands.default_permissions(manage_guild=True)
    async def counting_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the counting game."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "counting_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Counting is now **{'enabled' if enabled else 'disabled'}**."), ephemeral=True
        )

    @counting_group.command(name="stats", description="Show the counting statistics for this server.")
    async def counting_stats(self, interaction: discord.Interaction) -> None:
        """Display the current count, the last counter and the channel."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone("SELECT * FROM counting WHERE guild_id = ?", (guild.id,))
        channel_id = await database.db.get(guild.id, "counting_channel")
        enabled = await database.db.get_bool(guild.id, "counting_enabled", False)
        current = int(row["current"]) if row else 0
        last_user = int(row["last_user"]) if row and row["last_user"] else None
        embed = base_embed(
            title="🔢 Counting statistics",
            fields=[
                ("Current count", str(current), True),
                ("Next number", str(current + 1), True),
                ("Last counter", f"<@{last_user}>" if last_user else "—", True),
                ("Channel", f"<#{channel_id}>" if channel_id else "Not set", True),
                ("Status", "Enabled" if enabled else "Disabled", True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @counting_group.command(name="reset", description="Reset the count back to zero.")
    @app_commands.default_permissions(manage_messages=True)
    async def counting_reset(self, interaction: discord.Interaction) -> None:
        """Reset the stored count."""
        guild = interaction.guild
        assert guild is not None
        channel_id = await database.db.get(guild.id, "counting_channel")
        await database.db.execute(
            "INSERT INTO counting (guild_id, channel_id, current, last_user) VALUES (?, ?, 0, NULL) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, current = 0, last_user = NULL",
            (guild.id, channel_id),
        )
        await interaction.response.send_message(
            embed=success_embed("The count was reset. Start again from **1**!"), ephemeral=True
        )

    # -- sticky commands -----------------------------------------------------
    @sticky_group.command(name="set", description="Add or replace a sticky message in a channel.")
    @app_commands.describe(channel="The channel that should keep the message at the bottom", content="The text to keep sticky")
    @app_commands.default_permissions(manage_guild=True)
    async def sticky_set(
        self, interaction: discord.Interaction, channel: discord.TextChannel, content: str
    ) -> None:
        """Store a sticky message and post it immediately."""
        guild = interaction.guild
        assert guild is not None
        await database.db.execute(
            "INSERT INTO sticky_messages (guild_id, channel_id, content, message_id) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(guild_id, channel_id) DO UPDATE SET content = excluded.content, message_id = NULL",
            (guild.id, channel.id, truncate(content, STICKY_LIMIT)),
        )
        row = await database.db.fetchone(
            "SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
        if row is not None:
            await _repost_sticky(self.bot, dict(row))
        await interaction.response.send_message(
            embed=success_embed(f"A sticky message was set in {channel.mention}."), ephemeral=True
        )

    @sticky_group.command(name="remove", description="Remove the sticky message from a channel.")
    @app_commands.describe(channel="The channel to clear")
    @app_commands.default_permissions(manage_guild=True)
    async def sticky_remove(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Delete a sticky message and its database row."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=info_embed("There is no sticky message in that channel."), ephemeral=True
            )
            return
        if row["message_id"]:
            try:
                previous = await channel.fetch_message(int(row["message_id"]))
                await previous.delete()
            except discord.HTTPException:
                pass
        await database.db.execute(
            "DELETE FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"The sticky message in {channel.mention} was removed."), ephemeral=True
        )

    @sticky_group.command(name="edit", description="Change the text of an existing sticky message.")
    @app_commands.describe(channel="The channel with the sticky message", content="The new text")
    @app_commands.default_permissions(manage_guild=True)
    async def sticky_edit(
        self, interaction: discord.Interaction, channel: discord.TextChannel, content: str
    ) -> None:
        """Update a sticky message in place and repost it."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("There is no sticky message in that channel. Use `/sticky set` first."),
                ephemeral=True,
            )
            return
        await database.db.execute(
            "UPDATE sticky_messages SET content = ?, message_id = NULL WHERE guild_id = ? AND channel_id = ?",
            (truncate(content, STICKY_LIMIT), guild.id, channel.id),
        )
        fresh = await database.db.fetchone(
            "SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
        if fresh is not None:
            await _repost_sticky(self.bot, dict(fresh))
        await interaction.response.send_message(
            embed=success_embed(f"The sticky message in {channel.mention} was updated."), ephemeral=True
        )

    @sticky_group.command(name="list", description="List every sticky message on this server.")
    async def sticky_list(self, interaction: discord.Interaction) -> None:
        """Show all configured sticky messages."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM sticky_messages WHERE guild_id = ? ORDER BY channel_id", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No sticky messages are configured."), ephemeral=True
            )
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="📌 Sticky messages")
            for row in rows[start : start + 5]:
                embed.add_field(
                    name=f"<#{row['channel_id']}>",
                    value=truncate(str(row["content"]), 200),
                    inline=False,
                )
            embed.set_footer(text=f"{len(rows)} sticky message(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    # -- auto-publish commands -----------------------------------------------
    @autopublish_group.command(name="add", description="Add an announcement channel to the auto-publish list.")
    @app_commands.describe(channel="The announcement channel to publish automatically")
    @app_commands.default_permissions(manage_guild=True)
    async def autopublish_add(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Track a channel whose messages are published automatically."""
        guild = interaction.guild
        assert guild is not None
        if not channel.is_news():
            await interaction.response.send_message(
                embed=error_embed("That is not an announcement channel."), ephemeral=True
            )
            return
        channels = [int(cid) for cid in await database.db.get(guild.id, "autopublish_channels", [])]
        if channel.id in channels:
            await interaction.response.send_message(
                embed=info_embed("That channel is already auto-published."), ephemeral=True
            )
            return
        channels.append(channel.id)
        await database.db.set(guild.id, "autopublish_channels", channels)
        await interaction.response.send_message(
            embed=success_embed(f"{channel.mention} will now auto-publish its messages."), ephemeral=True
        )

    @autopublish_group.command(name="remove", description="Remove a channel from the auto-publish list.")
    @app_commands.describe(channel="The announcement channel to stop publishing")
    @app_commands.default_permissions(manage_guild=True)
    async def autopublish_remove(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Stop tracking a channel for auto-publish."""
        guild = interaction.guild
        assert guild is not None
        channels = [int(cid) for cid in await database.db.get(guild.id, "autopublish_channels", [])]
        if channel.id not in channels:
            await interaction.response.send_message(
                embed=info_embed("That channel is not in the auto-publish list."), ephemeral=True
            )
            return
        channels = [cid for cid in channels if cid != channel.id]
        await database.db.set(guild.id, "autopublish_channels", channels)
        await interaction.response.send_message(
            embed=success_embed(f"{channel.mention} will no longer auto-publish."), ephemeral=True
        )

    @autopublish_group.command(name="list", description="List the channels that auto-publish.")
    async def autopublish_list(self, interaction: discord.Interaction) -> None:
        """Show the configured auto-publish channels."""
        guild = interaction.guild
        assert guild is not None
        channels = [int(cid) for cid in await database.db.get(guild.id, "autopublish_channels", [])]
        if not channels:
            await interaction.response.send_message(
                embed=info_embed("No channels are set to auto-publish."), ephemeral=True
            )
            return
        embed = base_embed(
            title="📣 Auto-publish channels",
            description="\n".join(f"<#{cid}>" for cid in channels),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- message listener ----------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Run the counting, sticky and auto-publish logic for this message."""
        if message.guild is None:
            return
        try:
            await self._handle_autopublish(message)
            await self._handle_sticky(message)
            await self._handle_counting(message)
        except Exception:
            log.exception("Misc message handler failed in guild %s", message.guild.id)

    async def _handle_autopublish(self, message: discord.Message) -> None:
        """Publish messages posted in tracked announcement channels."""
        guild = message.guild
        assert guild is not None
        if message.author.bot:
            return
        channel = message.channel
        if not isinstance(channel, discord.TextChannel) or not channel.is_news():
            return
        channels = [int(cid) for cid in await database.db.get(guild.id, "autopublish_channels", [])]
        if channel.id not in channels:
            return
        try:
            await message.publish()
        except discord.HTTPException:
            log.warning("Could not publish message %s", message.id)

    async def _handle_sticky(self, message: discord.Message) -> None:
        """Re-post the sticky message of the channel, if one exists."""
        guild = message.guild
        assert guild is not None
        if message.author.bot:
            return
        row = await database.db.fetchone(
            "SELECT * FROM sticky_messages WHERE guild_id = ? AND channel_id = ?",
            (guild.id, message.channel.id),
        )
        if row is None:
            return
        if row["message_id"] and int(row["message_id"]) == message.id:
            return
        await _repost_sticky(self.bot, dict(row))

    async def _handle_counting(self, message: discord.Message) -> None:
        """Validate the next number in the counting channel."""
        guild = message.guild
        assert guild is not None
        if message.author.bot:
            return
        if not await database.db.get_bool(guild.id, "counting_enabled", False):
            return
        channel_id = await database.db.get(guild.id, "counting_channel")
        if not channel_id or message.channel.id != int(channel_id):
            return

        content = message.content.strip()
        row = await database.db.fetchone("SELECT * FROM counting WHERE guild_id = ?", (guild.id,))
        current = int(row["current"]) if row else 0
        last_user = int(row["last_user"]) if row and row["last_user"] else None

        if not content.isdigit():
            await self._safe_delete(message)
            await self._safe_react(message, "❌")
            await self._notify(
                message.channel,
                warn_embed("Numbers only! Send the next number to keep the count going."),
                6,
            )
            return

        number = int(content)
        same_user = last_user == message.author.id
        if number == current + 1 and not same_user:
            await database.db.execute(
                "INSERT INTO counting (guild_id, channel_id, current, last_user) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, "
                "current = excluded.current, last_user = excluded.last_user",
                (guild.id, message.channel.id, number, message.author.id),
            )
            await self._safe_react(message, "✅")
            return

        await database.db.execute(
            "INSERT INTO counting (guild_id, channel_id, current, last_user) VALUES (?, ?, 0, NULL) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, current = 0, last_user = NULL",
            (guild.id, message.channel.id),
        )
        await self._safe_react(message, "❌")
        reason = "you counted twice in a row" if same_user else f"the next number was **{current + 1}**"
        await self._notify(
            message.channel,
            error_embed(f"The count was ruined by {message.author.mention} — {reason}. Restarting from **1**!"),
            None,
        )

    @staticmethod
    async def _safe_delete(message: discord.Message) -> None:
        """Delete a message, ignoring missing permissions."""
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    @staticmethod
    async def _safe_react(message: discord.Message, emoji: str) -> None:
        """Add a reaction, ignoring failures."""
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    @staticmethod
    async def _notify(channel: discord.abc.Messageable, embed: discord.Embed, delete_after: Optional[float]) -> None:
        """Send a short informative message, optionally self-deleting."""
        try:
            await channel.send(embed=embed, delete_after=delete_after)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    """Load the misc cog."""
    await bot.add_cog(Misc(bot))
