"""Custom commands, auto-responders and tags triggered by a configurable
prefix (default ``?``), plus their management slash commands."""
from __future__ import annotations

import logging
import random
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import placeholder_format, truncate
from utils.paginator import paginate, paginate_embeds

log = logging.getLogger("cogs.customcommands")

DEFAULT_PREFIX = "?"
MAX_PREFIX_LENGTH = 10
MATCH_MODES: tuple[str, ...] = ("contains", "equals", "starts")
NAME_RE = re.compile(r"^[a-z0-9_\-]{1,50}$")
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


# ---------------------------------------------------------------------------
# Settings & data helpers
# ---------------------------------------------------------------------------
def _clean_name(name: str) -> Optional[str]:
    """Normalise a command/tag name, returning None when it is invalid."""
    cleaned = name.strip().lower()
    return cleaned if NAME_RE.match(cleaned) else None


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


async def _get_prefix(guild_id: int) -> str:
    """Return the guild's configured custom command prefix."""
    prefix = await database.db.get(guild_id, "custom_prefix", DEFAULT_PREFIX)
    return prefix if isinstance(prefix, str) and prefix else DEFAULT_PREFIX


async def _find_custom_command(guild_id: int, name: str) -> Optional[dict]:
    """Look up one custom command by (guild, name)."""
    row = await database.db.fetchone(
        "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?", (guild_id, name)
    )
    return dict(row) if row else None


async def _find_tag(guild_id: int, name: str) -> Optional[dict]:
    """Look up one tag by (guild, name)."""
    row = await database.db.fetchone("SELECT * FROM tags WHERE guild_id = ? AND name = ?", (guild_id, name))
    return dict(row) if row else None


async def _auto_response_count(guild_id: int) -> int:
    """Count custom commands + auto-responders (against AUTO_RESPONSE_LIMIT)."""
    row = await database.db.fetchone(
        "SELECT (SELECT COUNT(*) FROM custom_commands WHERE guild_id = ?) + "
        "(SELECT COUNT(*) FROM autoresponders WHERE guild_id = ?) AS total",
        (guild_id, guild_id),
    )
    return int(row["total"]) if row else 0


async def _tag_count(guild_id: int) -> int:
    """Count the tags stored for a guild (against TAG_LIMIT)."""
    row = await database.db.fetchone("SELECT COUNT(*) AS total FROM tags WHERE guild_id = ?", (guild_id,))
    return int(row["total"]) if row else 0


