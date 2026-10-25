"""Ticket system: panels (buttons or select menu), private channels with
permission overwrites, modals for the reason, close/claim/add/remove/rename/
transfer controls, HTML+text transcripts, per-user limits, inactivity
auto-close and post-close ratings."""
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
from utils.helpers import parse_id, truncate
from utils.paginator import paginate
from utils.transcript import fetch_history, send_transcript
from utils.views import ConfirmView

log = logging.getLogger("cogs.tickets")

# Panel options: key -> (label, emoji)
PANEL_OPTIONS: dict[str, tuple[str, str]] = {
    "support": ("Support", "🎫"),
    "report": ("Report", "🛡️"),
    "purchase": ("Purchase", "🛍️"),
    "other": ("Other", "💬"),
}

# channel_id -> (guild_id, owner_id, last_activity) for cheap activity tracking
_CACHE: dict[int, tuple[int, int, int]] = {}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
async def _get_support_roles(guild: discord.Guild) -> list[discord.Role]:
    """Return the configured support roles (that still exist)."""
    ids: list[int] = await database.db.get(guild.id, "ticket_support_roles", [])
    return [role for rid in ids if (role := guild.get_role(rid)) is not None]


async def _log_event(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send a ticket event embed to the configured log channel."""
    channel_id = await database.db.get(guild.id, "ticket_log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def _get_ticket_by_channel(channel_id: int) -> Optional[dict]:
    """Return the ticket row for a channel, if any."""
    row = await database.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))
    return dict(row) if row else None


async def _open_ticket_count(guild_id: int, user_id: int) -> int:
    """Count a user's open tickets."""
    row = await database.db.fetchone(
        "SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND owner_id = ? AND status = 'open'",
        (guild_id, user_id),
    )
    return int(row["c"]) if row else 0


async def _next_counter(guild_id: int) -> int:
    """Increment and return the per-guild ticket counter."""
    await database.db.execute(
        "INSERT INTO ticket_counters (guild_id, counter) VALUES (?, 1) "
        "ON CONFLICT(guild_id) DO UPDATE SET counter = counter + 1",
        (guild_id,),
    )
    row = await database.db.fetchone("SELECT counter FROM ticket_counters WHERE guild_id = ?", (guild_id,))
    return int(row["counter"]) if row else 1


def _sanitize_channel_name(text: str, limit: int = 100) -> str:
    """Turn arbitrary text into a valid channel name."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:limit] or "ticket"


async def _fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """Fetch a member, tolerating leavers/unknown users."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------
async def open_ticket(interaction: discord.Interaction, option_key: str, reason: str) -> None:
    """Create a private ticket channel for the invoking member.

    Expects the interaction to already be deferred.
    """
    guild = interaction.guild
    member = interaction.user
    assert isinstance(guild, discord.Guild) and isinstance(member, discord.Member)

    limit = await database.db.get_int(guild.id, "ticket_limit", config.DEFAULT_OPEN_TICKETS_PER_USER)
    if limit > 0:
        current = await _open_ticket_count(guild.id, member.id)
        if current >= limit:
            await interaction.followup.send(
                embed=error_embed(
                    f"You already have {current} open ticket(s). Please close them before opening a new one."
                ),
                ephemeral=True,
            )
            return

    category_id = await database.db.get(guild.id, "ticket_category")
    category = guild.get_channel(int(category_id)) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    counter = await _next_counter(guild.id)
    option_label = PANEL_OPTIONS.get(option_key, (option_key.title(), "🎫"))[0]
    name = f"{counter:04d}-{_sanitize_channel_name(member.name, 50)}-{option_key}"[:100]

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, connect=True, speak=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, manage_messages=True,
            connect=True, speak=True, read_message_history=True,
        ),
    }
    for role in await _get_support_roles(guild):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, connect=True, speak=True, read_message_history=True
        )

    try:
        channel = await guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket #{counter} opened by {member} ({option_label}): {truncate(reason, 100)}",
        )
    except discord.HTTPException:
        log.exception("Failed to create ticket channel in guild %s", guild.id)
        await interaction.followup.send(
            embed=error_embed(
                "I could not create the ticket channel. Please check that I can manage channels "
                "and that the configured category exists and is reachable by me."
            ),
            ephemeral=True,
        )
        return

    now = int(time.time())
    await database.db.execute(
        "INSERT INTO tickets (guild_id, channel_id, owner_id, reason, category, status, counter, created_at, last_activity) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (guild.id, channel.id, member.id, reason, option_key, counter, now, now),
    )
    _CACHE[channel.id] = (guild.id, member.id, now)

    support_roles = await _get_support_roles(guild)
    mentions = " ".join(role.mention for role in support_roles)
    embed = base_embed(
        title=f"Ticket #{counter} — {option_label}",
        description=(
            f"Hello {member.mention}, thank you for opening a ticket!\n"
            "A member of our support team will be with you shortly.\n\n"
            f"**Reason:** {truncate(reason, 500)}"
        ),
    )
    await channel.send(
        content=mentions or None,
        embed=embed,
        view=TicketActionsView(),
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )

    await interaction.followup.send(
        embed=success_embed(f"Your ticket was created: {channel.mention}"), ephemeral=True
    )
    await _log_event(
        guild,
        base_embed(
            title="🎫 Ticket opened",
            color=config.SUCCESS_COLOR,
            fields=[
                ("Ticket", f"#{counter} ({option_label})", True),
                ("User", member.mention, True),
                ("Channel", channel.mention, True),
                ("Reason", truncate(reason, 500), False),
            ],
        ),
    )


