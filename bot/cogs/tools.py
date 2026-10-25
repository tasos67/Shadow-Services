"""Tools: personal reminders, persistent polls and an AFK system.

* ``/remind`` stores reminders that a background loop delivers (DM first,
  then a configurable channel fallback).
* ``/poll`` posts polls with persistent voting buttons and live results.
* ``/afk`` keeps an away status that answers mentions in the guild.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import format_ts, parse_duration, parse_id, remaining, truncate
from utils.paginator import paginate
from utils.views import ConfirmView

log = logging.getLogger("cogs.tools")

POLL_BAR_LENGTH = 20
MAX_POLL_OPTIONS = 10


# ---------------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------------
async def _get_poll(message_id: int) -> Optional[dict]:
    """Return the poll row for a message id, if any."""
    row = await database.db.fetchone("SELECT * FROM polls WHERE message_id = ?", (message_id,))
    return dict(row) if row else None


def _poll_options(row: dict) -> list[str]:
    """Decode the JSON list of poll option labels."""
    try:
        data = json.loads(row["options"])
        return [str(item) for item in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _poll_counts(message_id: int) -> dict[int, int]:
    """Return ``{choice_index: vote_count}`` for a poll."""
    rows = await database.db.fetchall(
        "SELECT choice, COUNT(*) AS c FROM poll_votes WHERE message_id = ? GROUP BY choice",
        (message_id,),
    )
    return {int(row["choice"]): int(row["c"]) for row in rows}


def _vote_bar(count: int, total: int, length: int = POLL_BAR_LENGTH) -> str:
    """Build a proportional text bar for one poll option."""
    filled = 0 if total <= 0 else round(count / total * length)
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)


def _poll_embed(row: dict, options: list[str], counts: dict[int, int], *, closed: bool) -> discord.Embed:
    """Render the live (or final) embed for a poll."""
    total = sum(counts.values())
    lines: list[str] = []
    for index, option in enumerate(options):
        count = counts.get(index, 0)
        percent = f"{count / total * 100:.0f}%" if total else "0%"
        lines.append(
            f"**{index + 1}.** {truncate(option, 80)}\n`{_vote_bar(count, total)}` {count} ({percent})"
        )
    embed = base_embed(
        title="📊 Poll — closed" if closed else "📊 Poll",
        description=f"**{truncate(str(row['question']), 200)}**\n\n" + ("\n".join(lines) or "No options."),
        color=config.INFO_COLOR,
    )
    embed.add_field(name="Total votes", value=str(total), inline=True)
    if closed:
        embed.add_field(name="Status", value="Closed", inline=True)
    else:
        embed.add_field(name="Ends", value=f"{format_ts(int(row['ends_at']))} ({remaining(row['ends_at'])})", inline=True)
    embed.set_footer(text=f"Poll ID: {row['message_id']}")
    return embed


def _winning_options(options: list[str], counts: dict[int, int]) -> list[int]:
    """Return the indexes of the option(s) with the highest vote count."""
    if not counts:
        return []
    top = max(counts.values())
    if top <= 0:
        return []
    return [index for index in range(len(options)) if counts.get(index, 0) == top]


async def _close_poll(bot: commands.Bot, poll: dict) -> None:
    """Mark a poll as closed, show final results and announce the winner."""
    options = _poll_options(poll)
    await database.db.execute("UPDATE polls SET closed = 1 WHERE message_id = ?", (poll["message_id"],))
    counts = await _poll_counts(int(poll["message_id"]))

    guild = bot.get_guild(int(poll["guild_id"]))
    channel = guild.get_channel(int(poll["channel_id"])) if guild else None
    message: Optional[discord.Message] = None
    if isinstance(channel, discord.TextChannel):
        try:
            message = await channel.fetch_message(int(poll["message_id"]))
        except discord.HTTPException:
            message = None
    if message is not None:
        try:
            await message.edit(embed=_poll_embed({**poll, "closed": 1}, options, counts, closed=True), view=None)
        except discord.HTTPException:
            log.warning("Could not edit closed poll %s", poll["message_id"])

    if not isinstance(channel, discord.TextChannel):
        return
    winners = _winning_options(options, counts)
    if winners:
        names = ", ".join(f"**{truncate(options[index], 80)}**" for index in winners)
        text = (
            f"📊 The poll **{truncate(str(poll['question']), 120)}** has ended!\n"
            f"Winning option: {names} with {counts.get(winners[0], 0)} vote(s)."
        )
    else:
        text = f"📊 The poll **{truncate(str(poll['question']), 120)}** has ended with no votes."
    try:
        await channel.send(embed=base_embed(description=text))
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Persistent poll voting buttons
# ---------------------------------------------------------------------------
class PollVoteButton(discord.ui.Button):
    """One numbered poll option (fixed custom id so it survives restarts)."""

    def __init__(self, index: int) -> None:
        super().__init__(label=str(index + 1), style=discord.ButtonStyle.primary, custom_id=f"poll:{index}")
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.message is None:
            await interaction.response.send_message(
                embed=error_embed("This poll is no longer available."), ephemeral=True
            )
            return
        poll = await _get_poll(interaction.message.id)
        if poll is None:
            await interaction.response.send_message(embed=error_embed("This poll no longer exists."), ephemeral=True)
            return
        options = _poll_options(poll)
        if self.index >= len(options):
            await interaction.response.send_message(
                embed=error_embed("That option is not part of this poll."), ephemeral=True
            )
            return
        if poll["closed"]:
            await interaction.response.send_message(embed=error_embed("This poll is closed."), ephemeral=True)
            return
        await database.db.execute(
            "INSERT INTO poll_votes (message_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET choice = excluded.choice",
            (poll["message_id"], interaction.user.id, self.index),
        )
        counts = await _poll_counts(int(poll["message_id"]))
        await interaction.response.send_message(
            embed=success_embed(f"Your vote for **{truncate(options[self.index], 80)}** was recorded."),
            ephemeral=True,
        )
        try:
            await interaction.message.edit(embed=_poll_embed(poll, options, counts, closed=False))
        except discord.HTTPException:
            log.warning("Could not refresh poll %s", poll["message_id"])


class PollView(discord.ui.View):
    """Persistent voting view holding up to ten numbered option buttons."""

    def __init__(self, count: int = MAX_POLL_OPTIONS) -> None:
        super().__init__(timeout=None)
        for index in range(min(count, MAX_POLL_OPTIONS)):
            self.add_item(PollVoteButton(index))


# ---------------------------------------------------------------------------
# /setup wizard step
# ---------------------------------------------------------------------------
async def _apply_tools_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save the tools settings coming from the setup wizard / modal."""
    lines: list[str] = []
    afk_raw = values.get("afk_enabled", "").strip().lower()
    if afk_raw:
        enabled = afk_raw in {"yes", "y", "true", "1", "on", "enable", "enabled"}
        await database.db.set(guild.id, "afk_enabled", enabled)
        lines.append(f"AFK system: **{'enabled' if enabled else 'disabled'}**")
    channel_id = parse_id(values.get("reminder_channel", ""))
    if channel_id:
        await database.db.set(guild.id, "reminder_channel", channel_id)
        lines.append(f"Reminder fallback channel: <#{channel_id}>")
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the tools step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="tools",
            label="Tools",
            emoji="🛠️",
            description="Reminders, polls and the AFK system.",
            fields=[
                ("Enable AFK system (yes/no)", "yes", False, "afk_enabled"),
                ("Reminder DM fallback channel", "#general (optional)", False, "reminder_channel"),
            ],
            handler=_apply_tools_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Tools(commands.Cog):
    """Reminders, polls and AFK status."""

    remind_group = app_commands.Group(name="remind", description="Personal reminders.", guild_only=True)
    poll_group = app_commands.Group(name="poll", description="Create and manage polls.", guild_only=True)
    afk_group = app_commands.Group(name="afk", description="Away-from-keyboard status.", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.poll_loop.cancel()

    # -- reminders -----------------------------------------------------------
    @remind_group.command(name="set", description="Set a reminder that will be sent to you later.")
    @app_commands.describe(duration="When to remind you, e.g. 10m, 2h, 1d", text="What to remind you about")
    async def remind_set(
        self,
        interaction: discord.Interaction,
        duration: str,
        text: str,
    ) -> None:
        """Store a personal reminder."""
        guild = interaction.guild
        assert guild is not None
        seconds = parse_duration(duration)
        if not seconds or seconds <= 0:
            await interaction.response.send_message(
                embed=error_embed("Please give a valid duration such as `10m`, `2h` or `1d`."), ephemeral=True
            )
            return
        remind_at = int(time.time()) + seconds
        await database.db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, remind_at, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.user.id, guild.id, interaction.channel_id, remind_at, truncate(text, 1500), int(time.time())),
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"I will remind you {format_ts(remind_at, 'R')} ({format_ts(remind_at, 'f')}).\n\n"
                f"**Reminder:** {truncate(text, 500)}"
            ),
            ephemeral=True,
        )

    @remind_group.command(name="list", description="List your pending reminders.")
    async def remind_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of the invoker's own reminders."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM reminders WHERE user_id = ? AND guild_id = ? ORDER BY remind_at",
            (interaction.user.id, guild.id),
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("You have no pending reminders."), ephemeral=True
            )
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="⏰ Your reminders")
            for row in rows[start : start + 5]:
                embed.add_field(
                    name=f"#{row['id']} • {format_ts(int(row['remind_at']), 'R')}",
                    value=truncate(row["text"], 200),
                    inline=False,
                )
            embed.set_footer(text=f"{len(rows)} reminder(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    @remind_group.command(name="cancel", description="Cancel one of your reminders.")
    @app_commands.describe(reminder_id="The reminder id shown by /remind list")
    async def remind_cancel(self, interaction: discord.Interaction, reminder_id: int) -> None:
        """Delete a reminder that belongs to the invoker."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ? AND guild_id = ?",
            (reminder_id, interaction.user.id, guild.id),
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("I could not find that reminder (it must be your own)."), ephemeral=True
            )
            return
        await database.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await interaction.response.send_message(
            embed=success_embed(f"Reminder **#{reminder_id}** was cancelled."), ephemeral=True
        )

    @remind_group.command(name="clear", description="Cancel every one of your reminders.")
    async def remind_clear(self, interaction: discord.Interaction) -> None:
        """Delete all of the invoker's reminders after a confirmation."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ? AND guild_id = ?",
            (interaction.user.id, guild.id),
        )
        count = int(row["c"]) if row else 0
        if count == 0:
            await interaction.response.send_message(
                embed=info_embed("You have no pending reminders."), ephemeral=True
            )
            return

        async def _confirm() -> None:
            await database.db.execute(
                "DELETE FROM reminders WHERE user_id = ? AND guild_id = ?", (interaction.user.id, guild.id)
            )
            await interaction.followup.send(
                embed=success_embed(f"Cancelled **{count}** reminder(s)."), ephemeral=True
            )

        view = ConfirmView(user_id=interaction.user.id, on_confirm=_confirm, confirm_label="Clear all")
        await interaction.response.send_message(
            embed=warn_embed(f"This will cancel all **{count}** of your reminders. Continue?"),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    # -- polls ---------------------------------------------------------------
    @poll_group.command(name="create", description="Create a poll with 2-10 options.")
    @app_commands.describe(
        question="The poll question",
        duration="How long the poll runs, e.g. 10m, 1h, 1d",
        options="The options, separated by a pipe character: Option A | Option B | Option C",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: str,
        duration: str,
        options: str,
    ) -> None:
        """Post a poll with persistent voting buttons."""
        guild = interaction.guild
        assert guild is not None
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Polls can only be created in a text channel."), ephemeral=True
            )
            return
        seconds = parse_duration(duration)
        if not seconds or seconds < 10:
            await interaction.response.send_message(
                embed=error_embed("Please give a duration of at least 10 seconds, e.g. `10m`, `1h`, `1d`."),
                ephemeral=True,
            )
            return
        parsed = [part.strip() for part in options.split("|")]
        parsed = [part for part in parsed if part]
        if not 2 <= len(parsed) <= MAX_POLL_OPTIONS:
            await interaction.response.send_message(
                embed=error_embed(
                    f"Provide between **2** and **{MAX_POLL_OPTIONS}** options separated by `|`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        ends_at = int(time.time()) + seconds
        placeholder = {"message_id": 0, "question": question, "ends_at": ends_at}
        try:
            message = await channel.send(
                embed=_poll_embed(placeholder, parsed, {}, closed=False), view=PollView(len(parsed))
            )
        except discord.HTTPException:
            await interaction.followup.send(
                embed=error_embed("I could not post the poll there. Check my permissions."), ephemeral=True
            )
            return
        await database.db.execute(
            "INSERT INTO polls (message_id, guild_id, channel_id, question, options, ends_at, closed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (message.id, guild.id, channel.id, truncate(question, 250), json.dumps(parsed), ends_at),
        )
        poll = await _get_poll(message.id)
        if poll is not None:
            try:
                await message.edit(embed=_poll_embed(poll, parsed, {}, closed=False))
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            embed=success_embed(f"Poll posted in {channel.mention} — it ends {format_ts(ends_at, 'R')}."),
            ephemeral=True,
        )

    @poll_group.command(name="end", description="Close a poll early and show its final results.")
    @app_commands.describe(message_id="The poll message id (shown as 'Poll ID' in its footer)")
    @app_commands.default_permissions(manage_messages=True)
    async def poll_end(self, interaction: discord.Interaction, message_id: str) -> None:
        """Close a running poll immediately."""
        guild = interaction.guild
        assert guild is not None
        poll_id = parse_id(message_id)
        poll = await _get_poll(poll_id) if poll_id else None
        if poll is None or int(poll["guild_id"]) != guild.id:
            await interaction.response.send_message(embed=error_embed("Poll not found."), ephemeral=True)
            return
        if poll["closed"]:
            await interaction.response.send_message(
                embed=error_embed("That poll is already closed."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _close_poll(self.bot, poll)
        await interaction.followup.send(embed=success_embed("Poll closed and results announced."), ephemeral=True)

    @poll_group.command(name="list", description="List the polls that are still running.")
    async def poll_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of open polls."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM polls WHERE guild_id = ? AND closed = 0 ORDER BY ends_at", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(embed=info_embed("There are no open polls."), ephemeral=True)
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="📊 Open polls")
            for row in rows[start : start + 5]:
                counts = await _poll_counts(int(row["message_id"]))
                embed.add_field(
                    name=f"#{row['message_id']} • {format_ts(int(row['ends_at']), 'R')}",
                    value=(
                        f"{truncate(row['question'], 120)}\n"
                        f"Votes: {sum(counts.values())}\n"
                        f"[jump](https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']})"
                    ),
                    inline=False,
                )
            pages.append(embed)
        await paginate(interaction, pages)

    # -- AFK -----------------------------------------------------------------
    @afk_group.command(name="set", description="Set an AFK status with an optional reason.")
    @app_commands.describe(reason="Why you are away (optional)")
    async def afk_set(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        """Mark the invoker as AFK."""
        guild = interaction.guild
        assert guild is not None
        if not await database.db.get_bool(guild.id, "afk_enabled", True):
            await interaction.response.send_message(
                embed=error_embed("The AFK system is disabled on this server."), ephemeral=True
            )
            return
        reason_text = truncate(reason.strip(), 200) if reason and reason.strip() else "AFK"
        await database.db.execute(
            "INSERT INTO afk (guild_id, user_id, reason, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET reason = excluded.reason, set_at = excluded.set_at",
            (guild.id, interaction.user.id, reason_text, int(time.time())),
        )
        await interaction.response.send_message(
            embed=success_embed(f"You are now AFK: {reason_text}"), ephemeral=True
        )

    @afk_group.command(name="clear", description="Remove your AFK status.")
    async def afk_clear(self, interaction: discord.Interaction) -> None:
        """Clear the invoker's AFK status."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT 1 FROM afk WHERE guild_id = ? AND user_id = ?", (guild.id, interaction.user.id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=info_embed("You are not AFK."), ephemeral=True
            )
            return
        await database.db.execute(
            "DELETE FROM afk WHERE guild_id = ? AND user_id = ?", (guild.id, interaction.user.id)
        )
        await interaction.response.send_message(embed=success_embed("Your AFK status was removed."), ephemeral=True)

    @afk_group.command(name="list", description="List everyone who is currently AFK.")
    @app_commands.default_permissions(manage_messages=True)
    async def afk_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of AFK members."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM afk WHERE guild_id = ? ORDER BY set_at", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(embed=info_embed("Nobody is AFK right now."), ephemeral=True)
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="💤 AFK members")
            for row in rows[start : start + 5]:
                embed.add_field(
                    name=f"<@{row['user_id']}> • {format_ts(int(row['set_at']), 'R')}",
                    value=truncate(row["reason"], 200),
                    inline=False,
                )
            embed.set_footer(text=f"{len(rows)} AFK member(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    # -- listeners & background tasks ----------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle AFK removal and mention replies."""
        try:
            await self._handle_afk(message)
        except Exception:
            log.exception("AFK listener failed")

    async def _handle_afk(self, message: discord.Message) -> None:
        """Delete AFK rows on activity and answer mentions of AFK members."""
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not await database.db.get_bool(guild.id, "afk_enabled", True):
            return

        author_row = await database.db.fetchone(
            "SELECT * FROM afk WHERE guild_id = ? AND user_id = ?", (guild.id, message.author.id)
        )
        if author_row is not None:
            await database.db.execute(
                "DELETE FROM afk WHERE guild_id = ? AND user_id = ?", (guild.id, message.author.id)
            )
            try:
                await message.channel.send(
                    embed=info_embed(f"Welcome back {message.author.mention}! Your AFK status was removed."),
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        for user in message.mentions:
            if user.bot or user.id == message.author.id:
                continue
            row = await database.db.fetchone(
                "SELECT * FROM afk WHERE guild_id = ? AND user_id = ?", (guild.id, user.id)
            )
            if row is None:
                continue
            try:
                await message.reply(
                    embed=info_embed(
                        f"{user.mention} is AFK: {truncate(row['reason'], 200)}\n"
                        f"Since {format_ts(int(row['set_at']), 'R')}."
                    ),
                    delete_after=8,
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the reminder and poll loops after a restart."""
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()
        if not self.poll_loop.is_running():
            self.poll_loop.start()

    @tasks.loop(seconds=30)
    async def reminder_loop(self) -> None:
        """Deliver every reminder whose time has come."""
        rows = await database.db.fetchall(
            "SELECT * FROM reminders WHERE remind_at <= ?", (int(time.time()),)
        )
        for row in rows:
            try:
                await self._deliver_reminder(dict(row))
            except Exception:
                log.exception("Failed to deliver reminder %s", row["id"])

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_reminder(self, row: dict) -> None:
        """DM a reminder, falling back to the channel, then delete the row."""
        embed = base_embed(title="⏰ Reminder", description=truncate(row["text"], 4000), color=config.INFO_COLOR)
        jump = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}"
        embed.add_field(name="Origin", value=f"[jump to the channel]({jump})", inline=False)

        user: Optional[discord.User] = self.bot.get_user(int(row["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(row["user_id"]))
            except discord.HTTPException:
                user = None

        delivered = False
        if user is not None:
            try:
                await user.send(embed=embed)
                delivered = True
            except discord.HTTPException:
                delivered = False

        if not delivered:
            fallback_id = await database.db.get(int(row["guild_id"]), "reminder_channel") or row["channel_id"]
            guild = self.bot.get_guild(int(row["guild_id"]))
            channel = guild.get_channel(int(fallback_id)) if guild else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        content=f"<@{row['user_id']}>",
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.HTTPException:
                    log.warning("Could not deliver reminder %s in a channel", row["id"])

        await database.db.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))

    @tasks.loop(seconds=30)
    async def poll_loop(self) -> None:
        """Close polls whose end time has passed."""
        rows = await database.db.fetchall(
            "SELECT * FROM polls WHERE closed = 0 AND ends_at <= ?", (int(time.time()),)
        )
        for row in rows:
            try:
                await _close_poll(self.bot, dict(row))
            except Exception:
                log.exception("Failed to close poll %s", row["message_id"])

    @poll_loop.before_loop
    async def before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the tools cog."""
    await bot.add_cog(Tools(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [PollView(MAX_POLL_OPTIONS)]
