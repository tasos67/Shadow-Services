"""Core commands: /ping, /help, /botinfo, /serverinfo, /userinfo, /avatar,
/settings and the guided /setup wizard."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from utils import checks
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import format_duration, format_ts, truncate
from utils.paginator import paginate
from utils.views import ConfirmView

log = logging.getLogger("cogs.core")

# ---------------------------------------------------------------------------
# Help categorisation: cog module stem -> (emoji, title, description)
# ---------------------------------------------------------------------------
HELP_CATEGORIES: dict[str, tuple[str, str, str]] = {
    "core": ("🧭", "General", "Basic bot information and help."),
    "moderation": ("🛡️", "Moderation", "Bans, kicks, timeouts, warnings and case tracking."),
    "logs": ("📜", "Logging", "Message, member, channel, role and voice logs."),
    "tickets": ("🎫", "Tickets", "Private support tickets with transcripts and ratings."),
    "welcome": ("👋", "Welcome & Leave", "Welcome/goodbye messages, autorole and verification."),
    "reactionroles": ("🎭", "Reaction Roles", "Reaction, button and select-menu role panels."),
    "giveaways": ("🎉", "Giveaways", "Giveaways with requirements, rerolls and pauses."),
    "security": ("🔒", "Security", "Anti-spam, anti-raid, anti-nuke and auto-moderation."),
    "leveling": ("📈", "Leveling", "XP, rank cards, level roles and leaderboards."),
    "economy": ("💰", "Economy", "Currency, work, shop, gambling and leaderboards."),
    "customcommands": ("🤖", "Custom Commands", "Custom commands, tags and auto-responders."),
    "suggestions": ("💡", "Suggestions", "Suggestion boxes with staff review and voting."),
    "starboard": ("⭐", "Starboard", "Automatically highlight the best messages."),
    "tools": ("🛠️", "Tools", "Reminders, polls and AFK status."),
    "birthday": ("🎂", "Birthdays", "Per-server birthday registration and announcements."),
    "tempvoice": ("🔊", "Voice", "Temporary join-to-create voice channels."),
    "fun": ("🎲", "Fun & Utility", "Games, trivia, embed builder, announcements."),
    "misc": ("🧩", "Games & Extras", "Counting game, sticky messages and auto-publish."),
}


# ---------------------------------------------------------------------------
# /setup wizard infrastructure (cogs register their own categories)
# ---------------------------------------------------------------------------
@dataclass
class SetupCategory:
    """One step of the /setup wizard, provided by a cog."""

    key: str
    label: str
    emoji: str
    description: str
    # (input label, placeholder, required, dict key)
    fields: list[tuple[str, str, bool, str]]
    handler: Callable[[discord.Guild, dict[str, str]], Awaitable[str]]


SETUP_HANDLERS: dict[str, SetupCategory] = {}


def register_setup_category(category: SetupCategory) -> None:
    """Register a wizard category so /setup offers it."""
    SETUP_HANDLERS[category.key] = category


class SetupModal(discord.ui.Modal):
    """Modal collecting the inputs of one wizard category."""

    def __init__(self, category: SetupCategory) -> None:
        super().__init__(title=f"Setup • {category.label}", timeout=600)
        self.category = category
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for label, placeholder, required, key in category.fields[:5]:
            item = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                required=required,
                style=discord.TextStyle.short,
                max_length=250,
            )
            self.add_item(item)
            self.inputs[key] = item

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        values = {key: str(item.value or "").strip() for key, item in self.inputs.items()}
        try:
            summary = await self.category.handler(interaction.guild, values)
        except Exception:
            log.exception("Setup step %s failed", self.category.key)
            await interaction.response.send_message(
                embed=error_embed("Something went wrong while saving. Please check the values and try again."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(summary, title=f"{self.category.label} configured"), ephemeral=True
        )


class SetupSelect(discord.ui.Select):
    """Category picker for the setup wizard."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label=category.label,
                emoji=category.emoji,
                description=truncate(category.description, 100),
                value=category.key,
            )
            for category in SETUP_HANDLERS.values()
        ][:25]
        super().__init__(
            custom_id="setup:select",
            placeholder="Choose what you want to configure…",
            options=options or [discord.SelectOption(label="Nothing registered yet", value="none")],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category = SETUP_HANDLERS.get(self.values[0])
        if category is None:
            await interaction.response.send_message("Unknown category.", ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(category))


class SetupView(discord.ui.View):
    """Persistent view behind the /setup panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(SetupSelect())

    @discord.ui.button(label="Close", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="setup:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
def _module_of(command) -> str:
    callback = getattr(command, "callback", None)
    module = getattr(callback, "__module__", "") or ""
    return module.split(".")[-1]


def _collect_commands(bot: commands.Bot) -> list[tuple[str, str, str]]:
    """Flatten the command tree into (qualified name, description, module) rows."""
    rows: list[tuple[str, str, str]] = []
    for command in bot.tree.get_commands():
        if getattr(command, "hidden", False):
            continue
        if isinstance(command, app_commands.Group):
            subs = [c for c in command.commands if not getattr(c, "hidden", False)]
            for sub in subs:
                rows.append((f"{command.name} {sub.name}", sub.description or "No description.", _module_of(sub)))
        else:
            rows.append((command.name, command.description or "No description.", _module_of(command)))
    return rows


def _overview_embed(bot: commands.Bot) -> discord.Embed:
    rows = _collect_commands(bot)
    embed = base_embed(
        title=f"{config.BOT_NAME} — Help",
        description=(
            f"Hello {config.BOT_NAME} here! I am an all-in-one moderation, ticket, "
            "leveling and fun bot.\nPick a category below or use `/help <category>`.\n\n"
            "**Need help?** Ask your server admins or check the README."
        ),
    )
    for module, (emoji, title, description) in HELP_CATEGORIES.items():
        count = sum(1 for _, _, m in rows if m == module)
        if count:
            embed.add_field(
                name=f"{emoji} {title}",
                value=f"{description}\n*{count} command{'s' if count != 1 else ''}*",
                inline=True,
            )
    embed.add_field(name="Total commands", value=str(len(rows)), inline=True)
    return embed


def _category_pages(bot: commands.Bot, module: str) -> list[discord.Embed]:
    emoji, title, description = HELP_CATEGORIES.get(module, ("🔧", module.title(), "Commands."))
    rows = [r for r in _collect_commands(bot) if r[2] == module]
    pages: list[discord.Embed] = []
    per_page = 18
    for start in range(0, len(rows), per_page):
        chunk = rows[start : start + per_page]
        embed = base_embed(title=f"{emoji} {title} — commands", description=description)
        for name, desc, _ in chunk:
            embed.add_field(name=f"/{name}", value=truncate(desc, 120), inline=False)
        embed.set_footer(
            text=f"Page {start // per_page + 1}/{(len(rows) + per_page - 1) // per_page} • use /help for all categories"
        )
        pages.append(embed)
    if not pages:
        pages.append(base_embed(title=f"{emoji} {title}", description="No commands in this category yet."))
    return pages


class HelpSelect(discord.ui.Select):
    """Category picker for /help."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        modules = {m for _, _, m in _collect_commands(bot)}
        options = [discord.SelectOption(label="Overview", emoji="🏠", value="overview")]
        for module, (emoji, title, description) in HELP_CATEGORIES.items():
            if module in modules:
                options.append(
                    discord.SelectOption(
                        label=title, emoji=emoji, value=module, description=truncate(description, 100)
                    )
                )
        super().__init__(
            custom_id="help:select",
            placeholder="Choose a category…",
            options=options[:25],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "overview":
            embeds = [_overview_embed(self.bot)]
        else:
            embeds = _category_pages(self.bot, value)
        if len(embeds) > 1:
            from utils.paginator import Paginator

            view = Paginator(embeds, owner_id=interaction.user.id)
            view.page_label.label = f"1/{len(embeds)}"
            await interaction.response.edit_message(embed=embeds[0], view=view)
        else:
            self.placeholder = "Choose a category…"
            await interaction.response.edit_message(embed=embeds[0], view=self.view)


class HelpView(discord.ui.View):
    """Persistent view behind /help."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(HelpSelect(bot))

    @discord.ui.button(label="Home", emoji="🏠", style=discord.ButtonStyle.secondary, custom_id="help:home")
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_overview_embed(self.bot), view=self)


# ---------------------------------------------------------------------------
# The cog itself
# ---------------------------------------------------------------------------
class Core(commands.Cog):
    """General information and setup commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- /ping --------------------------------------------------------------
    @app_commands.command(description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Show websocket and API latency."""
        start = discord.utils.utcnow()
        await interaction.response.send_message(embed=info_embed("Pong!"))
        latency_ms = round(self.bot.latency * 1000)
        api_ms = round((discord.utils.utcnow() - start).total_seconds() * 1000)
        embed = info_embed(
            f"🏓 **WebSocket:** {latency_ms}ms\n🔁 **API response:** {api_ms}ms", title="Pong!"
        )
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException:
            pass

    # -- /help ---------------------------------------------------------------
    @app_commands.command(description="List all commands, grouped by category.")
    @app_commands.describe(category="Show only one category")
    async def help(self, interaction: discord.Interaction, category: Optional[str] = None) -> None:
        """Categorized help with a select menu."""
        view = HelpView(self.bot)
        if category:
            module = next(
                (m for m, (_, title, _) in HELP_CATEGORIES.items() if title.lower() == category.lower() or m == category.lower()),
                None,
            )
            if module is None:
                await interaction.response.send_message(
                    embed=error_embed(
                        "Unknown category. Available: "
                        + ", ".join(t for _, t, _ in HELP_CATEGORIES.values())
                    ),
                    ephemeral=True,
                )
                return
            pages = _category_pages(self.bot, module)
            if len(pages) > 1:
                await paginate(interaction, pages)
                return
            await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)
            return
        await interaction.response.send_message(embed=_overview_embed(self.bot), view=view, ephemeral=True)

    @help.autocomplete("category")
    async def _help_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        modules = {m for _, _, m in _collect_commands(self.bot)}
        current = current.lower()
        return [
            app_commands.Choice(name=f"{emoji} {title}", value=title)
            for module, (emoji, title, _) in HELP_CATEGORIES.items()
            if module in modules and current in title.lower()
        ][:25]

    # -- /botinfo ------------------------------------------------------------
    @app_commands.command(description="Show information about the bot.")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        """Bot statistics and versions."""
        import sys

        uptime = "starting up…"
        if self.bot.uptime:
            uptime = format_duration(int((discord.utils.utcnow() - self.bot.uptime).total_seconds()))
        owner = None
        try:
            app = await self.bot.application_info()
            owner = app.owner
        except discord.HTTPException:
            pass
        embed = base_embed(
            title=f"{config.BOT_NAME} — information",
            author=self.bot.user,
            thumbnail=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        embed.add_field(name="Owner", value=str(owner) if owner else "Unknown", inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Users", value=str(len(self.bot.users)), inline=True)
        embed.add_field(name="Commands", value=str(len(_collect_commands(self.bot))), inline=True)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(
            name="Versions",
            value=(
                f"Python `{sys.version.split()[0]}`\n"
                f"discord.py `{discord.__version__}`\n"
                f"Database `SQLite (aiosqlite)`"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # -- /serverinfo -----------------------------------------------------------
    @app_commands.command(description="Show information about this server.")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        """Overview of the current guild."""
        guild = interaction.guild
        assert guild is not None
        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        channels = guild.text_channels, guild.voice_channels, guild.stage_channels, guild.categories
        embed = base_embed(
            title=guild.name,
            description=guild.description or None,
            thumbnail=guild.icon.url if guild.icon else None,
        )
        embed.set_author(name="Server information", icon_url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Owner", value=str(guild.owner or guild.get_member(guild.owner_id) or "Unknown"), inline=True)
        embed.add_field(name="Created", value=f"{format_ts(int(guild.created_at.timestamp()))}", inline=True)
        embed.add_field(name="ID", value=str(guild.id), inline=True)
        embed.add_field(name="Members", value=f"{guild.member_count} ({humans} humans • {bots} bots)", inline=True)
        embed.add_field(
            name="Channels",
            value=f"{len(channels[0])} text • {len(channels[1]) + len(channels[2])} voice • {len(channels[3])} categories",
            inline=True,
        )
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(
            name="Emojis / Stickers",
            value=f"{len(guild.emojis)} / {len(guild.stickers)}",
            inline=True,
        )
        embed.add_field(
            name="Boosts",
            value=f"{guild.premium_subscription_count} (level {guild.premium_tier})",
            inline=True,
        )
        embed.add_field(
            name="Verification",
            value=str(guild.verification_level).replace("VerificationLevel.", "").title(),
            inline=True,
        )
        await interaction.response.send_message(embed=embed)

    # -- /userinfo ----------------------------------------------------------
    @app_commands.command(description="Show information about a member.")
    @app_commands.describe(member="The member to inspect (defaults to you)")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        """Account age, join date, roles and status of a member."""
        member = member or interaction.user
        assert isinstance(member, discord.Member)
        roles = [role.mention for role in member.roles if role != member.guild.default_role]
        roles_text = ", ".join(roles[:20]) or "None"
        if len(roles) > 20:
            roles_text += f" (+{len(roles) - 20} more)"
        permissions = member.guild_permissions
        key_perms = [
            name.replace("_", " ").title().replace("Guild", "Server")
            for name, value in permissions
            if value and name
            in {"administrator", "manage_guild", "manage_channels", "manage_messages", "ban_members", "kick_members", "manage_roles"}
        ]
        status = str(member.status).replace("Status.", "").title()
        embed = base_embed(
            title=str(member),
            thumbnail=member.display_avatar.url,
            color=member.color if member.color != discord.Color.default() else None,
        )
        embed.set_author(name=f"{member.display_name} • {status}", icon_url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Account created", value=format_ts(int(member.created_at.timestamp())), inline=True)
        embed.add_field(name="Joined", value=format_ts(int(member.joined_at.timestamp())) if member.joined_at else "Unknown", inline=True)
        embed.add_field(name="Highest role", value=member.top_role.mention, inline=True)
        embed.add_field(name="Boosting", value=format_ts(int(member.premium_since.timestamp())) if member.premium_since else "No", inline=True)
        embed.add_field(name=f"Roles ({len(roles)})", value=truncate(roles_text, 1024), inline=False)
        embed.add_field(name="Key permissions", value=", ".join(key_perms) or "None", inline=False)
        await interaction.response.send_message(embed=embed)

    # -- /avatar -----------------------------------------------------------
    @app_commands.command(description="Show a member's avatar.")
    @app_commands.describe(member="Whose avatar to show (defaults to you)")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.User] = None) -> None:
        """Display the full-size avatar of a user."""
        member = member or interaction.user
        avatar = member.display_avatar.with_size(4096)
        embed = base_embed(
            title=f"Avatar of {member}",
            image=avatar.url,
            color=config.THEME_COLOR,
        )
        embed.add_field(name="Links", value=f"[open in browser]({avatar.url})", inline=False)
        await interaction.response.send_message(embed=embed)

    # -- /settings ----------------------------------------------------------
    settings_group = app_commands.Group(
        name="settings",
        description="View or reset this server's bot settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @settings_group.command(name="view", description="Show every configured setting for this server.")
    async def settings_view(self, interaction: discord.Interaction) -> None:
        """Paginate through the stored settings."""
        assert interaction.guild is not None
        values = await database.db.get_all(interaction.guild.id)
        if not values:
            await interaction.response.send_message(
                embed=info_embed("No settings are configured yet. Run `/setup` to get started."), ephemeral=True
            )
            return
        pages = []
        items = sorted(values.items())
        for start in range(0, len(items), 10):
            embed = base_embed(title="Server settings", description=f"{len(items)} configured value(s)")
            for key, value in items[start : start + 10]:
                rendered = value if isinstance(value, str) else repr(value)
                embed.add_field(name=key, value=truncate(rendered, 500), inline=False)
            pages.append(embed)
        await paginate(interaction, pages)

    @settings_group.command(name="reset", description="Delete every stored setting for this server.")
    @app_commands.describe(confirm="Type RESET to confirm")
    async def settings_reset(self, interaction: discord.Interaction, confirm: str) -> None:
        """Erase all configuration for this guild (not tickets/messages/etc.)."""
        assert interaction.guild is not None
        if confirm.strip().upper() != "RESET":
            await interaction.response.send_message(
                embed=error_embed("Type `RESET` in the `confirm` argument to proceed."), ephemeral=True
            )
            return
        await database.db.clear_guild(interaction.guild.id)
        await interaction.response.send_message(
            embed=success_embed("All stored settings for this server have been deleted."), ephemeral=True
        )

    # -- /setup -------------------------------------------------------------
    @app_commands.command(description="Walk an administrator through the basic bot setup.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup(self, interaction: discord.Interaction) -> None:
        """Interactive configuration wizard."""
        if not SETUP_HANDLERS:
            await interaction.response.send_message(
                embed=error_embed("The setup wizard has no categories loaded yet."), ephemeral=True
            )
            return
        embed = base_embed(
            title="⚙️ Setup wizard",
            description=(
                "Pick a category below and fill in the form that opens.\n"
                "You can run this command as many times as you want.\n\n"
                + "\n".join(
                    f"{c.emoji} **{c.label}** — {c.description}" for c in SETUP_HANDLERS.values()
                )
            ),
        )
        await interaction.response.send_message(embed=embed, view=SetupView(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Load the core cog."""
    await bot.add_cog(Core(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [SetupView(), HelpView(bot)]