async def close_ticket(
    channel: discord.TextChannel,
    closed_by: discord.abc.User,
    reason: str = "No reason provided",
    *,
    delete_channel: bool = True,
) -> None:
    """Close a ticket: transcript to log channel + user DM, then delete it."""
    ticket = await _get_ticket_by_channel(channel.id)
    guild = channel.guild
    if ticket is None or ticket["status"] != "open":
        return

    owner = await _fetch_member(guild, int(ticket["owner_id"]))
    log_channel_id = await database.db.get(guild.id, "ticket_log_channel")
    log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None

    history = await fetch_history(channel, limit=500)
    await send_transcript(
        channel,
        history,
        log_channel=log_channel if isinstance(log_channel, discord.TextChannel) else None,
        user=owner,
        closed_by=closed_by,
        reason=reason,
    )

    now = int(time.time())
    await database.db.execute(
        "UPDATE tickets SET status = 'closed', closed_at = ? WHERE channel_id = ?", (now, channel.id)
    )
    _CACHE.pop(channel.id, None)

    counter = ticket["counter"]
    await _log_event(
        guild,
        base_embed(
            title="🎫 Ticket closed",
            color=config.ERROR_COLOR,
            fields=[
                ("Ticket", f"#{counter}", True),
                ("Closed by", str(closed_by), True),
                ("Reason", truncate(reason, 500), False),
                ("Messages", str(len(history)), True),
            ],
        ),
    )

    if await database.db.get_bool(guild.id, "ticket_rating", True) and owner is not None:
        try:
            await owner.send(
                embed=base_embed(
                    title="⭐ Rate your experience",
                    description=(
                        f"Your ticket **#{counter}** in **{guild.name}** was closed.\n"
                        "How would you rate the support you received?"
                    ),
                ),
                view=TicketRatingView(),
            )
        except discord.HTTPException:
            pass

    if delete_channel:
        try:
            await channel.delete(reason=f"Ticket #{counter} closed by {closed_by}")
        except discord.HTTPException:
            log.exception("Could not delete ticket channel %s", channel.id)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------
