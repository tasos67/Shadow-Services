"""Role panels: reaction roles, button roles and select-menu roles with a
persistent, restart-safe implementation."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import parse_id, truncate
from utils.paginator import paginate

log = logging.getLogger("cogs.reactionroles")

MAX_BUTTON_ENTRIES = 5
MAX_SELECT_ENTRIES = 25
DEFAULT_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):(\d+)>")


def parse_emojis(text: str) -> list[str]:
    """Parse a whitespace/comma separated emoji list (unicode + custom)."""
    if not text:
        return []
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    return tokens


async def _find_panel_by_message(message_id: int) -> Optional[dict]:
    row = await database.db.fetchone(
        "SELECT * FROM reaction_role_panels WHERE message_id = ?", (message_id,)
    )
    return dict(row) if row else None


async def _find_panel_by_id(panel_id: int) -> Optional[dict]:
    row = await database.db.fetchone("SELECT * FROM reaction_role_panels WHERE id = ?", (panel_id,))
    return dict(row) if row else None


def _entries(panel: dict) -> list[dict]:
    try:
        data = json.loads(panel["entries"])
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _save_entries(panel_id: int, entries: list[dict]) -> None:
    await database.db.execute(
        "UPDATE reaction_role_panels SET entries = ? WHERE id = ?", (json.dumps(entries), panel_id)
    )


async def _toggle_role(interaction: discord.Interaction, role: discord.Role) -> None:
    """Add the role if missing, otherwise remove it."""
    member = interaction.user
    guild = interaction.guild
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
        return
    me = guild.me
    if role >= me.top_role or not me.guild_permissions.manage_roles:
        await interaction.response.send_message(
            embed=error_embed("I cannot manage that role — it is higher than my highest role."), ephemeral=True
        )
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Role panel")
            await interaction.response.send_message(
                embed=success_embed(f"Removed {role.mention}.", title="Role updated"), ephemeral=True
            )
        else:
            await member.add_roles(role, reason="Role panel")
            await interaction.response.send_message(
                embed=success_embed(f"Added {role.mention}.", title="Role updated"), ephemeral=True
            )
    except discord.HTTPException:
        await interaction.response.send_message(
            embed=error_embed("Something went wrong while updating your roles."), ephemeral=True
        )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class RoleButton(discord.ui.Button):
    """One role button on a panel (index based, fixed custom id)."""

    def __init__(self, index: int, label: Optional[str] = None, emoji: Optional[str] = None) -> None:
        super().__init__(
            label=label or f"Role {index + 1}",
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"rr:button:{index}",
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
            return
        panel = await _find_panel_by_message(interaction.message.id)
        if panel is None or panel["type"] != "button":
            await interaction.response.send_message(
                embed=error_embed("This role panel no longer exists."), ephemeral=True
            )
            return
        entries = _entries(panel)
        if self.index >= len(entries):
            await interaction.response.send_message(embed=error_embed("This button is not configured."), ephemeral=True)
            return
        role = interaction.guild.get_role(int(entries[self.index]["role_id"]))
        if role is None:
            await interaction.response.send_message(embed=error_embed("That role was deleted."), ephemeral=True)
            return
        await _toggle_role(interaction, role)


class RoleSelect(discord.ui.Select):
    """Select-menu role picker (fixed custom id, options resolved at runtime)."""

    def __init__(self, entries: Optional[list[dict]] = None, placeholder: str = "Pick your roles…") -> None:
        entries = (entries or [])[:MAX_SELECT_ENTRIES]
        options = [
            discord.SelectOption(
                label=str(entry.get("role_name") or entry["role_id"]),
                value=str(entry["role_id"]),
                emoji=entry.get("emoji") or None,
            )
            for entry in entries
        ]
        super().__init__(
            custom_id="rr:select",
            placeholder=placeholder,
            options=options or [discord.SelectOption(label="No roles configured", value="none")],
            min_values=0,
            max_values=max(1, len(options)) if options else 1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
            return
        panel = await _find_panel_by_message(interaction.message.id)
        if panel is None:
            await interaction.response.send_message(embed=error_embed("This role panel no longer exists."), ephemeral=True)
            return
        entries = {str(e["role_id"]): e for e in _entries(panel)}
        member = interaction.user
        assert isinstance(member, discord.Member)
        added, removed = [], []
        for value in self.values:
            entry = entries.get(value)
            role = interaction.guild.get_role(int(entry["role_id"])) if entry else None
            if role is None or role >= interaction.guild.me.top_role:
                continue
            try:
                if role in member.roles:
                    # selected roles stay; unselected ones are removed below
                    continue
                await member.add_roles(role, reason="Role panel")
                added.append(role.mention)
            except discord.HTTPException:
                continue
        # remove roles the member has that belong to this panel but were deselected
        selected = set(self.values)
        for value, entry in entries.items():
            if value in selected:
                continue
            role = interaction.guild.get_role(int(entry["role_id"]))
            if role is None or role >= interaction.guild.me.top_role or role not in member.roles:
                continue
            try:
                await member.remove_roles(role, reason="Role panel")
                removed.append(role.mention)
            except discord.HTTPException:
                continue
        parts = []
        if added:
            parts.append("Added: " + ", ".join(added))
        if removed:
            parts.append("Removed: " + ", ".join(removed))
        await interaction.response.send_message(
            embed=success_embed("\n".join(parts) or "No changes were made.", title="Roles updated"), ephemeral=True
        )


class PersistentRoleView(discord.ui.View):
    """Persistent routing view for button and select role panels."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RoleSelect())
        for index in range(MAX_BUTTON_ENTRIES):
            self.add_item(RoleButton(index))


