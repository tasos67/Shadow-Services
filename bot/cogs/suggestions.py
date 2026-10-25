"""Suggestion box: public posts with up/down voting, persistent vote buttons
and staff review commands (approve / deny / implement / delete / edit)."""
from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import parse_id, truncate
from utils.paginator import paginate, paginate_embeds

log = logging.getLogger("cogs.suggestions")

STATUS_COLORS: dict[str, discord.Color] = {
    "pending": config.INFO_COLOR,
    "approved": config.SUCCESS_COLOR,
    "denied": config.ERROR_COLOR,
    "implemented": config.THEME_COLOR,
}

STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "approved": "Approved",
    "denied": "Denied",
    "implemented": "Implemented",
}

STATUS_EMOJIS: dict[str, str] = {
    "pending": "🕓",
    "approved": "✅",
    "denied": "❌",
    "implemented": "🚀",
}


# ---------------------------------------------------------------------------
# Settings helpers (shared with the /setup wizard)
# ---------------------------------------------------------------------------
def _parse_yes_no(text: str) -> Optional[bool]:
    """Parse a yes/no wizard answer into a bool (None when not provided)."""
    value = text.strip().lower()
    if not value:
        return None
    if value in {"y", "yes", "true", "1", "on", "enabled"}:
        return True
    if value in {"n", "no", "false", "0", "off", "disabled"}:
        return False
    return None


