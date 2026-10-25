"""Leveling system: XP from messages and voice time, level-up roles,
announcements, rank cards and a leaderboard.

XP math is delegated to :mod:`utils.helpers` (``level_from_xp`` /
``xp_for_level``) so the curve stays consistent across the whole bot.
"""
from __future__ import annotations

import io
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.core import SetupCategory, register_setup_category
from database import db
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import (
    level_from_xp,
    parse_id,
    placeholder_format,
    truncate,
    xp_for_level,
)
from utils.paginator import Paginator
from utils.rankcard import render_rank_card

log = logging.getLogger("cogs.leveling")

DEFAULT_LEVEL_MESSAGE = "🎉 {user} reached **level {level}**!"
LEADERBOARD_PAGE_SIZE = 10
IGNORED_PREFIXES = ("?", "!", ".")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
async def _roles_config(guild_id: int) -> list[dict]:
    """Return the configured level-up roles as clean ``{level, role_id}`` dicts."""
    raw = await db.get(guild_id, "level_roles", [])
    entries: list[dict] = []
    for item in raw or []:
        try:
            entries.append({"level": int(item["level"]), "role_id": int(item["role_id"])})
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def _progress_bar(current: int, total: int, width: int = 20) -> str:
    """Return a unicode progress bar for the given values."""
    total = max(1, total)
    filled = max(0, min(width, int(width * (current / total))))
    return "█" * filled + "░" * (width - filled)