def build_role_view(panel_type: str, entries: list[dict], placeholder: str) -> discord.ui.View:
    """Build the exact view sent with a role panel message."""
    view = discord.ui.View(timeout=None)
    if panel_type == "select":
        view.add_item(RoleSelect(entries, placeholder))
    else:
        for index, entry in enumerate(entries[:MAX_BUTTON_ENTRIES]):
            view.add_item(
                RoleButton(
                    index,
                    label=str(entry.get("role_name") or f"Role {index + 1}")[:80],
                    emoji=entry.get("emoji") or None,
                )
            )
    return view


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class ReactionRoles(commands.Cog):
    """Reaction, button and select-menu role panels."""

    rr_group = app_commands.Group(
        name="reactionrole",
        description="Create and manage self-assignable role panels.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @rr_group.command(name="create", description="Create a role panel.")
    @app_commands.describe(
        channel="Channel where the panel is posted",
        type="Reaction, button or select-menu panel",
        roles="Role mentions or IDs, separated by spaces or commas",
        emojis="Emojis for the buttons/reactions (optional, space separated)",
        title="Panel title",
        description="Panel description",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Buttons", value="button"),
            app_commands.Choice(name="Select menu", value="select"),
            app_commands.Choice(name="Reactions", value="reaction"),
        ]
    )
    async def rr_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        type: app_commands.Choice[str],
        roles: str,
        emojis: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        """Post a new role panel with the given entries."""
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True)
        role_ids = [rid for part in roles.replace(",", " ").split() if (rid := parse_id(part))]
        if not role_ids:
            await interaction.followup.send(
                embed=error_embed("I could not parse any roles. Mention them like `@Member @Artist`."), ephemeral=True
            )
            return
        max_entries = MAX_SELECT_ENTRIES if type.value == "select" else (
            MAX_BUTTON_ENTRIES if type.value == "button" else 50
        )
        role_ids = role_ids[:max_entries]
        emoji_list = parse_emojis(emojis or "")

        entries: list[dict] = []
        for index, role_id in enumerate(role_ids):
            role = guild.get_role(role_id)
            if role is None:
                continue
            emoji: Optional[str] = emoji_list[index] if index < len(emoji_list) else None
            if emoji is None and type.value in {"reaction", "button"}:
                emoji = DEFAULT_NUMBER_EMOJIS[index] if index < len(DEFAULT_NUMBER_EMOJIS) else None
            entries.append({"role_id": role.id, "role_name": role.name, "emoji": emoji})
        if not entries:
            await interaction.followup.send(embed=error_embed("None of the given roles exist."), ephemeral=True)
            return

        embed = base_embed(
            title=title or "🎭 Self-assignable roles",
            description=description or "Use the "
            + ("reactions below" if type.value == "reaction" else "options below")
            + " to give yourself a role.",
        )
        for entry in entries:
            role_mention = f"<@&{entry['role_id']}>"
            emoji_text = f"{entry['emoji']} " if entry["emoji"] else ""
            embed.add_field(name=f"{emoji_text}{entry['role_name']}", value=role_mention, inline=True)

        try:
            message = await channel.send(embed=embed, view=build_role_view(type.value, entries, "Pick your roles…"))
        except discord.HTTPException:
            await interaction.followup.send(
                embed=error_embed("I could not post the panel there. Check my permissions."), ephemeral=True
            )
            return

        if type.value == "reaction":
            for entry in entries:
                if not entry["emoji"]:
                    continue
                try:
                    await message.add_reaction(entry["emoji"])
                except discord.HTTPException:
                    log.warning("Could not add reaction %s to panel %s", entry["emoji"], message.id)

        panel_id = await database.db.execute(
            "INSERT INTO reaction_role_panels (guild_id, channel_id, message_id, type, entries) VALUES (?, ?, ?, ?, ?)",
            (guild.id, channel.id, message.id, type.value, json.dumps(entries)),
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Role panel posted in {channel.mention}.\nPanel ID: **{panel_id}** — "
                f"use `/reactionrole add`/`remove`/`delete` to manage it."
            ),
            ephemeral=True,
        )

    @rr_group.command(name="add", description="Add a role entry to an existing panel.")
    @app_commands.describe(panel_id="Panel ID shown when it was created", role="Role to add", emoji="Emoji (optional)")
    async def rr_add(
        self, interaction: discord.Interaction, panel_id: int, role: discord.Role, emoji: Optional[str] = None
    ) -> None:
        """Append one role to a panel and refresh its message."""
        guild = interaction.guild
        assert guild is not None
        panel = await _find_panel_by_id(panel_id)
        if panel is None or panel["guild_id"] != guild.id:
            await interaction.response.send_message(embed=error_embed("Panel not found."), ephemeral=True)
            return
        entries = _entries(panel)
        limit = MAX_SELECT_ENTRIES if panel["type"] == "select" else MAX_BUTTON_ENTRIES
        if len(entries) >= limit:
            await interaction.response.send_message(
                embed=error_embed(f"This panel already has the maximum of {limit} entries."), ephemeral=True
            )
            return
        if emoji is None and panel["type"] in {"reaction", "button"}:
            emoji = DEFAULT_NUMBER_EMOJIS[len(entries)] if len(entries) < len(DEFAULT_NUMBER_EMOJIS) else None
        entries.append({"role_id": role.id, "role_name": role.name, "emoji": emoji})
        await _save_entries(panel_id, entries)
        await self._refresh_panel(panel, entries)
        await interaction.response.send_message(
            embed=success_embed(f"Added {role.mention} to panel **{panel_id}**."), ephemeral=True
        )

    @rr_group.command(name="remove", description="Remove a role entry from a panel.")
    @app_commands.describe(panel_id="Panel ID", role="Role to remove")
    async def rr_remove(self, interaction: discord.Interaction, panel_id: int, role: discord.Role) -> None:
        """Remove one role from a panel and refresh its message."""
        guild = interaction.guild
        assert guild is not None
        panel = await _find_panel_by_id(panel_id)
        if panel is None or panel["guild_id"] != guild.id:
            await interaction.response.send_message(embed=error_embed("Panel not found."), ephemeral=True)
            return
        entries = [e for e in _entries(panel) if int(e["role_id"]) != role.id]
        await _save_entries(panel_id, entries)
        await self._refresh_panel(panel, entries)
        await interaction.response.send_message(
            embed=success_embed(f"Removed {role.mention} from panel **{panel_id}**."), ephemeral=True
        )

    @rr_group.command(name="delete", description="Delete a role panel and its message.")
    @app_commands.describe(panel_id="Panel ID")
    async def rr_delete(self, interaction: discord.Interaction, panel_id: int) -> None:
        """Delete a panel from the database and remove its message."""
        guild = interaction.guild
        assert guild is not None
        panel = await _find_panel_by_id(panel_id)
        if panel is None or panel["guild_id"] != guild.id:
            await interaction.response.send_message(embed=error_embed("Panel not found."), ephemeral=True)
            return
        channel = guild.get_channel(int(panel["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(int(panel["message_id"]))
                await message.delete()
            except discord.HTTPException:
                pass
        await database.db.execute("DELETE FROM reaction_role_panels WHERE id = ?", (panel_id,))
        await interaction.response.send_message(embed=success_embed(f"Panel **{panel_id}** deleted."), ephemeral=True)

    @rr_group.command(name="list", description="List all role panels on this server.")
    async def rr_list(self, interaction: discord.Interaction) -> None:
        """Paginated overview of role panels."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM reaction_role_panels WHERE guild_id = ? ORDER BY id", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(embed=info_embed("No role panels configured yet."), ephemeral=True)
            return
        pages = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="🎭 Role panels")
            for row in rows[start : start + 5]:
                entries = _entries(dict(row))
                roles = ", ".join(
                    f"{e.get('emoji') or ''} <@&{e['role_id']}>".strip() for e in entries[:10]
                ) or "—"
                embed.add_field(
                    name=f"#{row['id']} • {row['type']} • <#{row['channel_id']}>",
                    value=truncate(f"{roles}\n[jump to message](https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']})", 1000),
                    inline=False,
                )
            embed.set_footer(text=f"{len(rows)} panel(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    async def _refresh_panel(self, panel: dict, entries: list[dict]) -> None:
        """Edit the panel message (embed + buttons) after entry changes."""
        guild = self.bot.get_guild(int(panel["guild_id"]))
        if guild is None:
            return
        channel = guild.get_channel(int(panel["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(int(panel["message_id"]))
        except discord.HTTPException:
            return
        embed = message.embeds[0] if message.embeds else base_embed(title="🎭 Self-assignable roles")
        new_embed = base_embed(
            title=embed.title or "🎭 Self-assignable roles",
            description=embed.description or "Pick your roles below.",
        )
        for entry in entries:
            emoji_text = f"{entry['emoji']} " if entry.get("emoji") else ""
            new_embed.add_field(
                name=f"{emoji_text}{entry.get('role_name') or entry['role_id']}",
                value=f"<@&{entry['role_id']}>",
                inline=True,
            )
        try:
            await message.edit(embed=new_embed, view=build_role_view(panel["type"], entries, "Pick your roles…"))
        except discord.HTTPException:
            log.warning("Could not refresh role panel %s", panel["id"])

    # -- listeners ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Give a role when the matching reaction is added."""
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        panel = await _find_panel_by_message(payload.message_id)
        if panel is None or panel["type"] != "reaction":
            return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if member.bot:
            return
        target = str(payload.emoji)
        for entry in _entries(panel):
            if entry.get("emoji") == target:
                role = guild.get_role(int(entry["role_id"]))
                if role is not None and role < guild.me.top_role:
                    try:
                        await member.add_roles(role, reason="Reaction role")
                    except discord.HTTPException:
                        log.warning("Reaction role add failed for %s", member)
                break

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Remove the role when the reaction is removed."""
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        panel = await _find_panel_by_message(payload.message_id)
        if panel is None or panel["type"] != "reaction":
            return
        guild = self.bot.get_guild(payload.guild_id or 0)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            return
        target = str(payload.emoji)
        for entry in _entries(panel):
            if entry.get("emoji") == target:
                role = guild.get_role(int(entry["role_id"]))
                if role is not None and role < guild.me.top_role:
                    try:
                        await member.remove_roles(role, reason="Reaction role removed")
                    except discord.HTTPException:
                        log.warning("Reaction role remove failed for %s", member)
                break


async def setup(bot: commands.Bot) -> None:
    """Load the reaction roles cog."""
    await bot.add_cog(ReactionRoles(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [PersistentRoleView()]