async def _apply_custom_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save custom command settings from the /setup wizard."""
    lines: list[str] = []
    prefix = values.get("prefix", "").strip()
    if prefix:
        prefix = prefix[:MAX_PREFIX_LENGTH]
        await database.db.set(guild.id, "custom_prefix", prefix)
        lines.append(f"Command prefix: `{prefix}`")
    auto = _parse_yes_no(values.get("autoresponder_enabled", ""))
    if auto is not None:
        await database.db.set(guild.id, "autoresponder_enabled", auto)
        lines.append(f"Auto-responders: **{'enabled' if auto else 'disabled'}**")
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="customcommands",
        label="Custom Commands",
        emoji="🤖",
        description="Command prefix and auto-responder behaviour.",
        fields=[
            ("Command prefix", "?", True, "prefix"),
            ("Enable auto-responders (yes/no)", "yes", False, "autoresponder_enabled"),
        ],
        handler=_apply_custom_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class CustomCommands(commands.Cog):
    """Custom commands, auto-responders and tags."""

    customcommand_group = app_commands.Group(
        name="customcommand",
        description="Manage prefix custom commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    autoresponder_group = app_commands.Group(
        name="autoresponder",
        description="Manage automatic replies to matching messages.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    tag_group = app_commands.Group(
        name="tag", description="Server tags: short reusable text snippets.", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- listeners ------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Answer prefix custom commands/tags and auto-responders."""
        if message.guild is None or message.author.bot or not message.content:
            return
        try:
            if await self._run_custom_command(message):
                return
            await self._run_autoresponders(message)
        except (discord.HTTPException, discord.Forbidden):
            log.exception("Could not answer message %s in channel %s", message.id, message.channel.id)
        except Exception:
            log.exception("Custom command handling failed for message %s", message.id)

    async def _run_custom_command(self, message: discord.Message) -> bool:
        """Reply when the message is a custom command or a tag. Returns True when answered."""
        guild = message.guild
        assert guild is not None and isinstance(message.author, discord.Member)
        if not await database.db.get_bool(guild.id, "customcommands_enabled", True):
            return False
        prefix = await _get_prefix(guild.id)
        if not message.content.startswith(prefix):
            return False
        remainder = message.content[len(prefix) :].strip()
        if not remainder:
            return False
        name, _, args = remainder.partition(" ")
        name = name.strip().lower()
        if not name:
            return False

        row = await _find_custom_command(guild.id, name)
        if row is not None:
            await database.db.execute(
                "UPDATE custom_commands SET uses = uses + 1 WHERE guild_id = ? AND name = ?",
                (guild.id, name),
            )
            text = placeholder_format(
                row["response"], member=message.author, guild=guild, extra={"args": args.strip()}
            )
            await message.channel.send(text, allowed_mentions=SAFE_MENTIONS)
            return True
        return await self._run_tag_lookup(message, name, args.strip())

    async def _run_tag_lookup(self, message: discord.Message, name: str, args: str) -> bool:
        """Reply with a tag when ``?name`` matches one."""
        guild = message.guild
        assert guild is not None and isinstance(message.author, discord.Member)
        row = await _find_tag(guild.id, name)
        if row is None:
            return False
        await database.db.execute(
            "UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (guild.id, name)
        )
        text = placeholder_format(
            row["content"], member=message.author, guild=guild, extra={"args": args, "tag": name}
        )
        await message.channel.send(text, allowed_mentions=SAFE_MENTIONS)
        return True

    async def _run_autoresponders(self, message: discord.Message) -> None:
        """Reply with the first matching auto-responder, if any."""
        guild = message.guild
        assert guild is not None and isinstance(message.author, discord.Member)
        if not await database.db.get_bool(guild.id, "autoresponder_enabled", True):
            return
        rows = await database.db.fetchall("SELECT * FROM autoresponders WHERE guild_id = ?", (guild.id,))
        if not rows:
            return
        content = message.content.casefold()
        for row in rows:
            trigger = str(row["trigger"] or "").casefold()
            if not trigger:
                continue
            mode = str(row["match_mode"] or "contains")
            if mode == "equals":
                matched = content == trigger
            elif mode == "starts":
                matched = content.startswith(trigger)
            else:
                matched = trigger in content
            if not matched:
                continue
            text = placeholder_format(
                row["response"], member=message.author, guild=guild, extra={"args": message.content}
            )
            await message.channel.send(text, allowed_mentions=SAFE_MENTIONS)
            return

    # -- /customcommand --------------------------------------------------------
    @customcommand_group.command(name="add", description="Create a new custom command.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        name="Command name typed after the prefix (letters, numbers, - and _ only)",
        response="Text to reply with — supports {user}, {server}, {membercount} and {args}",
    )
    @app_commands.guild_only()
    async def customcommand_add(
        self, interaction: discord.Interaction, name: str, response: str
    ) -> None:
        """Add a prefix command."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        if cleaned is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Names must be 1-50 characters long and use only letters, numbers, `_` or `-`."
                ),
                ephemeral=True,
            )
            return
        if await _find_custom_command(guild.id, cleaned) is not None:
            await interaction.response.send_message(
                embed=error_embed(f"`{cleaned}` already exists — use `/customcommand edit`."), ephemeral=True
            )
            return
        if await _auto_response_count(guild.id) >= config.AUTO_RESPONSE_LIMIT:
            await interaction.response.send_message(
                embed=error_embed(
                    f"You reached the limit of **{config.AUTO_RESPONSE_LIMIT}** custom commands and "
                    "auto-responders. Remove a few before adding more."
                ),
                ephemeral=True,
            )
            return
        await database.db.execute(
            "INSERT INTO custom_commands (guild_id, name, response, uses, created_by) "
            "VALUES (?, ?, ?, 0, ?)",
            (guild.id, cleaned, response, interaction.user.id),
        )
        prefix = await _get_prefix(guild.id)
        await interaction.response.send_message(
            embed=success_embed(f"Custom command `{prefix}{cleaned}` created."), ephemeral=True
        )

    @customcommand_group.command(name="edit", description="Change the reply of a custom command.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(name="Command name to edit", response="New reply text")
    @app_commands.guild_only()
    async def customcommand_edit(
        self, interaction: discord.Interaction, name: str, response: str
    ) -> None:
        """Edit a custom command's response."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        if cleaned is None or await _find_custom_command(guild.id, cleaned) is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a custom command called `{name}`."), ephemeral=True
            )
            return
        await database.db.execute(
            "UPDATE custom_commands SET response = ? WHERE guild_id = ? AND name = ?",
            (response, guild.id, cleaned),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Custom command `{cleaned}` updated."), ephemeral=True
        )

    @customcommand_group.command(name="remove", description="Delete a custom command.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(name="Command name to delete")
    @app_commands.guild_only()
    async def customcommand_remove(self, interaction: discord.Interaction, name: str) -> None:
        """Delete a custom command."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        if cleaned is None or await _find_custom_command(guild.id, cleaned) is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a custom command called `{name}`."), ephemeral=True
            )
            return
        await database.db.execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?", (guild.id, cleaned)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Custom command `{cleaned}` deleted."), ephemeral=True
        )

    @customcommand_group.command(name="list", description="List every custom command of this server.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def customcommand_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of custom commands."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? ORDER BY uses DESC, name", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("There are no custom commands yet — add one with `/customcommand add`."),
                ephemeral=True,
            )
            return
        prefix = await _get_prefix(guild.id)
        pages = paginate_embeds(
            rows,
            title="🤖 Custom commands",
            description_builder=lambda row: (
                f"`{prefix}{row['name']}`",
                f"Uses: **{row['uses']}**\nResponse: {truncate(row['response'], 300)}",
            ),
            per_page=4,
            footer_extra=f"{len(rows)} command(s) • prefix `{prefix}`",
        )
        await paginate(interaction, pages)

    @customcommand_group.command(name="prefix", description="Change the prefix used for custom commands.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(prefix=f"New prefix, for example ? ! or . (max {MAX_PREFIX_LENGTH} characters)")
    @app_commands.guild_only()
    async def customcommand_prefix(self, interaction: discord.Interaction, prefix: str) -> None:
        """Store the custom command prefix."""
        guild = interaction.guild
        assert guild is not None
        cleaned = prefix.strip()
        if not cleaned:
            await interaction.response.send_message(
                embed=error_embed("The prefix cannot be empty."), ephemeral=True
            )
            return
        if len(cleaned) > MAX_PREFIX_LENGTH:
            await interaction.response.send_message(
                embed=error_embed(f"The prefix must be at most {MAX_PREFIX_LENGTH} characters long."),
                ephemeral=True,
            )
            return
        await database.db.set(guild.id, "custom_prefix", cleaned)
        await interaction.response.send_message(
            embed=success_embed(f"Custom commands now start with `{cleaned}`."), ephemeral=True
        )

    @customcommand_group.command(name="toggle", description="Enable or disable prefix custom commands.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(enabled="Whether custom commands are answered")
    @app_commands.guild_only()
    async def customcommand_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the prefix command listener."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "customcommands_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Custom commands are now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- /autoresponder --------------------------------------------------------
    @autoresponder_group.command(name="add", description="Create an automatic reply.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        trigger="Text that triggers the reply",
        response="Reply text — supports {user}, {server} and {membercount}",
        match_mode="How the trigger is matched against messages",
    )
    @app_commands.choices(
        match_mode=[
            app_commands.Choice(name="Contains (default)", value="contains"),
            app_commands.Choice(name="Exact match", value="equals"),
            app_commands.Choice(name="Starts with", value="starts"),
        ]
    )
    @app_commands.guild_only()
    async def autoresponder_add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        response: str,
        match_mode: app_commands.Choice[str] = None,  # type: ignore[assignment]
    ) -> None:
        """Add an auto-responder."""
        guild = interaction.guild
        assert guild is not None
        cleaned = trigger.strip()
        if not cleaned:
            await interaction.response.send_message(
                embed=error_embed("The trigger cannot be empty."), ephemeral=True
            )
            return
        if len(cleaned) > 200:
            await interaction.response.send_message(
                embed=error_embed("The trigger must be at most 200 characters long."), ephemeral=True
            )
            return
        if await _auto_response_count(guild.id) >= config.AUTO_RESPONSE_LIMIT:
            await interaction.response.send_message(
                embed=error_embed(
                    f"You reached the limit of **{config.AUTO_RESPONSE_LIMIT}** custom commands and "
                    "auto-responders. Remove a few before adding more."
                ),
                ephemeral=True,
            )
            return
        mode = match_mode.value if match_mode else "contains"
        new_id = await database.db.execute(
            "INSERT INTO autoresponders (guild_id, trigger, response, match_mode, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild.id, cleaned, response, mode, interaction.user.id),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Auto-responder **#{new_id}** created ({mode} `{cleaned}`)."),
            ephemeral=True,
        )

    @autoresponder_group.command(name="remove", description="Delete an auto-responder.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(id="Auto-responder id shown in /autoresponder list")
    @app_commands.guild_only()
    async def autoresponder_remove(self, interaction: discord.Interaction, id: int) -> None:
        """Delete an auto-responder by id."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT * FROM autoresponders WHERE id = ? AND guild_id = ?", (id, guild.id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find an auto-responder with id `{id}`."), ephemeral=True
            )
            return
        await database.db.execute(
            "DELETE FROM autoresponders WHERE id = ? AND guild_id = ?", (id, guild.id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Auto-responder **#{id}** deleted."), ephemeral=True
        )

    @autoresponder_group.command(name="list", description="List every auto-responder of this server.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def autoresponder_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of auto-responders."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM autoresponders WHERE guild_id = ? ORDER BY id", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("There are no auto-responders yet — add one with `/autoresponder add`."),
                ephemeral=True,
            )
            return
        pages = paginate_embeds(
            rows,
            title="🤖 Auto-responders",
            description_builder=lambda row: (
                f"#{row['id']} • {row['match_mode']}",
                f"Trigger: {truncate(row['trigger'], 120)}\nResponse: {truncate(row['response'], 200)}",
            ),
            per_page=4,
            footer_extra=f"{len(rows)} auto-responder(s)",
        )
        await paginate(interaction, pages)

    @autoresponder_group.command(name="toggle", description="Enable or disable auto-responders.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(enabled="Whether auto-responders are answered")
    @app_commands.guild_only()
    async def autoresponder_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the auto-responder listener."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "autoresponder_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Auto-responders are now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- /tag ------------------------------------------------------------------
    @tag_group.command(name="create", description="Create a new tag.")
    @app_commands.describe(
        name="Tag name (letters, numbers, - and _ only)",
        content="Text stored in the tag",
    )
    @app_commands.guild_only()
    async def tag_create(self, interaction: discord.Interaction, name: str, content: str) -> None:
        """Create a tag owned by the invoker."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        if cleaned is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Tag names must be 1-50 characters long and use only letters, numbers, `_` or `-`."
                ),
                ephemeral=True,
            )
            return
        if await _find_tag(guild.id, cleaned) is not None:
            await interaction.response.send_message(
                embed=error_embed(f"The tag `{cleaned}` already exists — use `/tag edit`."), ephemeral=True
            )
            return
        if await _tag_count(guild.id) >= config.TAG_LIMIT:
            await interaction.response.send_message(
                embed=error_embed(
                    f"This server reached the limit of **{config.TAG_LIMIT}** tags. "
                    "Delete a few before creating more."
                ),
                ephemeral=True,
            )
            return
        await database.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, uses) VALUES (?, ?, ?, ?, 0)",
            (guild.id, cleaned, content, interaction.user.id),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Tag `{cleaned}` created — use it with `/tag get {cleaned}`."),
            ephemeral=True,
        )

    @tag_group.command(name="get", description="Show the content of a tag.")
    @app_commands.describe(name="Tag name to show")
    @app_commands.guild_only()
    async def tag_get(self, interaction: discord.Interaction, name: str) -> None:
        """Send a tag's content in the channel."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        cleaned = _clean_name(name)
        row = await _find_tag(guild.id, cleaned) if cleaned else None
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a tag called `{name}`."), ephemeral=True
            )
            return
        await database.db.execute(
            "UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (guild.id, row["name"])
        )
        text = placeholder_format(
            row["content"], member=interaction.user, guild=guild, extra={"tag": row["name"]}
        )
        await interaction.response.send_message(text, allowed_mentions=SAFE_MENTIONS)

    @tag_group.command(name="edit", description="Change the content of a tag.")
    @app_commands.describe(name="Tag name", content="New tag content")
    @app_commands.guild_only()
    async def tag_edit(self, interaction: discord.Interaction, name: str, content: str) -> None:
        """Edit a tag (owner or Manage Messages)."""
        guild = interaction.guild
        assert guild is not None
        row = await _load_tag_for_edit(interaction, name)
        if row is None:
            return
        await database.db.execute(
            "UPDATE tags SET content = ? WHERE guild_id = ? AND name = ?",
            (content, guild.id, row["name"]),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Tag `{row['name']}` updated."), ephemeral=True
        )

    @tag_group.command(name="delete", description="Delete a tag.")
    @app_commands.describe(name="Tag name to delete")
    @app_commands.guild_only()
    async def tag_delete(self, interaction: discord.Interaction, name: str) -> None:
        """Delete a tag (owner or Manage Messages)."""
        guild = interaction.guild
        assert guild is not None
        row = await _load_tag_for_edit(interaction, name)
        if row is None:
            return
        await database.db.execute(
            "DELETE FROM tags WHERE guild_id = ? AND name = ?", (guild.id, row["name"])
        )
        await interaction.response.send_message(
            embed=success_embed(f"Tag `{row['name']}` deleted."), ephemeral=True
        )

    @tag_group.command(name="info", description="Show details about a tag.")
    @app_commands.describe(name="Tag name to inspect")
    @app_commands.guild_only()
    async def tag_info(self, interaction: discord.Interaction, name: str) -> None:
        """Show who owns a tag and how often it was used."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        row = await _find_tag(guild.id, cleaned) if cleaned else None
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a tag called `{name}`."), ephemeral=True
            )
            return
        owner = guild.get_member(int(row["owner_id"]))
        embed = base_embed(
            title=f"Tag: {row['name']}",
            description=truncate(row["content"], 1000),
            fields=[
                ("Owner", owner.mention if owner else f"<@{row['owner_id']}>", True),
                ("Uses", str(row["uses"]), True),
                ("Content length", f"{len(row['content'])} characters", True),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @tag_group.command(name="list", description="List the tags of this server, or of one member.")
    @app_commands.describe(member="Only show tags owned by this member")
    @app_commands.guild_only()
    async def tag_list(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        """Paginated list of tag names."""
        guild = interaction.guild
        assert guild is not None
        if member is not None:
            rows = await database.db.fetchall(
                "SELECT * FROM tags WHERE guild_id = ? AND owner_id = ? ORDER BY name",
                (guild.id, member.id),
            )
            footer = f"{len(rows)} tag(s) of {member}"
        else:
            rows = await database.db.fetchall(
                "SELECT * FROM tags WHERE guild_id = ? ORDER BY uses DESC, name", (guild.id,)
            )
            footer = f"{len(rows)} tag(s)"
        if not rows:
            await interaction.response.send_message(embed=info_embed("No tags found."), ephemeral=True)
            return
        pages = paginate_embeds(
            rows,
            title="🤖 Tags",
            description_builder=lambda row: (
                row["name"],
                f"Uses: **{row['uses']}** • Owner: <@{row['owner_id']}>",
            ),
            per_page=10,
            footer_extra=footer,
        )
        await paginate(interaction, pages)

    @tag_group.command(name="transfer", description="Transfer ownership of one of your tags.")
    @app_commands.describe(name="Tag name", member="New owner")
    @app_commands.guild_only()
    async def tag_transfer(
        self, interaction: discord.Interaction, name: str, member: discord.Member
    ) -> None:
        """Transfer a tag (owner only)."""
        guild = interaction.guild
        assert guild is not None
        cleaned = _clean_name(name)
        row = await _find_tag(guild.id, cleaned) if cleaned else None
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a tag called `{name}`."), ephemeral=True
            )
            return
        if int(row["owner_id"]) != interaction.user.id:
            await interaction.response.send_message(
                embed=error_embed("Only the owner of a tag can transfer it."), ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                embed=error_embed("Tags cannot be transferred to a bot."), ephemeral=True
            )
            return
        await database.db.execute(
            "UPDATE tags SET owner_id = ? WHERE guild_id = ? AND name = ?",
            (member.id, guild.id, row["name"]),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Tag `{row['name']}` is now owned by {member.mention}."), ephemeral=True
        )

    @tag_group.command(name="random", description="Show a random tag of this server.")
    @app_commands.guild_only()
    async def tag_random(self, interaction: discord.Interaction) -> None:
        """Send a random tag in the channel."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        rows = await database.db.fetchall("SELECT * FROM tags WHERE guild_id = ?", (guild.id,))
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("There are no tags on this server yet."), ephemeral=True
            )
            return
        row = random.choice(rows)
        await database.db.execute(
            "UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (guild.id, row["name"])
        )
        text = placeholder_format(
            row["content"], member=interaction.user, guild=guild, extra={"tag": row["name"]}
        )
        await interaction.response.send_message(text, allowed_mentions=SAFE_MENTIONS)

    @tag_group.command(name="search", description="Search tags by name.")
    @app_commands.describe(query="Text to look for inside tag names")
    @app_commands.guild_only()
    async def tag_search(self, interaction: discord.Interaction, query: str) -> None:
        """Paginated list of tags whose name contains the query."""
        guild = interaction.guild
        assert guild is not None
        needle = query.strip()
        if not needle:
            await interaction.response.send_message(
                embed=error_embed("Please provide something to search for."), ephemeral=True
            )
            return
        rows = await database.db.fetchall(
            "SELECT * FROM tags WHERE guild_id = ? AND LOWER(name) LIKE ? ORDER BY name",
            (guild.id, f"%{needle.lower()}%"),
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(f"No tag matches `{needle}`."), ephemeral=True
            )
            return
        pages = paginate_embeds(
            rows,
            title=f"🤖 Tags matching “{needle}”",
            description_builder=lambda row: (
                row["name"],
                f"Uses: **{row['uses']}** • `/tag get {row['name']}`",
            ),
            per_page=10,
            footer_extra=f"{len(rows)} result(s)",
        )
        await paginate(interaction, pages)

    # -- internals -------------------------------------------------------------
    async def _load_tag_for_edit(
        self, interaction: discord.Interaction, name: str
    ) -> Optional[dict]:
        """Load a tag the invoker may edit (owner or Manage Messages)."""
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)
        cleaned = _clean_name(name)
        row = await _find_tag(guild.id, cleaned) if cleaned else None
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(f"I could not find a tag called `{name}`."), ephemeral=True
            )
            return None
        is_owner = int(row["owner_id"]) == interaction.user.id
        if not is_owner and not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                embed=error_embed("Only the tag owner or a member with **Manage Messages** can do that."),
                ephemeral=True,
            )
            return None
        return row


async def setup(bot: commands.Bot) -> None:
    """Load the custom commands cog."""
    await bot.add_cog(CustomCommands(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart (none here)."""
    return []