async def _apply_level_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save leveling settings from the /setup wizard and return a summary."""
    lines: list[str] = []

    enable_raw = values.get("enable", "").strip().lower()
    if enable_raw:
        enabled = enable_raw in {"y", "yes", "true", "1", "on", "enable", "enabled"}
        await db.set(guild.id, "level_enabled", enabled)
        lines.append(f"Leveling: **{'enabled' if enabled else 'disabled'}**")

    channel_id = parse_id(values.get("channel", ""))
    if channel_id:
        await db.set(guild.id, "level_announce_channel", channel_id)
        lines.append(f"Level-up channel: <#{channel_id}>")

    multiplier_raw = values.get("multiplier", "").strip().replace(",", ".")
    if multiplier_raw:
        try:
            multiplier = float(multiplier_raw)
        except ValueError:
            multiplier = 0.0
        if 0.1 <= multiplier <= 10.0:
            await db.set(guild.id, "level_multiplier", multiplier)
            lines.append(f"XP multiplier: **x{multiplier:g}**")

    voice_raw = values.get("voice_xp", "").strip()
    if voice_raw.isdigit():
        await db.set(guild.id, "level_voice_xp", int(voice_raw))
        lines.append(f"Voice XP: **{int(voice_raw)}** per interval")

    message = values.get("message", "").strip()
    if message:
        await db.set(guild.id, "level_message", truncate(message, 1500))
        lines.append("Announcement message saved.")

    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="leveling",
        label="Leveling",
        emoji="📈",
        description="Enable leveling, announcement channel, multiplier and message.",
        fields=[
            ("Enable leveling (yes/no)", "yes", True, "enable"),
            ("Level-up channel", "#level-ups (optional)", False, "channel"),
            ("XP multiplier", "1.0", False, "multiplier"),
            ("Voice XP per interval", "10", False, "voice_xp"),
            ("Announcement message", "{user} reached level {level}!", False, "message"),
        ],
        handler=_apply_level_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Leveling(commands.Cog):
    """Leveling: message & voice XP, level roles, rank cards and leaderboards."""

    level_group = app_commands.Group(
        name="level", description="Leveling configuration.", guild_only=True
    )
    role_group = app_commands.Group(
        name="role", description="Manage the roles granted on level-up.", parent=level_group
    )
    ignore_group = app_commands.Group(
        name="ignore", description="Channels and roles that never earn XP.", parent=level_group
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user_id -> last voice XP award (time.time()) to enforce the interval
        self._voice_last: dict[int, float] = {}

    def cog_unload(self) -> None:
        self.voice_loop.cancel()

    # -- XP granting ---------------------------------------------------------
    async def _grant_xp(
        self, guild: discord.Guild, member: discord.Member, amount: int, *, voice: bool
    ) -> None:
        """Add ``amount`` XP (message or voice) and handle level-ups."""
        if amount <= 0:
            return
        now = int(time.time())
        row = await db.fetchone(
            "SELECT xp FROM xp WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
        )
        current = int(row["xp"]) if row else 0
        old_level = level_from_xp(current)
        new_xp = current + amount
        new_level = level_from_xp(new_xp)
        if voice:
            await db.execute(
                "INSERT INTO xp (guild_id, user_id, xp, level, messages, voice_xp, last_msg) "
                "VALUES (?, ?, ?, ?, 0, ?, 0) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, "
                "level = excluded.level, voice_xp = voice_xp + excluded.voice_xp",
                (guild.id, member.id, new_xp, new_level, amount),
            )
        else:
            await db.execute(
                "INSERT INTO xp (guild_id, user_id, xp, level, messages, voice_xp, last_msg) "
                "VALUES (?, ?, ?, ?, 1, 0, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, "
                "level = excluded.level, messages = messages + 1, last_msg = excluded.last_msg",
                (guild.id, member.id, new_xp, new_level, now),
            )
        if new_level > old_level:
            await self._apply_level_roles(guild, member, new_level)
            await self._announce_level_up(guild, member, new_level)

    async def _apply_level_roles(self, guild: discord.Guild, member: discord.Member, level: int) -> None:
        """Grant (and optionally clean up) the roles configured for a level."""
        entries = await _roles_config(guild.id)
        if not entries or guild.me is None:
            return
        resolved: list[tuple[int, discord.Role]] = []
        for entry in entries:
            role = guild.get_role(entry["role_id"])
            if role is None:
                continue
            try:
                assignable = role.is_assignable()
            except AttributeError:
                assignable = False
            if assignable:
                resolved.append((entry["level"], role))
        if not resolved:
            return
        resolved.sort(key=lambda item: item[0])
        stack = await db.get_bool(guild.id, "level_stack_roles", False)
        matching = [(req, role) for req, role in resolved if level >= req]
        to_grant: set[discord.Role] = set()
        to_remove: set[discord.Role] = set()
        if stack:
            to_grant = {role for _, role in matching}
            to_remove = {role for _, role in resolved if role not in to_grant and role in member.roles}
        elif matching:
            best = matching[-1][0]
            to_grant = {role for req, role in matching if req == best}
            to_remove = {role for req, role in resolved if req < best and role in member.roles}
        add = [role for role in to_grant if role not in member.roles]
        remove = [role for role in to_remove if role in member.roles]
        try:
            if add:
                await member.add_roles(*add, reason="Level-up roles", atomic=True)
            if remove:
                await member.remove_roles(*remove, reason="Level-up roles", atomic=True)
        except (discord.HTTPException, discord.Forbidden):
            log.exception("Could not update level roles for %s in guild %s", member.id, guild.id)

    async def _announce_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        level: int,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Post the configured level-up message (announcement channel or origin)."""
        if not await db.get_bool(guild.id, "level_announce", True):
            return
        template = await db.get(guild.id, "level_message", DEFAULT_LEVEL_MESSAGE)
        text = placeholder_format(str(template), member=member, guild=guild, extra={"level": str(level)})
        destination: Optional[discord.TextChannel] = None
        channel_id = await db.get(guild.id, "level_announce_channel")
        if channel_id:
            candidate = guild.get_channel(int(channel_id))
            if isinstance(candidate, discord.TextChannel):
                destination = candidate
        destination = destination or channel
        if destination is None:
            return
        embed = base_embed(
            title="📈 Level up!",
            description=truncate(text, 1500),
            color=config.SUCCESS_COLOR,
            thumbnail=member.display_avatar.url,
        )
        try:
            await destination.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.exception("Could not announce level-up for %s in guild %s", member.id, guild.id)

    async def _rank_position(self, guild_id: int, xp: int) -> int:
        """Return the 1-based leaderboard position for a total XP amount."""
        row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM xp WHERE guild_id = ? AND xp > ?", (guild_id, xp)
        )
        return int(row["c"]) + 1 if row else 1

    # -- message listener ----------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Award message XP, honouring cooldowns and the ignore lists."""
        try:
            await self._handle_message(message)
        except (discord.HTTPException, discord.Forbidden):
            log.exception("HTTP error while awarding message XP")
        except Exception:
            log.exception("Unexpected error while awarding message XP")

    async def _handle_message(self, message: discord.Message) -> None:
        """Filter a message and grant XP when it qualifies."""
        guild = message.guild
        author = message.author
        if guild is None or author.bot or not isinstance(author, discord.Member):
            return
        if not await db.get_bool(guild.id, "level_enabled", True):
            return
        ignore_channels = await db.get(guild.id, "level_ignore_channels", []) or []
        if message.channel.id in ignore_channels:
            return
        ignore_roles = await db.get(guild.id, "level_ignore_roles", []) or []
        if any(role.id in ignore_roles for role in author.roles):
            return
        content = (message.content or "").strip()
        if not content or content.startswith(IGNORED_PREFIXES) or content.startswith("<@"):
            return

        cooldown = await db.get_int(guild.id, "level_cooldown", config.DEFAULT_XP_COOLDOWN)
        now = int(time.time())
        if cooldown > 0:
            row = await db.fetchone(
                "SELECT last_msg FROM xp WHERE guild_id = ? AND user_id = ?", (guild.id, author.id)
            )
            last = int(row["last_msg"]) if row else 0
            if now - last < cooldown:
                return

        xp_min = await db.get_int(guild.id, "level_xp_min", config.DEFAULT_XP_MIN)
        xp_max = await db.get_int(guild.id, "level_xp_max", config.DEFAULT_XP_MAX)
        if xp_min > xp_max:
            xp_min, xp_max = xp_max, xp_min
        xp_min = max(1, xp_min)
        xp_max = max(xp_min, xp_max)
        multiplier = float(await db.get(guild.id, "level_multiplier", 1.0) or 1.0)
        amount = max(1, int(round(random.randint(xp_min, xp_max) * multiplier)))
        await self._grant_xp(guild, author, amount, voice=False)

    # -- voice background task ----------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the voice XP task after (re)connects."""
        if not self.voice_loop.is_running():
            self.voice_loop.start()

    @tasks.loop(seconds=60)
    async def voice_loop(self) -> None:
        """Grant voice XP to eligible members, at most once per interval."""
        now = time.time()
        for guild in list(self.bot.guilds):
            try:
                await self._process_voice_guild(guild, now)
            except (discord.HTTPException, discord.Forbidden):
                log.exception("HTTP error while awarding voice XP in guild %s", guild.id)
            except Exception:
                log.exception("Unexpected error while awarding voice XP in guild %s", guild.id)

    @voice_loop.before_loop
    async def before_voice_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_voice_guild(self, guild: discord.Guild, now: float) -> None:
        """Award voice XP for one guild (skips ignored, deafened, alone and AFK)."""
        if not await db.get_bool(guild.id, "level_enabled", True):
            return
        if not await db.get_bool(guild.id, "level_voice_enabled", True):
            return
        amount = await db.get_int(guild.id, "level_voice_xp", 10)
        if amount <= 0:
            return
        interval = await db.get_int(guild.id, "level_voice_interval", config.DEFAULT_VOICE_XP_INTERVAL)
        interval = max(1, interval)
        ignore_channels = set(await db.get(guild.id, "level_ignore_channels", []) or [])
        ignore_roles = set(await db.get(guild.id, "level_ignore_roles", []) or [])
        afk_id = guild.afk_channel.id if guild.afk_channel else None
        for channel in guild.voice_channels:
            if channel.id == afk_id or channel.id in ignore_channels:
                continue
            members = list(channel.members)
            if len(members) < 2:  # nobody to talk to
                continue
            for member in members:
                if member.bot:
                    continue
                if any(role.id in ignore_roles for role in member.roles):
                    continue
                state = member.voice
                if state is None or state.deaf or state.self_deaf:
                    continue
                if now - self._voice_last.get(member.id, 0.0) < interval:
                    continue
                self._voice_last[member.id] = now
                await self._grant_xp(guild, member, amount, voice=True)

    # -- /rank ---------------------------------------------------------------
    @app_commands.command(name="rank", description="Show your rank card with level and XP progress.")
    @app_commands.describe(member="Whose rank card to show (defaults to you)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        """Render a rank card. XP is computed with level_from_xp / xp_for_level."""
        guild = interaction.guild
        assert guild is not None
        target = member or interaction.user
        assert isinstance(target, discord.Member)
        row = await db.fetchone(
            "SELECT xp, messages FROM xp WHERE guild_id = ? AND user_id = ?", (guild.id, target.id)
        )
        total_xp = int(row["xp"]) if row else 0
        messages = int(row["messages"]) if row else 0
        level = level_from_xp(total_xp)
        base = xp_for_level(level)
        need = xp_for_level(level + 1) - base
        into = max(0, total_xp - base)
        position = await self._rank_position(guild.id, total_xp)
        try:
            avatar = await target.display_avatar.read()
        except discord.HTTPException:
            avatar = None
        card: Optional[bytes] = None
        try:
            card = await render_rank_card(avatar, target.display_name, level, position, into, need)
        except Exception:
            log.exception("Rank card rendering failed for %s", target.id)
            card = None
        embed = base_embed(
            title=f"📈 {target.display_name}'s rank",
            description=(
                f"**Rank:** #{position}   •   **Level:** {level}\n"
                f"**Total XP:** {total_xp:,}   •   **Messages:** {messages:,}\n"
                f"`{_progress_bar(into, need)}`  {into:,}/{need:,} XP"
            ),
            color=config.THEME_COLOR,
            thumbnail=target.display_avatar.url,
        )
        kwargs: dict = {"embed": embed}
        if card is not None:
            embed.set_image(url="attachment://rank.png")
            kwargs["file"] = discord.File(io.BytesIO(card), filename="rank.png")
        await interaction.response.send_message(**kwargs)

    # -- /leaderboard --------------------------------------------------------
    @app_commands.command(name="leaderboard", description="Show the XP leaderboard for this server.")
    @app_commands.describe(page="Page to show (starts at 1)")
    @app_commands.guild_only()
    async def leaderboard(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 10000] = 1
    ) -> None:
        """Paginated leaderboard over the ``xp`` table (ordered by XP DESC)."""
        guild = interaction.guild
        assert guild is not None
        rows = await db.fetchall(
            "SELECT user_id, xp, level FROM xp WHERE guild_id = ? ORDER BY xp DESC", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No one has earned XP yet."), ephemeral=True
            )
            return
        pages = self._leaderboard_pages(rows)
        page = min(max(1, page), len(pages))
        if len(pages) > 1:
            view = Paginator(pages, owner_id=interaction.user.id)
            view.page_label.label = f"{page}/{len(pages)}"
            view.index = page - 1
            await interaction.response.send_message(embed=pages[page - 1], view=view)
        else:
            await interaction.response.send_message(embed=pages[0])

    def _leaderboard_pages(self, rows: list) -> list[discord.Embed]:
        """Build the (medal) leaderboard pages."""
        medals = ("🥇", "🥈", "🥉")
        pages: list[discord.Embed] = []
        total_pages = (len(rows) + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE
        for start in range(0, len(rows), LEADERBOARD_PAGE_SIZE):
            chunk = rows[start : start + LEADERBOARD_PAGE_SIZE]
            lines: list[str] = []
            for offset, row in enumerate(chunk):
                position = start + offset + 1
                badge = medals[position - 1] if position <= 3 else f"`#{position}`"
                lines.append(
                    f"{badge} <@{row['user_id']}> — level **{int(row['level'])}** • "
                    f"**{int(row['xp']):,}** XP"
                )
            embed = base_embed(title="📈 XP leaderboard", description="\n".join(lines))
            embed.set_footer(text=f"Page {start // LEADERBOARD_PAGE_SIZE + 1}/{total_pages}")
            pages.append(embed)
        return pages

    # -- /level enable -------------------------------------------------------
    @level_group.command(name="enable", description="Enable or disable message XP.")
    @app_commands.describe(enabled="Whether members earn XP from messages")
    @app_commands.default_permissions(manage_guild=True)
    async def level_enable(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the master leveling switch."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "level_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Leveling is now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- /level message ------------------------------------------------------
    @level_group.command(name="message", description="Set the level-up announcement message.")
    @app_commands.describe(message="Template using {user}, {level}, {server} and {membercount}")
    @app_commands.default_permissions(manage_guild=True)
    async def level_message(self, interaction: discord.Interaction, message: str) -> None:
        """Store the level-up message template."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "level_message", truncate(message, 1500))
        await interaction.response.send_message(
            embed=success_embed(f"Level-up message saved:\n{truncate(message, 400)}"), ephemeral=True
        )

    # -- /level channel ------------------------------------------------------
    @level_group.command(name="channel", description="Set (or clear) the level-up announcement channel.")
    @app_commands.describe(channel="Channel for level-up messages (leave empty to use the same channel)")
    @app_commands.default_permissions(manage_guild=True)
    async def level_channel(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Route level-up announcements to a channel, or back to where XP was earned."""
        assert interaction.guild is not None
        if channel is None:
            await db.delete(interaction.guild.id, "level_announce_channel")
            text = "Level-up messages will be posted in the channel where the XP was earned."
        else:
            await db.set(interaction.guild.id, "level_announce_channel", channel.id)
            text = f"Level-up messages will be posted in {channel.mention}."
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    # -- /level multiplier ---------------------------------------------------
    @level_group.command(name="multiplier", description="Set the global XP multiplier.")
    @app_commands.describe(value="Multiplier applied to every XP grant (0.1 - 10.0)")
    @app_commands.default_permissions(manage_guild=True)
    async def level_multiplier(
        self, interaction: discord.Interaction, value: app_commands.Range[float, 0.1, 10.0]
    ) -> None:
        """Scale all XP gains."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "level_multiplier", float(value))
        await interaction.response.send_message(
            embed=success_embed(f"XP multiplier set to **x{float(value):g}**."), ephemeral=True
        )

    # -- /level xp -----------------------------------------------------------
    @level_group.command(name="xp", description="Set the random XP range granted per message.")
    @app_commands.describe(min_xp="Minimum XP (1 - 1000)", max_xp="Maximum XP (1 - 1000)")
    @app_commands.default_permissions(manage_guild=True)
    async def level_xp(
        self,
        interaction: discord.Interaction,
        min_xp: app_commands.Range[int, 1, 1000],
        max_xp: app_commands.Range[int, 1, 1000],
    ) -> None:
        """Configure the per-message XP range."""
        assert interaction.guild is not None
        if min_xp > max_xp:
            await interaction.response.send_message(
                embed=error_embed("The minimum XP must not be greater than the maximum."), ephemeral=True
            )
            return
        await db.set(interaction.guild.id, "level_xp_min", int(min_xp))
        await db.set(interaction.guild.id, "level_xp_max", int(max_xp))
        await interaction.response.send_message(
            embed=success_embed(f"Members now earn between **{min_xp}** and **{max_xp}** XP per message."),
            ephemeral=True,
        )

    # -- /level cooldown -----------------------------------------------------
    @level_group.command(name="cooldown", description="Set the cooldown between XP grants.")
    @app_commands.describe(seconds="Seconds between XP grants (0 disables the cooldown)")
    @app_commands.default_permissions(manage_guild=True)
    async def level_cooldown(
        self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 3600]
    ) -> None:
        """Configure the per-user message cooldown."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "level_cooldown", int(seconds))
        text = (
            f"XP cooldown set to **{int(seconds)} seconds**."
            if seconds
            else "XP cooldown disabled — members earn XP on every message."
        )
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    # -- /level voice --------------------------------------------------------
    @level_group.command(name="voice", description="Configure voice channel XP.")
    @app_commands.describe(
        enabled="Whether members earn XP for sitting in voice channels",
        xp="XP granted per interval",
        interval="Seconds of voice time required per XP grant",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def level_voice(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        xp: Optional[app_commands.Range[int, 0, 100000]] = None,
        interval: Optional[app_commands.Range[int, 10, 604800]] = None,
    ) -> None:
        """Toggle voice XP and optionally tweak its amount and interval."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        await db.set(guild_id, "level_voice_enabled", enabled)
        if xp is not None:
            await db.set(guild_id, "level_voice_xp", int(xp))
        if interval is not None:
            await db.set(guild_id, "level_voice_interval", int(interval))
        current_xp = await db.get_int(guild_id, "level_voice_xp", 10)
        current_interval = await db.get_int(
            guild_id, "level_voice_interval", config.DEFAULT_VOICE_XP_INTERVAL
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"Voice XP **{'enabled' if enabled else 'disabled'}** — "
                f"{current_xp} XP every {current_interval}s."
            ),
            ephemeral=True,
        )

    # -- /level role add -----------------------------------------------------
    @role_group.command(name="add", description="Grant a role when a member reaches a level.")
    @app_commands.describe(level="Level required to receive the role", role="Role to grant")
    @app_commands.default_permissions(manage_guild=True)
    async def role_add(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 1000],
        role: discord.Role,
    ) -> None:
        """Add or replace the role configured for a level."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        entries = await _roles_config(guild_id)
        remaining = [entry for entry in entries if entry["level"] != int(level)]
        if len(remaining) >= config.DEFAULT_LEVEL_UP_ROLE_CAP:
            await interaction.response.send_message(
                embed=error_embed(
                    f"You can configure at most **{config.DEFAULT_LEVEL_UP_ROLE_CAP}** level roles."
                ),
                ephemeral=True,
            )
            return
        remaining.append({"level": int(level), "role_id": role.id})
        remaining.sort(key=lambda item: item["level"])
        await db.set(guild_id, "level_roles", remaining)
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} will be granted at **level {int(level)}**."),
            ephemeral=True,
        )

    # -- /level role remove --------------------------------------------------
    @role_group.command(name="remove", description="Remove the role configured for a level.")
    @app_commands.describe(level="Level whose role should be removed")
    @app_commands.default_permissions(manage_guild=True)
    async def role_remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 1000]) -> None:
        """Delete a level-up role entry."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        entries = await _roles_config(guild_id)
        remaining = [entry for entry in entries if entry["level"] != int(level)]
        if len(remaining) == len(entries):
            await interaction.response.send_message(
                embed=info_embed(f"There is no role configured for level **{int(level)}**."), ephemeral=True
            )
            return
        await db.set(guild_id, "level_roles", remaining)
        await interaction.response.send_message(
            embed=success_embed(f"Removed the level **{int(level)}** role."), ephemeral=True
        )

    # -- /level role list ----------------------------------------------------
    @role_group.command(name="list", description="List every configured level-up role.")
    async def role_list(self, interaction: discord.Interaction) -> None:
        """Show the configured level-up roles."""
        guild = interaction.guild
        assert guild is not None
        entries = await _roles_config(guild.id)
        if not entries:
            await interaction.response.send_message(
                embed=info_embed("No level-up roles are configured yet."), ephemeral=True
            )
            return
        lines = []
        for entry in entries:
            role = guild.get_role(entry["role_id"])
            lines.append(f"**Level {entry['level']}** — {role.mention if role else '*(deleted role)*'}")
        stack = await db.get_bool(guild.id, "level_stack_roles", False)
        embed = base_embed(
            title="📈 Level-up roles",
            description="\n".join(lines),
            fields=[("Stacking", "Enabled" if stack else "Disabled (highest only)", True)],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- /level ignore channel ----------------------------------------------
    @ignore_group.command(name="channel", description="Ignore or un-ignore a channel for XP.")
    @app_commands.describe(channel="Channel to ignore", remove="Set to true to stop ignoring it")
    @app_commands.default_permissions(manage_guild=True)
    async def ignore_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel, remove: bool = False
    ) -> None:
        """Manage the ignored channels list."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        current = [int(cid) for cid in await db.get(guild_id, "level_ignore_channels", []) or []]
        if remove:
            current = [cid for cid in current if cid != channel.id]
            text = f"{channel.mention} will now earn XP again."
        else:
            if channel.id not in current:
                current.append(channel.id)
            text = f"{channel.mention} will no longer earn XP."
        await db.set(guild_id, "level_ignore_channels", current)
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    # -- /level ignore role --------------------------------------------------
    @ignore_group.command(name="role", description="Ignore or un-ignore a role for XP.")
    @app_commands.describe(role="Role to ignore", remove="Set to true to stop ignoring it")
    @app_commands.default_permissions(manage_guild=True)
    async def ignore_role(
        self, interaction: discord.Interaction, role: discord.Role, remove: bool = False
    ) -> None:
        """Manage the ignored roles list."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        current = [int(rid) for rid in await db.get(guild_id, "level_ignore_roles", []) or []]
        if remove:
            current = [rid for rid in current if rid != role.id]
            text = f"Members with {role.mention} will now earn XP again."
        else:
            if role.id not in current:
                current.append(role.id)
            text = f"Members with {role.mention} will no longer earn XP."
        await db.set(guild_id, "level_ignore_roles", current)
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    # -- /level stackroles ---------------------------------------------------
    @level_group.command(name="stackroles", description="Keep every lower level role instead of only the highest.")
    @app_commands.describe(enabled="True to keep all earned level roles, false for the highest only")
    @app_commands.default_permissions(manage_guild=True)
    async def level_stackroles(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle whether level roles stack."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "level_stack_roles", enabled)
        text = (
            "Members will keep every level role they have earned."
            if enabled
            else "Members will only keep the highest level role they have earned."
        )
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    # -- /level reset --------------------------------------------------------
    @level_group.command(name="reset", description="Reset a member's level and XP.")
    @app_commands.describe(member="Member whose progress should be wiped")
    @app_commands.default_permissions(manage_guild=True)
    async def level_reset(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Delete a member's XP row."""
        assert interaction.guild is not None
        await db.execute(
            "DELETE FROM xp WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Reset the level and XP of {member.mention}."), ephemeral=True
        )

    # -- /level set ----------------------------------------------------------
    @level_group.command(name="set", description="Set a member's level.")
    @app_commands.describe(member="Member to update", level="Level to set (XP is set to match)")
    @app_commands.default_permissions(manage_guild=True)
    async def level_set(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        level: app_commands.Range[int, 0, 1000],
    ) -> None:
        """Set a level directly. XP is derived from xp_for_level() for consistency."""
        assert interaction.guild is not None
        target_xp = xp_for_level(int(level))
        await db.execute(
            "INSERT INTO xp (guild_id, user_id, xp, level, messages, voice_xp, last_msg) "
            "VALUES (?, ?, ?, ?, 0, 0, 0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
            (interaction.guild.id, member.id, target_xp, int(level)),
        )
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} is now **level {int(level)}**."), ephemeral=True
        )

    # -- /level give ---------------------------------------------------------
    @level_group.command(name="give", description="Give a member extra XP.")
    @app_commands.describe(member="Member to add XP to", amount="Amount of XP to add")
    @app_commands.default_permissions(manage_guild=True)
    async def level_give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 10000000],
    ) -> None:
        """Add XP to a member. The new level is recomputed with level_from_xp()."""
        guild = interaction.guild
        assert guild is not None
        row = await db.fetchone(
            "SELECT xp FROM xp WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
        )
        current = int(row["xp"]) if row else 0
        new_xp = current + int(amount)
        new_level = level_from_xp(new_xp)
        await db.execute(
            "INSERT INTO xp (guild_id, user_id, xp, level, messages, voice_xp, last_msg) "
            "VALUES (?, ?, ?, ?, 0, 0, 0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
            (guild.id, member.id, new_xp, new_level),
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"Gave **{int(amount):,}** XP to {member.mention} — now **level {new_level}** "
                f"({new_xp:,} XP total)."
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    """Load the leveling cog."""
    await bot.add_cog(Leveling(bot))
