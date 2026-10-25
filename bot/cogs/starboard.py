"""Starboard: highlight the best messages of a server by reaction count.

Recounts the configured emoji whenever a reaction is added or removed and
creates / updates / removes the starboard entry accordingly."""
from __future__ import annotations

import logging
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import parse_id, truncate

log = logging.getLogger("cogs.starboard")

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _parse_message_ref(text: str) -> tuple[Optional[int], Optional[int]]:
    """Extract ``(channel_id, message_id)`` from a message link or a raw id."""
    cleaned = text.strip()
    if "discord.com/channels/" in cleaned:
        parts = [part for part in cleaned.split("/") if part]
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
            return int(parts[-2]), int(parts[-1])
    return None, parse_id(cleaned)


def _emoji_matches(emoji: Union[str, discord.PartialEmoji, discord.Emoji], configured: str) -> bool:
    """Compare a reaction emoji with the configured starboard emoji."""
    name = getattr(emoji, "name", emoji)
    return str(name) == configured or str(emoji) == configured


def _first_image(message: discord.Message) -> Optional[str]:
    """Return the URL of the first image attachment, if any."""
    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        url = attachment.url.split("?")[0].lower()
        if content_type.startswith("image/") or url.endswith(IMAGE_SUFFIXES):
            return attachment.url
    return None


async def _ignored_channels(guild_id: int) -> list[int]:
    """Return the channels ignored by the starboard."""
    values = await database.db.get(guild_id, "starboard_ignore_channels", [])
    return [int(value) for value in values]