async def _apply_suggestion_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save suggestion settings from the /setup wizard."""
    lines: list[str] = []
    channel_id = parse_id(values.get("suggestion_channel", ""))
    if channel_id:
        await database.db.set(guild.id, "suggestions_channel", channel_id)
        await database.db.set(guild.id, "suggestions_enabled", True)
        lines.append(f"Suggestion channel: <#{channel_id}> (suggestions enabled)")
    staff_channel_id = parse_id(values.get("staff_channel", ""))
    if staff_channel_id:
        await database.db.set(guild.id, "suggestions_staff_channel", staff_channel_id)
        lines.append(f"Staff channel: <#{staff_channel_id}>")
    upvote = values.get("upvote", "").strip()
    if upvote:
        await database.db.set(guild.id, "suggestions_upvote", upvote)
        lines.append(f"Upvote emoji: {upvote}")
    downvote = values.get("downvote", "").strip()
    if downvote:
        await database.db.set(guild.id, "suggestions_downvote", downvote)
        lines.append(f"Downvote emoji: {downvote}")
    dm_author = _parse_yes_no(values.get("dm_author", ""))
    if dm_author is not None:
        await database.db.set(guild.id, "suggestions_dm_author", dm_author)
        lines.append(f"DM author: **{'enabled' if dm_author else 'disabled'}**")
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="suggestions",
        label="Suggestions",
        emoji="💡",
        description="Suggestion channel, staff channel, vote emojis and DMs.",
        fields=[
            ("Suggestion channel", "#suggestions", True, "suggestion_channel"),
            ("Staff channel", "#staff-suggestions (optional)", False, "staff_channel"),
            ("Upvote emoji", "👍", False, "upvote"),
            ("Downvote emoji", "👎", False, "downvote"),
            ("DM author (yes/no)", "yes", False, "dm_author"),
        ],
        handler=_apply_suggestion_settings,
    )
)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
async def _get_suggestion(guild_id: int, suggestion_id: int) -> Optional[dict]:
    """Load one suggestion of a guild."""
    row = await database.db.fetchone(
        "SELECT * FROM suggestions WHERE id = ? AND guild_id = ?", (suggestion_id, guild_id)
    )
    return dict(row) if row else None


async def _get_suggestion_by_message(message_id: int) -> Optional[dict]:
    """Load the suggestion that owns a message id."""
    row = await database.db.fetchone("SELECT * FROM suggestions WHERE message_id = ?", (message_id,))
    return dict(row) if row else None


async def _vote_counts(suggestion_id: int) -> tuple[int, int]:
    """Return (upvotes, downvotes) for a suggestion."""
    row = await database.db.fetchone(
        "SELECT COALESCE(SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END), 0) AS up, "
        "COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) AS down "
        "FROM suggestion_votes WHERE suggestion_id = ?",
        (suggestion_id,),
    )
    if row is None:
        return 0, 0
    return int(row["up"]), int(row["down"])


async def _author_of(bot: commands.Bot, user_id: int) -> Optional[discord.abc.User]:
    """Resolve an author for embed attribution, tolerating unknown users."""
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except (discord.HTTPException, discord.Forbidden):
        return None


async def _build_suggestion_embed(
    bot: commands.Bot, row: dict, *, up: int, down: int
) -> discord.Embed:
    """Build the live embed of a suggestion (votes + status)."""
    status = str(row.get("status") or "pending")
    author = await _author_of(bot, int(row["author_id"]))
    embed = base_embed(
        title=f"💡 Suggestion #{row['id']}",
        description=truncate(str(row["content"]), 4000),
        color=STATUS_COLORS.get(status, config.INFO_COLOR),
        author=author,
    )
    label = STATUS_LABELS.get(status, status.title())
    embed.add_field(name="Status", value=f"{STATUS_EMOJIS.get(status, '')} {label}", inline=True)
    embed.add_field(
        name="Votes",
        value=f"👍 **{up}** • 👎 **{down}**\nNet score: **{up - down:+d}**",
        inline=True,
    )
    embed.add_field(name="ID", value=str(row["id"]), inline=True)
    note = row.get("staff_note")
    if note:
        embed.add_field(name="Staff note", value=truncate(str(note), 1000), inline=False)
    handled_by = row.get("handled_by")
    if handled_by:
        embed.add_field(name="Handled by", value=f"<@{int(handled_by)}>", inline=True)
    created = int(row.get("created_at") or 0)
    if created:
        embed.timestamp = datetime.datetime.fromtimestamp(created, tz=datetime.timezone.utc)
    return embed


# ---------------------------------------------------------------------------
# Persistent vote buttons
# ---------------------------------------------------------------------------
class _SuggestionVoteButton(discord.ui.Button):
    """One vote button (up or down) of a suggestion."""

    def __init__(self, value: int, emoji: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id="suggestion:up" if value == 1 else "suggestion:down",
        )
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SuggestionVoteView)
        await view.handle_vote(interaction, self.value)


class SuggestionVoteView(discord.ui.View):
    """Persistent up/down vote buttons under every suggestion."""

    def __init__(self, *, up_emoji: str = "👍", down_emoji: str = "👎") -> None:
        super().__init__(timeout=None)
        self.add_item(_SuggestionVoteButton(1, up_emoji, "Upvote", discord.ButtonStyle.success))
        self.add_item(_SuggestionVoteButton(-1, down_emoji, "Downvote", discord.ButtonStyle.danger))

    async def handle_vote(self, interaction: discord.Interaction, value: int) -> None:
        """Record (or remove) one user's vote and refresh the embed."""
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(
                embed=error_embed("Voting only works inside a server."), ephemeral=True
            )
            return
        row = await _get_suggestion_by_message(interaction.message.id)
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("This suggestion no longer exists."), ephemeral=True
            )
            return
        existing = await database.db.fetchone(
            "SELECT vote FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
            (row["id"], interaction.user.id),
        )
        if existing is not None and int(existing["vote"]) == value:
            await database.db.execute(
                "DELETE FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                (row["id"], interaction.user.id),
            )
            removed = True
        else:
            await database.db.execute(
                "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?) "
                "ON CONFLICT(suggestion_id, user_id) DO UPDATE SET vote = excluded.vote",
                (row["id"], interaction.user.id, value),
            )
            removed = False
        up, down = await _vote_counts(row["id"])
        embed = await _build_suggestion_embed(interaction.client, row, up=up, down=down)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except (discord.HTTPException, discord.Forbidden):
            log.warning("Could not refresh suggestion %s after a vote", row["id"])
            return
        if removed:
            await interaction.followup.send(
                embed=info_embed("Your vote was removed.", title="Vote"), ephemeral=True
            )


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Suggestions(commands.Cog):
    """Suggestion box commands."""

    suggestion_group = app_commands.Group(
        name="suggestion",
        description="Staff tools for reviewing suggestions.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- /suggest --------------------------------------------------------------
    @app_commands.command(name="suggest", description="Submit a suggestion to this server.")
    @app_commands.describe(content="Your suggestion")
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, content: str) -> None:
        """Post a suggestion with vote buttons."""
        guild = interaction.guild
        assert guild is not None
        enabled = await database.db.get_bool(guild.id, "suggestions_enabled", False)
        channel_id = await database.db.get_int(guild.id, "suggestions_channel", 0)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not enabled or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed(
                    "Suggestions are not configured yet. Ask an administrator to run "
                    "`/suggestion setup` (or `/setup`)."
                ),
                ephemeral=True,
            )
            return
        text = content.strip()
        if not text:
            await interaction.response.send_message(
                embed=error_embed("Your suggestion cannot be empty."), ephemeral=True
            )
            return
        now = int(time.time())
        suggestion_id = await database.db.execute(
            "INSERT INTO suggestions "
            "(guild_id, message_id, channel_id, author_id, content, status, created_at) "
            "VALUES (?, 0, ?, ?, ?, 'pending', ?)",
            (guild.id, channel.id, interaction.user.id, truncate(text, 4000), now),
        )
        row = {
            "id": suggestion_id,
            "guild_id": guild.id,
            "message_id": 0,
            "channel_id": channel.id,
            "author_id": interaction.user.id,
            "content": truncate(text, 4000),
            "status": "pending",
            "staff_note": None,
            "created_at": now,
            "handled_by": None,
        }
        up_emoji = str(await database.db.get(guild.id, "suggestions_upvote", "👍") or "👍")
        down_emoji = str(await database.db.get(guild.id, "suggestions_downvote", "👎") or "👎")
        embed = await _build_suggestion_embed(self.bot, row, up=0, down=0)
        view = SuggestionVoteView(up_emoji=up_emoji, down_emoji=down_emoji)
        try:
            message = await channel.send(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden):
            await database.db.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
            log.warning("Could not post suggestion %s in channel %s", suggestion_id, channel.id)
            await interaction.response.send_message(
                embed=error_embed("I could not post your suggestion there. Check my permissions."),
                ephemeral=True,
            )
            return
        await database.db.execute(
            "UPDATE suggestions SET message_id = ? WHERE id = ?", (message.id, suggestion_id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Your suggestion **#{suggestion_id}** was posted in {channel.mention}."),
            ephemeral=True,
        )
        if await database.db.get_bool(guild.id, "suggestions_dm_author", True):
            try:
                await interaction.user.send(
                    embed=info_embed(
                        f"Your suggestion **#{suggestion_id}** was submitted to **{guild.name}**.",
                        title="Suggestion submitted",
                    )
                )
            except (discord.HTTPException, discord.Forbidden):
                pass

    # -- staff review ----------------------------------------------------------
    @suggestion_group.command(name="approve", description="Mark a suggestion as approved.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(id="Suggestion id", note="Optional note shown with the decision")
    @app_commands.guild_only()
    async def suggestion_approve(
        self, interaction: discord.Interaction, id: int, note: Optional[str] = None
    ) -> None:
        """Approve a suggestion."""
        await self._apply_status(interaction, id, "approved", note)

    @suggestion_group.command(name="deny", description="Mark a suggestion as denied.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(id="Suggestion id", note="Optional note shown with the decision")
    @app_commands.guild_only()
    async def suggestion_deny(
        self, interaction: discord.Interaction, id: int, note: Optional[str] = None
    ) -> None:
        """Deny a suggestion."""
        await self._apply_status(interaction, id, "denied", note)

    @suggestion_group.command(name="implement", description="Mark a suggestion as implemented.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(id="Suggestion id", note="Optional note shown with the decision")
    @app_commands.guild_only()
    async def suggestion_implement(
        self, interaction: discord.Interaction, id: int, note: Optional[str] = None
    ) -> None:
        """Mark a suggestion as implemented."""
        await self._apply_status(interaction, id, "implemented", note)

    @suggestion_group.command(name="edit", description="Change the text of a suggestion.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(id="Suggestion id", content="New suggestion text")
    @app_commands.guild_only()
    async def suggestion_edit(
        self, interaction: discord.Interaction, id: int, content: str
    ) -> None:
        """Edit a suggestion's content and its message."""
        guild = interaction.guild
        assert guild is not None
        row = await _get_suggestion(guild.id, id)
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find suggestion `#{id}`."), ephemeral=True
            )
            return
        await database.db.execute(
            "UPDATE suggestions SET content = ? WHERE id = ?", (truncate(content, 4000), id)
        )
        row["content"] = truncate(content, 4000)
        await self._refresh_message(guild, row)
        await interaction.response.send_message(
            embed=success_embed(f"Suggestion **#{id}** updated."), ephemeral=True
        )

    @suggestion_group.command(name="delete", description="Delete a suggestion and its message.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(id="Suggestion id")
    @app_commands.guild_only()
    async def suggestion_delete(self, interaction: discord.Interaction, id: int) -> None:
        """Remove a suggestion from the database and the channel."""
        guild = interaction.guild
        assert guild is not None
        row = await _get_suggestion(guild.id, id)
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find suggestion `#{id}`."), ephemeral=True
            )
            return
        await database.db.execute("DELETE FROM suggestion_votes WHERE suggestion_id = ?", (id,))
        await database.db.execute("DELETE FROM suggestions WHERE id = ?", (id,))
        channel = guild.get_channel(int(row["channel_id"]))
        if isinstance(channel, discord.TextChannel) and row["message_id"]:
            try:
                message = await channel.fetch_message(int(row["message_id"]))
                await message.delete()
            except (discord.HTTPException, discord.Forbidden):
                pass
        await interaction.response.send_message(
            embed=success_embed(f"Suggestion **#{id}** deleted."), ephemeral=True
        )

    @suggestion_group.command(name="list", description="List the suggestions of this server.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(status="Only show suggestions with this status")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Pending", value="pending"),
            app_commands.Choice(name="Approved", value="approved"),
            app_commands.Choice(name="Denied", value="denied"),
            app_commands.Choice(name="Implemented", value="implemented"),
        ]
    )
    @app_commands.guild_only()
    async def suggestion_list(
        self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None
    ) -> None:
        """Paginated list of suggestions."""
        guild = interaction.guild
        assert guild is not None
        sql = "SELECT * FROM suggestions WHERE guild_id = ?"
        params: list[int | str] = [guild.id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY id DESC"
        rows = await database.db.fetchall(sql, params)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No suggestions found."), ephemeral=True
            )
            return

        def builder(row) -> tuple[str, str]:
            state = str(row["status"])
            jump = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
            return (
                f"#{row['id']} • {STATUS_LABELS.get(state, state.title())}",
                f"{truncate(str(row['content']), 200)}\n[open message]({jump})",
            )

        pages = paginate_embeds(
            rows,
            title="💡 Suggestions",
            description_builder=builder,
            per_page=5,
            footer_extra=f"{len(rows)} suggestion(s)",
        )
        await paginate(interaction, pages)

    @suggestion_group.command(name="config", description="Show the current suggestion configuration.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def suggestion_config(self, interaction: discord.Interaction) -> None:
        """Display the suggestion settings."""
        guild = interaction.guild
        assert guild is not None
        channel_id = await database.db.get_int(guild.id, "suggestions_channel", 0)
        staff_id = await database.db.get_int(guild.id, "suggestions_staff_channel", 0)
        up_emoji = str(await database.db.get(guild.id, "suggestions_upvote", "👍") or "👍")
        down_emoji = str(await database.db.get(guild.id, "suggestions_downvote", "👎") or "👎")
        enabled = await database.db.get_bool(guild.id, "suggestions_enabled", False)
        dm_author = await database.db.get_bool(guild.id, "suggestions_dm_author", True)
        counts = await database.db.fetchall(
            "SELECT status, COUNT(*) AS total FROM suggestions WHERE guild_id = ? GROUP BY status",
            (guild.id,),
        )
        summary_lines = [
            f"{STATUS_LABELS.get(str(r['status']), r['status'])}: {r['total']}" for r in counts
        ]
        summary = ", ".join(summary_lines)
        embed = base_embed(
            title="💡 Suggestion settings",
            fields=[
                ("Enabled", "Yes" if enabled else "No", True),
                ("Channel", f"<#{channel_id}>" if channel_id else "Not set", True),
                ("Staff channel", f"<#{staff_id}>" if staff_id else "Not set", True),
                ("Upvote emoji", up_emoji, True),
                ("Downvote emoji", down_emoji, True),
                ("DM author", "Yes" if dm_author else "No", True),
                ("Suggestions", summary or "None yet", False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @suggestion_group.command(name="setup", description="Set the suggestion and staff channels.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        channel="Channel where suggestions are posted",
        staff_channel="Channel where staff decisions are posted (optional)",
    )
    @app_commands.guild_only()
    async def suggestion_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        staff_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Configure the suggestion channels and enable the feature."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "suggestions_channel", channel.id)
        await database.db.set(guild.id, "suggestions_enabled", True)
        lines = [f"Suggestion channel: {channel.mention}"]
        if staff_channel is not None:
            await database.db.set(guild.id, "suggestions_staff_channel", staff_channel.id)
            lines.append(f"Staff channel: {staff_channel.mention}")
        await interaction.response.send_message(
            embed=success_embed("\n".join(lines) + "\nSuggestions are now **enabled**."),
            ephemeral=True,
        )

    # -- internals -------------------------------------------------------------
    async def _apply_status(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
        status: str,
        note: Optional[str],
    ) -> None:
        """Set a suggestion's status, refresh its message and notify everyone."""
        guild = interaction.guild
        assert guild is not None
        row = await _get_suggestion(guild.id, suggestion_id)
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find suggestion `#{suggestion_id}`."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        row["status"] = status
        row["staff_note"] = note
        row["handled_by"] = interaction.user.id
        await database.db.execute(
            "UPDATE suggestions SET status = ?, staff_note = ?, handled_by = ? WHERE id = ?",
            (status, note, interaction.user.id, suggestion_id),
        )
        await self._refresh_message(guild, row)
        await self._post_decision(guild, row, interaction.user, note)
        if await database.db.get_bool(guild.id, "suggestions_dm_author", True):
            author = await _author_of(self.bot, int(row["author_id"]))
            if author is not None:
                try:
                    await author.send(
                        embed=info_embed(
                            f"Your suggestion **#{suggestion_id}** in **{guild.name}** was "
                            f"**{STATUS_LABELS.get(status, status)}**"
                            + (f".\nStaff note: {truncate(note, 500)}" if note else "."),
                            title="Suggestion update",
                        )
                    )
                except (discord.HTTPException, discord.Forbidden):
                    pass
        await interaction.followup.send(
            embed=success_embed(
                f"Suggestion **#{suggestion_id}** is now **{STATUS_LABELS.get(status, status)}**."
            ),
            ephemeral=True,
        )

    async def _refresh_message(self, guild: discord.Guild, row: dict) -> None:
        """Re-render the suggestion message embed with fresh vote counts."""
        channel = guild.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or not row["message_id"]:
            return
        try:
            message = await channel.fetch_message(int(row["message_id"]))
        except (discord.HTTPException, discord.Forbidden):
            return
        up, down = await _vote_counts(int(row["id"]))
        try:
            await message.edit(embed=await _build_suggestion_embed(self.bot, row, up=up, down=down))
        except (discord.HTTPException, discord.Forbidden):
            log.warning("Could not refresh suggestion message %s", row["message_id"])

    async def _post_decision(
        self, guild: discord.Guild, row: dict, staff: discord.abc.User, note: Optional[str]
    ) -> None:
        """Post the staff decision to the configured staff channel."""
        staff_channel_id = await database.db.get_int(guild.id, "suggestions_staff_channel", 0)
        channel = guild.get_channel(staff_channel_id) if staff_channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        status = str(row["status"])
        jump = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
        embed = base_embed(
            title=f"{STATUS_EMOJIS.get(status, '💡')} Suggestion #{row['id']} — "
            f"{STATUS_LABELS.get(status, status.title())}",
            description=truncate(str(row["content"]), 500),
            color=STATUS_COLORS.get(status, config.INFO_COLOR),
            fields=[
                ("Author", f"<@{row['author_id']}>", True),
                ("Handled by", staff.mention, True),
                ("Note", truncate(note, 500) if note else "—", False),
                ("Message", f"[jump]({jump})", False),
            ],
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, discord.Forbidden):
            log.warning("Could not post the decision for suggestion %s", row["id"])


async def setup(bot: commands.Bot) -> None:
    """Load the suggestions cog."""
    await bot.add_cog(Suggestions(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [SuggestionVoteView()]