class TicketReasonModal(discord.ui.Modal):
    """Ask the user why they are opening a ticket."""

    def __init__(self, option_key: str) -> None:
        label, _ = PANEL_OPTIONS.get(option_key, (option_key.title(), "🎫"))
        super().__init__(title=f"Open a {label} ticket", timeout=300)
        self.option_key = option_key
        self.reason_input = discord.ui.TextInput(
            label="Why are you opening this ticket?",
            placeholder="Describe your issue in a few words…",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await open_ticket(interaction, self.option_key, str(self.reason_input.value))


class TicketUserModal(discord.ui.Modal):
    """Collect a user to add, remove or transfer the ticket to."""

    def __init__(self, action: str) -> None:
        super().__init__(title=f"{action.title()} user", timeout=300)
        self.action = action
        self.user_input = discord.ui.TextInput(
            label="User (mention or ID)",
            placeholder="e.g. @user or 123456789012345678",
            required=True,
            max_length=100,
        )
        self.add_item(self.user_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        user_id = parse_id(str(self.user_input.value))
        member = await _fetch_member(guild, user_id) if user_id else None
        if member is None:
            await interaction.response.send_message(embed=error_embed("I could not find that member."), ephemeral=True)
            return
        if member.id == interaction.user.id and self.action != "transfer":
            await interaction.response.send_message(embed=error_embed("You cannot do this to yourself."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return

        if self.action == "add":
            await channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            )
            await interaction.response.send_message(
                embed=success_embed(f"{member.mention} was added to this ticket."), ephemeral=True
            )
            await channel.send(embed=info_embed(f"{member.mention} was added by {interaction.user.mention}."))
        elif self.action == "remove":
            await channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(
                embed=success_embed(f"{member.mention} was removed from this ticket."), ephemeral=True
            )
            await channel.send(embed=info_embed(f"{member.mention} was removed by {interaction.user.mention}."))
        else:  # transfer
            old_owner = await _fetch_member(guild, int(ticket["owner_id"]))
            await database.db.execute("UPDATE tickets SET owner_id = ? WHERE channel_id = ?", (member.id, channel.id))
            await channel.set_permissions(
                member,
                overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            )
            if old_owner is not None and old_owner.id != member.id:
                await channel.set_permissions(
                    old_owner,
                    overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                )
            _CACHE[channel.id] = (guild.id, member.id, int(time.time()))
            await interaction.response.send_message(
                embed=success_embed(f"This ticket now belongs to {member.mention}."), ephemeral=True
            )
            await channel.send(
                embed=info_embed(f"Ticket ownership transferred to {member.mention} by {interaction.user.mention}.")
            )


class TicketRenameModal(discord.ui.Modal):
    """Collect a new channel name for the ticket."""

    def __init__(self, current_name: str) -> None:
        super().__init__(title="Rename ticket", timeout=300)
        self.new_name = discord.ui.TextInput(
            label="New channel name",
            placeholder="e.g. account-issue",
            required=True,
            default=current_name[:100],
            max_length=100,
        )
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        name = _sanitize_channel_name(str(self.new_name.value))
        try:
            await channel.edit(name=name, reason=f"Ticket renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("I could not rename this channel."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=success_embed(f"This ticket is now named **#{name}**."), ephemeral=True
        )


# ---------------------------------------------------------------------------
# Views & buttons (fixed custom ids so they survive restarts)
# ---------------------------------------------------------------------------
class PanelButton(discord.ui.Button):
    """One ticket category button on the panel."""

    def __init__(self, key: str, label: Optional[str] = None, emoji: Optional[str] = None) -> None:
        default_label, default_emoji = PANEL_OPTIONS.get(key, (key.title(), "🎫"))
        super().__init__(
            label=label or default_label,
            emoji=emoji or default_emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"ticket:create:{key}",
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TicketReasonModal(self.key))


class PanelSelect(discord.ui.Select):
    """Select-menu variant of the ticket panel."""

    def __init__(self, option_keys: Optional[list[str]] = None) -> None:
        keys = list(option_keys or PANEL_OPTIONS)[:25]
        options = [
            discord.SelectOption(
                label=PANEL_OPTIONS.get(key, (key.title(), "🎫"))[0],
                emoji=PANEL_OPTIONS.get(key, (key.title(), "🎫"))[1],
                value=key,
                description=f"Open a {PANEL_OPTIONS.get(key, (key.title(), '🎫'))[0].lower()} ticket",
            )
            for key in keys
        ]
        super().__init__(
            custom_id="ticket:panel_select",
            placeholder="Choose a ticket category…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TicketReasonModal(self.values[0]))


class TicketCreateView(discord.ui.View):
    """Persistent routing view for panel buttons (all four categories)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for key in PANEL_OPTIONS:
            self.add_item(PanelButton(key))


class TicketPanelView(discord.ui.View):
    """Persistent routing view for the panel select menu."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(PanelSelect())


def build_panel_view(style: str, option_keys: list[str]) -> discord.ui.View:
    """Build the exact view sent with a panel message (same custom ids)."""
    view = discord.ui.View(timeout=None)
    if style == "select":
        view.add_item(PanelSelect(option_keys))
    else:
        for key in option_keys:
            view.add_item(PanelButton(key))
    return view


async def _claim_ticket(interaction: discord.Interaction) -> None:
    """Shared claim logic for the button and the slash command."""
    channel = interaction.channel
    guild = interaction.guild
    if guild is None or not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
        return
    ticket = await _get_ticket_by_channel(channel.id)
    if ticket is None or ticket["status"] != "open":
        await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
        return
    if ticket["claimer_id"]:
        claimer = guild.get_member(int(ticket["claimer_id"]))
        await interaction.response.send_message(
            embed=error_embed(
                f"This ticket is already claimed by {claimer.mention if claimer else 'another staff member'}."
            ),
            ephemeral=True,
        )
        return
    await database.db.execute("UPDATE tickets SET claimer_id = ? WHERE channel_id = ?", (interaction.user.id, channel.id))
    if not channel.name.startswith("claimed-"):
        try:
            await channel.edit(name="claimed-" + channel.name[:94], reason=f"Ticket claimed by {interaction.user}")
        except discord.HTTPException:
            pass
    await interaction.response.send_message(embed=success_embed(f"{interaction.user.mention} claimed this ticket."))
    await _log_event(
        guild,
        base_embed(
            title="🙋 Ticket claimed",
            color=config.WARNING_COLOR,
            fields=[("Ticket", f"#{ticket['counter']}", True), ("Claimed by", interaction.user.mention, True)],
        ),
    )


class TicketCloseButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        support_roles = await _get_support_roles(guild)
        is_staff = any(role in interaction.user.roles for role in support_roles) or interaction.user.guild_permissions.manage_messages
        if interaction.user.id != int(ticket["owner_id"]) and not is_staff:
            await interaction.response.send_message(
                embed=error_embed("Only the ticket owner or the support team can close this ticket."), ephemeral=True
            )
            return

        async def _confirm() -> None:
            await interaction.followup.send(embed=info_embed("Closing ticket and generating the transcript…"))
            await close_ticket(channel, interaction.user, "Closed from panel button")

        view = ConfirmView(user_id=interaction.user.id, on_confirm=_confirm, confirm_label="Close ticket")
        await interaction.response.send_message(
            embed=warn_embed("Are you sure you want to close this ticket? A transcript will be saved."),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


class TicketClaimButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket:claim")

    async def callback(self, interaction: discord.Interaction) -> None:
        await _claim_ticket(interaction)


class TicketAddButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Add user", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="ticket:add")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TicketUserModal("add"))


class TicketRemoveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Remove user", emoji="➖", style=discord.ButtonStyle.secondary, custom_id="ticket:remove")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TicketUserModal("remove"))


class TicketRenameButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="ticket:rename")

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        name = channel.name if isinstance(channel, discord.TextChannel) else "ticket"
        await interaction.response.send_modal(TicketRenameModal(name))


class TicketTransferButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Transfer", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="ticket:transfer")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TicketUserModal("transfer"))


class TicketActionsView(discord.ui.View):
    """Persistent control panel shown inside every ticket."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketCloseButton())
        self.add_item(TicketClaimButton())
        self.add_item(TicketAddButton())
        self.add_item(TicketRemoveButton())
        self.add_item(TicketRenameButton())
        self.add_item(TicketTransferButton())


class _RatingButton(discord.ui.Button):
    """One star of the 1-5 rating."""

    def __init__(self, value: int) -> None:
        super().__init__(
            label="⭐" * value, style=discord.ButtonStyle.secondary, custom_id=f"ticket:rate:{value}"
        )
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        row = await database.db.fetchone(
            "SELECT * FROM tickets WHERE owner_id = ? AND status = 'closed' ORDER BY closed_at DESC LIMIT 1",
            (interaction.user.id,),
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("I could not find a recently closed ticket to rate."), ephemeral=True
            )
            return
        ticket = dict(row)
        existing = await database.db.fetchone(
            "SELECT 1 FROM ticket_ratings WHERE guild_id = ? AND ticket_id = ? AND user_id = ?",
            (ticket["guild_id"], ticket["id"], interaction.user.id),
        )
        if existing:
            await interaction.response.send_message(
                embed=info_embed("You already rated this ticket — thank you!"), ephemeral=True
            )
            return
        await database.db.execute(
            "INSERT INTO ticket_ratings (guild_id, ticket_id, user_id, rating, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket["guild_id"], ticket["id"], interaction.user.id, self.value, int(time.time())),
        )
        for child in self.view.children:
            child.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(
            embed=success_embed(f"Thank you! You rated this ticket {'⭐' * self.value}."), view=self.view
        )


class TicketRatingView(discord.ui.View):
    """1-5 star rating sent to the user after a ticket closes (persistent)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for value in range(1, 6):
            self.add_item(_RatingButton(value))


# ---------------------------------------------------------------------------
# Setup modal for /ticket setup + /setup wizard
# ---------------------------------------------------------------------------
class TicketSetupModal(discord.ui.Modal):
    """Collect the four basic ticket settings."""

    def __init__(self) -> None:
        super().__init__(title="Ticket setup", timeout=300)
        self.panel_channel = discord.ui.TextInput(
            label="Panel channel", placeholder="Where the ticket panel should live, e.g. #support", required=True
        )
        self.category = discord.ui.TextInput(
            label="Ticket category", placeholder="Category where ticket channels are created", required=True
        )
        self.support_roles = discord.ui.TextInput(
            label="Support roles", placeholder="@Support @Helpers (mentions or IDs, comma separated)", required=True
        )
        self.log_channel = discord.ui.TextInput(
            label="Log channel", placeholder="Where transcripts and events are posted (optional)", required=False
        )
        for item in (self.panel_channel, self.category, self.support_roles, self.log_channel):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        summary = await _apply_ticket_settings(
            interaction.guild,
            {
                "panel_channel": str(self.panel_channel.value),
                "category": str(self.category.value),
                "support_roles": str(self.support_roles.value),
                "log_channel": str(self.log_channel.value),
            },
        )
        await interaction.response.send_message(embed=success_embed(summary, title="Tickets configured"), ephemeral=True)


async def _apply_ticket_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save the ticket settings (shared with /setup wizard)."""
    panel_channel_id = parse_id(values.get("panel_channel", ""))
    category_id = parse_id(values.get("category", ""))
    log_channel_id = parse_id(values.get("log_channel", ""))
    role_ids = [rid for part in values.get("support_roles", "").replace(",", " ").split() if (rid := parse_id(part))]

    if panel_channel_id:
        await database.db.set(guild.id, "ticket_panel_channel", panel_channel_id)
    if category_id:
        await database.db.set(guild.id, "ticket_category", category_id)
    if role_ids:
        await database.db.set(guild.id, "ticket_support_roles", role_ids)
    if log_channel_id:
        await database.db.set(guild.id, "ticket_log_channel", log_channel_id)

    lines = []
    if panel_channel_id:
        lines.append(f"Panel channel: <#{panel_channel_id}>")
    if category_id:
        lines.append(f"Category: <#{category_id}>")
    if role_ids:
        lines.append("Support roles: " + ", ".join(f"<@&{rid}>" for rid in role_ids))
    if log_channel_id:
        lines.append(f"Log channel: <#{log_channel_id}>")
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the ticket step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="tickets",
            label="Tickets",
            emoji="🎫",
            description="Panel channel, category, support roles and log channel.",
            fields=[
                ("Panel channel", "#support", True, "panel_channel"),
                ("Ticket category", "Support", True, "category"),
                ("Support roles", "@Support (comma separated)", True, "support_roles"),
                ("Log channel", "#ticket-logs (optional)", False, "log_channel"),
            ],
            handler=_apply_ticket_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Tickets(commands.Cog):
    """Ticket management commands."""

    ticket_group = app_commands.Group(
        name="ticket",
        description="Ticket system commands.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        self.auto_close_loop.cancel()

    # -- commands ----------------------------------------------------------
    @ticket_group.command(name="setup", description="Configure the ticket system (panel, category, roles, logs).")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_setup(self, interaction: discord.Interaction) -> None:
        """Open the ticket setup form."""
        await interaction.response.send_modal(TicketSetupModal())

    @ticket_group.command(
        name="panel", description="Post the ticket panel (buttons or a select menu)."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where to post the panel",
        style="Button panel or select menu",
        categories="Which categories to offer, comma separated (support, report, purchase, other)",
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Buttons", value="buttons"),
            app_commands.Choice(name="Select menu", value="select"),
        ]
    )
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        style: app_commands.Choice[str] = None,  # type: ignore[assignment]
        categories: Optional[str] = None,
    ) -> None:
        """Post a panel message with a persistent view."""
        assert interaction.guild is not None
        keys = list(PANEL_OPTIONS)
        if categories:
            requested = [c.strip().lower() for c in categories.replace(",", " ").split() if c.strip()]
            keys = [k for k in requested if k in PANEL_OPTIONS]
            if not keys:
                await interaction.response.send_message(
                    embed=error_embed(
                        "No valid categories. Available: " + ", ".join(PANEL_OPTIONS)
                    ),
                    ephemeral=True,
                )
                return
        style_value = style.value if style else "buttons"
        option_lines = "\n".join(
            f"{PANEL_OPTIONS[k][1]} **{PANEL_OPTIONS[k][0]}** — open a {PANEL_OPTIONS[k][0].lower()} ticket"
            for k in keys
        )
        embed = base_embed(
            title="🎫 Support tickets",
            description=(
                "Need help? Open a ticket and our support team will get back to you as soon as possible.\n\n"
                f"{option_lines}\n\n"
                "*Only you and the support team can see your ticket.*"
            ),
        )
        try:
            message = await channel.send(embed=embed, view=build_panel_view(style_value, keys))
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("I could not send the panel there. Do I have access to that channel?"),
                ephemeral=True,
            )
            return
        await database.db.execute(
            "INSERT INTO ticket_panels (guild_id, channel_id, message_id, style, options) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, channel.id, message.id, style_value, json.dumps(keys)),
        )
        await database.db.set(interaction.guild.id, "ticket_panel_channel", channel.id)
        await interaction.response.send_message(
            embed=success_embed(f"Panel posted in {channel.mention}."), ephemeral=True
        )

    @ticket_group.command(name="open", description="Open a ticket manually.")
    @app_commands.describe(reason="Why you are opening the ticket")
    async def ticket_open(self, interaction: discord.Interaction, reason: str = "No reason provided") -> None:
        """Create a ticket channel for the invoker."""
        await interaction.response.defer(ephemeral=True)
        await open_ticket(interaction, "support", reason)

    @ticket_group.command(name="close", description="Close the current ticket (with a transcript).")
    @app_commands.describe(reason="Reason for closing")
    async def ticket_close(self, interaction: discord.Interaction, reason: str = "No reason provided") -> None:
        """Close a ticket and generate its transcript."""
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        support_roles = await _get_support_roles(guild)
        is_staff = any(role in interaction.user.roles for role in support_roles) or interaction.user.guild_permissions.manage_messages
        if interaction.user.id != int(ticket["owner_id"]) and not is_staff:
            await interaction.response.send_message(
                embed=error_embed("Only the ticket owner or the support team can close this ticket."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=info_embed("Closing ticket and generating the transcript…"))
        await close_ticket(channel, interaction.user, reason)

    @ticket_group.command(name="delete", description="Close a ticket and delete its channel immediately.")
    @app_commands.describe(reason="Reason for deleting")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_delete(self, interaction: discord.Interaction, reason: str = "Deleted by staff") -> None:
        """Force-close a ticket without the rating DM niceties."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        await interaction.response.send_message(embed=info_embed("Deleting ticket…"))
        await close_ticket(channel, interaction.user, reason)

    @ticket_group.command(name="claim", description="Claim the current ticket.")
    async def ticket_claim(self, interaction: discord.Interaction) -> None:
        """Assign yourself to the current ticket."""
        await _claim_ticket(interaction)

    @ticket_group.command(name="add", description="Add a user to the current ticket.")
    @app_commands.describe(member="User to add")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Give a user access to the ticket channel."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        await channel.set_permissions(
            member,
            overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        )
        await interaction.response.send_message(embed=success_embed(f"{member.mention} was added to this ticket."))
        await channel.send(embed=info_embed(f"{member.mention} was added by {interaction.user.mention}."))

    @ticket_group.command(name="remove", description="Remove a user from the current ticket.")
    @app_commands.describe(member="User to remove")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Revoke a user's access to the ticket channel."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        await channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(embed=success_embed(f"{member.mention} was removed from this ticket."))

    @ticket_group.command(name="rename", description="Rename the current ticket channel.")
    @app_commands.describe(name="New channel name")
    async def ticket_rename(self, interaction: discord.Interaction, name: str) -> None:
        """Rename the ticket channel."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        cleaned = _sanitize_channel_name(name)
        try:
            await channel.edit(name=cleaned, reason=f"Ticket renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("I could not rename this channel."), ephemeral=True)
            return
        await interaction.response.send_message(embed=success_embed(f"Ticket renamed to **#{cleaned}**."))

    @ticket_group.command(name="transfer", description="Transfer the current ticket to another user.")
    @app_commands.describe(member="New ticket owner")
    async def ticket_transfer(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Hand the ticket over to another user."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Use this inside a ticket channel."), ephemeral=True)
            return
        ticket = await _get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message(embed=error_embed("This is not an open ticket."), ephemeral=True)
            return
        old_owner = await _fetch_member(channel.guild, int(ticket["owner_id"]))
        await database.db.execute("UPDATE tickets SET owner_id = ? WHERE channel_id = ?", (member.id, channel.id))
        await channel.set_permissions(
            member,
            overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        )
        if old_owner is not None and old_owner.id != member.id:
            await channel.set_permissions(
                old_owner,
                overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            )
        _CACHE[channel.id] = (channel.guild.id, member.id, int(time.time()))
        await interaction.response.send_message(embed=success_embed(f"Ticket transferred to {member.mention}."))

    @ticket_group.command(name="list", description="List all open tickets on this server.")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of open tickets."""
        assert interaction.guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY counter", (interaction.guild.id,)
        )
        if not rows:
            await interaction.response.send_message(embed=info_embed("There are no open tickets."), ephemeral=True)
            return
        pages = []
        for start in range(0, len(rows), 8):
            embed = base_embed(title="🎫 Open tickets")
            for row in rows[start : start + 8]:
                owner = interaction.guild.get_member(int(row["owner_id"]))
                claimer = interaction.guild.get_member(int(row["claimer_id"])) if row["claimer_id"] else None
                value = (
                    f"Owner: {owner.mention if owner else row['owner_id']}\n"
                    f"Claimed by: {claimer.mention if claimer else '—'}\n"
                    f"Reason: {truncate(row['reason'] or '—', 150)}\n"
                    f"Created: <t:{row['created_at']}:R>"
                )
                embed.add_field(
                    name=f"#{row['counter']} • <#{row['channel_id']}>", value=value, inline=False
                )
            embed.set_footer(text=f"{len(rows)} open ticket(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    @ticket_group.command(name="settings", description="Show the current ticket configuration.")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_settings(self, interaction: discord.Interaction) -> None:
        """Display ticket settings."""
        assert interaction.guild is not None
        guild = interaction.guild
        panel = await database.db.get(guild.id, "ticket_panel_channel")
        category = await database.db.get(guild.id, "ticket_category")
        log_channel = await database.db.get(guild.id, "ticket_log_channel")
        roles = await _get_support_roles(guild)
        limit = await database.db.get_int(guild.id, "ticket_limit", config.DEFAULT_OPEN_TICKETS_PER_USER)
        inactive = await database.db.get_int(guild.id, "ticket_inactivity_minutes", config.DEFAULT_TICKET_INACTIVITY_MINUTES)
        rating = await database.db.get_bool(guild.id, "ticket_rating", True)
        open_count_row = await database.db.fetchone(
            "SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND status = 'open'", (guild.id,)
        )
        embed = base_embed(
            title="🎫 Ticket settings",
            fields=[
                ("Panel channel", f"<#{panel}>" if panel else "Not set", True),
                ("Category", f"<#{category}>" if category else "Not set", True),
                ("Log channel", f"<#{log_channel}>" if log_channel else "Not set", True),
                ("Support roles", ", ".join(r.mention for r in roles) or "None", False),
                ("Open tickets", str(open_count_row["c"] if open_count_row else 0), True),
                ("Limit per user", str(limit) if limit > 0 else "Unlimited", True),
                ("Auto-close after", f"{inactive} minutes" if inactive > 0 else "Disabled", True),
                ("Rating after close", "Enabled" if rating else "Disabled", True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket_group.command(
        name="inactivity", description="Set how many minutes of inactivity before a ticket auto-closes."
    )
    @app_commands.describe(minutes="Minutes of inactivity (0 disables auto-close)")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_inactivity(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 43200]) -> None:
        """Configure the inactivity auto-close timer."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "ticket_inactivity_minutes", minutes)
        text = f"Tickets will auto-close after **{minutes} minutes** of inactivity." if minutes else "Inactivity auto-close disabled."
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    @ticket_group.command(name="limit", description="Set the maximum number of open tickets per user.")
    @app_commands.describe(limit="Maximum open tickets per user (0 = unlimited)")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 50]) -> None:
        """Configure the per-user ticket limit."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "ticket_limit", limit)
        text = f"Users may now have up to **{limit}** open ticket(s)." if limit else "The ticket limit was removed."
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    @ticket_group.command(name="rating", description="Toggle the rating question after a ticket is closed.")
    @app_commands.describe(enabled="Whether to ask for a rating")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_rating(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle post-close ratings."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "ticket_rating", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Ticket ratings are now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- listeners & background task -------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Track ticket activity for the inactivity auto-close."""
        if message.channel.id in _CACHE:
            guild_id, owner_id, _ = _CACHE[message.channel.id]
            _CACHE[message.channel.id] = (guild_id, owner_id, int(time.time()))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Warm the ticket cache after a restart and start the auto-close task."""
        if not self.auto_close_loop.is_running():
            self.auto_close_loop.start()
        rows = await database.db.fetchall("SELECT channel_id, guild_id, owner_id, last_activity FROM tickets WHERE status = 'open'")
        for row in rows:
            _CACHE[int(row["channel_id"])] = (int(row["guild_id"]), int(row["owner_id"]), int(row["last_activity"]))

    @tasks.loop(minutes=5)
    async def auto_close_loop(self) -> None:
        """Auto-close tickets that have been inactive for too long."""
        await self.bot.wait_until_ready()
        now = int(time.time())
        for channel_id, (guild_id, owner_id, last_activity) in list(_CACHE.items()):
            threshold = await database.db.get_int(guild_id, "ticket_inactivity_minutes", config.DEFAULT_TICKET_INACTIVITY_MINUTES)
            if threshold <= 0 or now - last_activity < threshold * 60:
                continue
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await close_ticket(channel, guild.me or self.bot.user, "Auto-closed: inactive ticket")
                except discord.HTTPException:
                    log.exception("Auto-close failed for ticket channel %s", channel_id)
            else:
                _CACHE.pop(channel_id, None)

    @auto_close_loop.before_loop
    async def before_auto_close_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the tickets cog."""
    await bot.add_cog(Tickets(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [TicketCreateView(), TicketPanelView(), TicketActionsView(), TicketRatingView()]