async def _star_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Return the configured starboard channel, if it exists."""
    channel_id = await database.db.get_int(guild.id, "starboard_channel", 0)
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


async def _star_count(message: discord.Message, emoji: str, *, self_star: bool) -> int:
    """Count the reactions matching ``emoji`` (optionally ignoring the author)."""
    for reaction in message.reactions:
        if not _emoji_matches(reaction.emoji, emoji):
            continue
        if self_star:
            return reaction.count
        users = [user async for user in reaction.users()]
        return sum(1 for user in users if user.id != message.author.id)
    return 0


def _star_embed(message: discord.Message, emoji: str, count: int) -> discord.Embed:
    """Build the starboard embed for an original message."""
    embed = base_embed(
        title=f"{emoji} Starboard",
        description=truncate(message.content, 4000) or "*This message has no text content.*",
        author=message.author,
        image=_first_image(message),
        fields=[
            ("Original channel", f"<#{message.channel.id}>", True),
            ("Message ID", str(message.id), True),
        ],
    )
    embed.set_footer(text=f"{emoji} {count} star{'s' if count != 1 else ''} • {config.FOOTER_TEXT}")
    return embed


# ---------------------------------------------------------------------------
# Settings helper (shared with the /setup wizard)
# ---------------------------------------------------------------------------
async def _apply_starboard_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save starboard settings from the /setup wizard."""
    lines: list[str] = []
    channel_id = parse_id(values.get("channel", ""))
    if channel_id:
        await database.db.set(guild.id, "starboard_channel", channel_id)
        await database.db.set(guild.id, "starboard_enabled", True)
        lines.append(f"Starboard channel: <#{channel_id}> (enabled)")
    emoji = values.get("emoji", "").strip()
    if emoji:
        await database.db.set(guild.id, "starboard_emoji", emoji[:100])
        lines.append(f"Emoji: {emoji}")
    threshold = values.get("threshold", "").strip()
    if threshold:
        if threshold.isdigit() and 1 <= int(threshold) <= 100:
            await database.db.set(guild.id, "starboard_threshold", int(threshold))
            lines.append(f"Threshold: **{threshold}** star(s)")
        else:
            lines.append(f"Threshold ignored (use a number between 1 and 100): `{threshold}`")
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="starboard",
        label="Starboard",
        emoji="⭐",
        description="Starboard channel, emoji and star threshold.",
        fields=[
            ("Starboard channel", "#starboard", True, "channel"),
            ("Emoji", "⭐", False, "emoji"),
            ("Threshold", "3", False, "threshold"),
        ],
        handler=_apply_starboard_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Starboard(commands.Cog):
    """Starboard commands and reaction listener."""

    starboard_group = app_commands.Group(
        name="starboard",
        description="Highlight the best messages of this server.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- listeners ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Recount the stars of a message when a reaction is added."""
        try:
            await self._handle_reaction(payload)
        except (discord.HTTPException, discord.Forbidden):
            log.exception("Could not update the starboard for message %s", payload.message_id)
        except Exception:
            log.exception("Starboard handling failed for message %s", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Recount the stars of a message when a reaction is removed."""
        try:
            await self._handle_reaction(payload)
        except (discord.HTTPException, discord.Forbidden):
            log.exception("Could not update the starboard for message %s", payload.message_id)
        except Exception:
            log.exception("Starboard handling failed for message %s", payload.message_id)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        """Recompute the star count and add / update / remove the entry."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if not await database.db.get_bool(guild.id, "starboard_enabled", False):
            return
        emoji = str(await database.db.get(guild.id, "starboard_emoji", "⭐") or "⭐")
        if not _emoji_matches(payload.emoji, emoji):
            return
        channel = await _star_channel(guild)
        if channel is None or payload.channel_id == channel.id:
            return
        ignored = await _ignored_channels(guild.id)
        if payload.channel_id in ignored:
            return
        source = guild.get_channel(payload.channel_id)
        if not isinstance(source, discord.TextChannel):
            return
        try:
            message = await source.fetch_message(payload.message_id)
        except (discord.HTTPException, discord.Forbidden):
            return
        if message.author.bot and await database.db.get_bool(guild.id, "starboard_ignore_bots", True):
            return
        self_star = await database.db.get_bool(guild.id, "starboard_self_star", False)
        count = await _star_count(message, emoji, self_star=self_star)
        threshold = await database.db.get_int(
            guild.id, "starboard_threshold", config.STARBOARD_DEFAULT_COUNT
        )
        if count >= threshold:
            await self._add_star(guild, message, count, emoji)
        else:
            await self._remove_star(guild, message.id)

    # -- starboard entries -----------------------------------------------------
    async def _add_star(
        self, guild: discord.Guild, message: discord.Message, count: int, emoji: str
    ) -> Optional[int]:
        """Create or update the starboard entry of a message."""
        channel = await _star_channel(guild)
        if channel is None:
            return None
        embed = _star_embed(message, emoji, count)
        content = f"{emoji} <#{message.channel.id}>"
        row = await database.db.fetchone(
            "SELECT * FROM starboard_messages WHERE guild_id = ? AND original_id = ?",
            (guild.id, message.id),
        )
        if row is None:
            try:
                sent = await channel.send(content=content, embed=embed)
            except (discord.HTTPException, discord.Forbidden):
                log.warning("Could not post the starboard entry for message %s", message.id)
                return None
            await database.db.execute(
                "INSERT INTO starboard_messages (guild_id, original_id, channel_id, star_message_id, stars) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild.id, message.id, message.channel.id, sent.id, count),
            )
            return sent.id
        star_message: Optional[discord.Message] = None
        if row["star_message_id"]:
            try:
                star_message = await channel.fetch_message(int(row["star_message_id"]))
            except (discord.HTTPException, discord.Forbidden):
                star_message = None
        if star_message is None:
            try:
                sent = await channel.send(content=content, embed=embed)
            except (discord.HTTPException, discord.Forbidden):
                log.warning("Could not re-post the starboard entry for message %s", message.id)
                return None
            star_message_id = sent.id
        else:
            star_message_id = star_message.id
            try:
                await star_message.edit(content=content, embed=embed)
            except (discord.HTTPException, discord.Forbidden):
                log.warning("Could not edit the starboard entry for message %s", message.id)
        await database.db.execute(
            "UPDATE starboard_messages SET star_message_id = ?, channel_id = ?, stars = ? "
            "WHERE guild_id = ? AND original_id = ?",
            (star_message_id, message.channel.id, count, guild.id, message.id),
        )
        return star_message_id

    async def _remove_star(self, guild: discord.Guild, original_id: int) -> bool:
        """Delete the starboard entry of a message (when it drops below the threshold)."""
        row = await database.db.fetchone(
            "SELECT * FROM starboard_messages WHERE guild_id = ? AND original_id = ?",
            (guild.id, original_id),
        )
        if row is None:
            return False
        channel = await _star_channel(guild)
        if channel is not None and row["star_message_id"]:
            try:
                message = await channel.fetch_message(int(row["star_message_id"]))
                await message.delete()
            except (discord.HTTPException, discord.Forbidden):
                pass
        await database.db.execute(
            "DELETE FROM starboard_messages WHERE guild_id = ? AND original_id = ?",
            (guild.id, original_id),
        )
        return True

    # -- /starboard ------------------------------------------------------------
    @starboard_group.command(name="channel", description="Set the starboard channel and enable it.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel where starred messages are posted")
    @app_commands.guild_only()
    async def starboard_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        """Store the starboard channel."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "starboard_channel", channel.id)
        await database.db.set(guild.id, "starboard_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(f"Starred messages will be posted in {channel.mention}."), ephemeral=True
        )

    @starboard_group.command(name="emoji", description="Set the emoji used to star messages.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(emoji="Unicode emoji or custom emoji to react with")
    @app_commands.guild_only()
    async def starboard_emoji(self, interaction: discord.Interaction, emoji: str) -> None:
        """Store the starboard emoji."""
        guild = interaction.guild
        assert guild is not None
        cleaned = emoji.strip()
        if not cleaned or len(cleaned) > 100:
            await interaction.response.send_message(
                embed=error_embed("Please provide a single emoji."), ephemeral=True
            )
            return
        await database.db.set(guild.id, "starboard_emoji", cleaned)
        await interaction.response.send_message(
            embed=success_embed(f"Messages now need {cleaned} reactions to be starred."), ephemeral=True
        )

    @starboard_group.command(
        name="threshold", description="Set how many reactions are needed to star a message."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(count="Number of reactions between 1 and 100")
    @app_commands.guild_only()
    async def starboard_threshold(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]
    ) -> None:
        """Store the star threshold."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "starboard_threshold", int(count))
        await interaction.response.send_message(
            embed=success_embed(f"Messages now need **{count}** star reactions."), ephemeral=True
        )

    @starboard_group.command(name="toggle", description="Enable or disable the starboard.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(enabled="Whether starred messages are posted")
    @app_commands.guild_only()
    async def starboard_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the starboard listener."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "starboard_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"The starboard is now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    @starboard_group.command(
        name="ignore", description="Add, remove or list channels ignored by the starboard."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        action="What to do with the channel", channel="Channel to ignore (not needed for list)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
            app_commands.Choice(name="List", value="list"),
        ]
    )
    @app_commands.guild_only()
    async def starboard_ignore(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Manage the ignored channel list."""
        guild = interaction.guild
        assert guild is not None
        current = await _ignored_channels(guild.id)
        if action.value == "list":
            text = ", ".join(f"<#{cid}>" for cid in current) or "No channel is ignored."
            await interaction.response.send_message(
                embed=info_embed(text, title="Ignored channels"), ephemeral=True
            )
            return
        if channel is None:
            await interaction.response.send_message(
                embed=error_embed("Please provide a channel for this action."), ephemeral=True
            )
            return
        if action.value == "add":
            if channel.id not in current:
                current.append(channel.id)
            message = f"{channel.mention} is now ignored by the starboard."
        else:
            current = [cid for cid in current if cid != channel.id]
            message = f"{channel.mention} is no longer ignored by the starboard."
        await database.db.set(guild.id, "starboard_ignore_channels", current)
        await interaction.response.send_message(embed=success_embed(message), ephemeral=True)

    @starboard_group.command(name="selfstar", description="Allow members to star their own messages.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(enabled="Whether the author's own star counts")
    @app_commands.guild_only()
    async def starboard_selfstar(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle self-stars."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "starboard_self_star", enabled)
        await interaction.response.send_message(
            embed=success_embed(
                f"Self-stars are now **{'counted' if enabled else 'ignored'}**."
            ),
            ephemeral=True,
        )

    @starboard_group.command(name="show", description="Force a message onto the starboard.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(message="Message link or message id to star")
    @app_commands.guild_only()
    async def starboard_show(self, interaction: discord.Interaction, message: str) -> None:
        """Force-post a message to the starboard."""
        guild = interaction.guild
        assert guild is not None
        channel = await _star_channel(guild)
        if channel is None:
            await interaction.response.send_message(
                embed=error_embed("Please configure the starboard channel first (`/starboard channel`)."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        channel_id, message_id = _parse_message_ref(message)
        if message_id is None:
            await interaction.followup.send(
                embed=error_embed("That does not look like a message link or id."), ephemeral=True
            )
            return
        target = await self._resolve_message(guild, channel_id, message_id)
        if target is None:
            await interaction.followup.send(
                embed=error_embed("I could not find that message. Try a full message link."), ephemeral=True
            )
            return
        emoji = str(await database.db.get(guild.id, "starboard_emoji", "⭐") or "⭐")
        threshold = await database.db.get_int(
            guild.id, "starboard_threshold", config.STARBOARD_DEFAULT_COUNT
        )
        self_star = await database.db.get_bool(guild.id, "starboard_self_star", False)
        count = max(threshold, await _star_count(target, emoji, self_star=self_star))
        star_id = await self._add_star(guild, target, count, emoji)
        if star_id is None:
            await interaction.followup.send(
                embed=error_embed("I could not post that message to the starboard."), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=success_embed(f"Message starred in {channel.mention} with **{count}** stars."),
            ephemeral=True,
        )

    @starboard_group.command(name="remove", description="Remove a message from the starboard.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(message_id="Id (or link) of the starred message or of the starboard entry")
    @app_commands.guild_only()
    async def starboard_remove(self, interaction: discord.Interaction, message_id: str) -> None:
        """Unstar a message and delete its starboard entry."""
        guild = interaction.guild
        assert guild is not None
        _, parsed = _parse_message_ref(message_id)
        if parsed is None:
            await interaction.response.send_message(
                embed=error_embed("That does not look like a message id."), ephemeral=True
            )
            return
        row = await database.db.fetchone(
            "SELECT * FROM starboard_messages "
            "WHERE guild_id = ? AND (original_id = ? OR star_message_id = ?)",
            (guild.id, parsed, parsed),
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("That message is not on the starboard."), ephemeral=True
            )
            return
        await self._remove_star(guild, int(row["original_id"]))
        await interaction.response.send_message(
            embed=success_embed("The message was removed from the starboard."), ephemeral=True
        )

    @starboard_group.command(name="config", description="Show the current starboard configuration.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def starboard_config(self, interaction: discord.Interaction) -> None:
        """Display the starboard settings."""
        guild = interaction.guild
        assert guild is not None
        channel_id = await database.db.get_int(guild.id, "starboard_channel", 0)
        emoji = str(await database.db.get(guild.id, "starboard_emoji", "⭐") or "⭐")
        threshold = await database.db.get_int(
            guild.id, "starboard_threshold", config.STARBOARD_DEFAULT_COUNT
        )
        enabled = await database.db.get_bool(guild.id, "starboard_enabled", False)
        self_star = await database.db.get_bool(guild.id, "starboard_self_star", False)
        ignore_bots = await database.db.get_bool(guild.id, "starboard_ignore_bots", True)
        ignored = await _ignored_channels(guild.id)
        row = await database.db.fetchone(
            "SELECT COUNT(*) AS total FROM starboard_messages WHERE guild_id = ?", (guild.id,)
        )
        embed = base_embed(
            title="⭐ Starboard settings",
            fields=[
                ("Enabled", "Yes" if enabled else "No", True),
                ("Channel", f"<#{channel_id}>" if channel_id else "Not set", True),
                ("Emoji", emoji, True),
                ("Threshold", str(threshold), True),
                ("Self-stars", "Counted" if self_star else "Ignored", True),
                ("Ignore bots", "Yes" if ignore_bots else "No", True),
                ("Starred messages", str(row["total"] if row else 0), True),
                ("Ignored channels", ", ".join(f"<#{cid}>" for cid in ignored) or "None", False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- internals -------------------------------------------------------------
    async def _resolve_message(
        self, guild: discord.Guild, channel_id: Optional[int], message_id: int
    ) -> Optional[discord.Message]:
        """Fetch a message from a known channel or by scanning text channels."""
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                return None
            try:
                return await channel.fetch_message(message_id)
            except (discord.HTTPException, discord.Forbidden):
                return None
        for channel in guild.text_channels[:50]:
            try:
                return await channel.fetch_message(message_id)
            except (discord.HTTPException, discord.Forbidden):
                continue
        return None


async def setup(bot: commands.Bot) -> None:
    """Load the starboard cog."""
    await bot.add_cog(Starboard(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart (none here)."""
    return []
