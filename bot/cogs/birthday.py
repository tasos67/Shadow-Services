"""Birthday tracker: per-guild birthday announcements with an optional role
and a configurable message template, driven by a daily background check."""
from __future__ import annotations

import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed
from utils.helpers import parse_id, placeholder_format, truncate
from utils.paginator import paginate

log = logging.getLogger("cogs.birthday")

DEFAULT_MESSAGE = "🎂 Happy birthday {user}! Have a great day!"
MAX_AGE = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _today() -> datetime.date:
    """Return the current UTC date."""
    return discord.utils.utcnow().date()


def _validate(day: int, month: int) -> bool:
    """Return True when the day/month combination exists (Feb 29 allowed)."""
    try:
        datetime.date(2004, month, day)  # leap year so 29/02 is accepted
    except ValueError:
        return False
    return True


def _next_occurrence(day: int, month: int, today: datetime.date) -> datetime.date:
    """Return the next date on which this birthday occurs."""
    try:
        candidate = datetime.date(today.year, month, day)
    except ValueError:
        candidate = datetime.date(today.year, 3, 1)  # 29 Feb in a non-leap year
    if candidate < today:
        try:
            candidate = datetime.date(today.year + 1, month, day)
        except ValueError:
            candidate = datetime.date(today.year + 1, 3, 1)
    return candidate


def _format_message(template: str, member: discord.Member, guild: discord.Guild, year: Optional[int]) -> str:
    """Apply placeholders, adding ``{age}`` when a birth year is known."""
    age = ""
    if year:
        age = str(_today().year - int(year))
    return placeholder_format(template, member=member, guild=guild, extra={"age": age})


async def _apply_birthday_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save the birthday settings coming from the setup wizard / modal."""
    lines: list[str] = []
    channel_id = parse_id(values.get("birthday_channel", ""))
    if channel_id:
        await database.db.set(guild.id, "birthday_channel", channel_id)
        await database.db.set(guild.id, "birthday_enabled", True)
        lines.append(f"Birthday channel: <#{channel_id}>")
    message = values.get("birthday_message", "").strip()
    if message:
        await database.db.set(guild.id, "birthday_message", truncate(message, 1500))
        lines.append(f"Birthday message: {truncate(message, 100)}")
    role_id = parse_id(values.get("birthday_role", ""))
    if role_id:
        await database.db.set(guild.id, "birthday_role", role_id)
        lines.append(f"Birthday role: <@&{role_id}>")
    return "\n".join(lines) or "No changes were made."


def _register_wizard() -> None:
    """Register the birthday step of the /setup wizard."""
    register_setup_category(
        SetupCategory(
            key="birthday",
            label="Birthdays",
            emoji="🎂",
            description="Birthday channel, message template and celebration role.",
            fields=[
                ("Birthday channel", "#birthdays", False, "birthday_channel"),
                (
                    "Birthday message",
                    "🎂 Happy birthday {user}! Have a great day!",
                    False,
                    "birthday_message",
                ),
                ("Birthday role", "@Birthday (optional)", False, "birthday_role"),
            ],
            handler=_apply_birthday_settings,
        )
    )


_register_wizard()


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Birthday(commands.Cog):
    """Birthday registration, listing and announcements."""

    birthday_group = app_commands.Group(
        name="birthday", description="Birthday tracker configuration.", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        self.birthday_loop.cancel()

    # -- commands ------------------------------------------------------------
    @birthday_group.command(name="set", description="Register your birthday so the server can celebrate it.")
    @app_commands.describe(
        day="Day of the month (1-31)",
        month="Month (1-12)",
        year="Your birth year (optional — used to show your age)",
    )
    async def birthday_set(
        self,
        interaction: discord.Interaction,
        day: app_commands.Range[int, 1, 31],
        month: app_commands.Range[int, 1, 12],
        year: Optional[int] = None,
    ) -> None:
        """Store the invoker's birthday."""
        guild = interaction.guild
        assert guild is not None
        if not _validate(int(day), int(month)):
            await interaction.response.send_message(
                embed=error_embed(f"**{day}/{month}** is not a valid date."), ephemeral=True
            )
            return
        if year is not None:
            age = _today().year - int(year)
            if age < 0:
                await interaction.response.send_message(
                    embed=error_embed("That year is in the future."), ephemeral=True
                )
                return
            if age > MAX_AGE:
                await interaction.response.send_message(
                    embed=error_embed(f"That would make you **{age}** years old — please check the year."),
                    ephemeral=True,
                )
                return
        await database.db.execute(
            "INSERT INTO birthdays (guild_id, user_id, day, month, year) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET day = excluded.day, month = excluded.month, "
            "year = excluded.year",
            (guild.id, interaction.user.id, int(day), int(month), int(year) if year is not None else None),
        )
        suffix = f" in {year}" if year is not None else ""
        await interaction.response.send_message(
            embed=success_embed(f"Your birthday is now set to **{day}/{month}**{suffix}."), ephemeral=True
        )

    @birthday_group.command(name="remove", description="Remove your registered birthday.")
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        """Delete the invoker's birthday row."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT 1 FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild.id, interaction.user.id)
        )
        if row is None:
            await interaction.response.send_message(
                embed=info_embed("You have not registered a birthday."), ephemeral=True
            )
            return
        await database.db.execute(
            "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild.id, interaction.user.id)
        )
        await interaction.response.send_message(embed=success_embed("Your birthday was removed."), ephemeral=True)

    @birthday_group.command(name="next", description="Show the next upcoming birthdays on this server.")
    async def birthday_next(self, interaction: discord.Interaction) -> None:
        """List the five closest upcoming birthdays."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall("SELECT * FROM birthdays WHERE guild_id = ?", (guild.id,))
        today = _today()
        entries: list[tuple[datetime.date, discord.Member]] = []
        for row in rows:
            member = guild.get_member(int(row["user_id"]))
            if member is None:
                continue
            entries.append((_next_occurrence(int(row["day"]), int(row["month"]), today), member))
        if not entries:
            await interaction.response.send_message(
                embed=info_embed("No birthdays are registered yet."), ephemeral=True
            )
            return
        entries.sort(key=lambda item: item[0])
        embed = base_embed(title="🎂 Upcoming birthdays")
        for when, member in entries[:5]:
            days = (when - today).days
            relative = "today 🎉" if days == 0 else f"in {days} day(s)"
            embed.add_field(
                name=member.display_name,
                value=f"{when.strftime('%d %B')} • {relative}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @birthday_group.command(name="list", description="List every birthday registered on this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        """Paginated list of all birthdays in the guild."""
        guild = interaction.guild
        assert guild is not None
        rows = await database.db.fetchall(
            "SELECT * FROM birthdays WHERE guild_id = ? ORDER BY month, day", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No birthdays are registered yet."), ephemeral=True
            )
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(rows), 10):
            embed = base_embed(title="🎂 Registered birthdays")
            for row in rows[start : start + 10]:
                year = f" • {row['year']}" if row["year"] else ""
                embed.add_field(
                    name=f"<@{row['user_id']}>",
                    value=f"{int(row['day']):02d}/{int(row['month']):02d}{year}",
                    inline=True,
                )
            embed.set_footer(text=f"{len(rows)} birthday(s)")
            pages.append(embed)
        await paginate(interaction, pages)

    @birthday_group.command(name="preview", description="Preview the configured birthday message.")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_preview(self, interaction: discord.Interaction) -> None:
        """Post the configured birthday message for the invoker."""
        guild = interaction.guild
        assert guild is not None
        template = await database.db.get(guild.id, "birthday_message", DEFAULT_MESSAGE)
        member = interaction.user
        assert isinstance(member, discord.Member)
        text = _format_message(str(template), member, guild, None)
        await interaction.response.send_message(content=text, allowed_mentions=discord.AllowedMentions(users=True))

    @birthday_group.command(name="channel", description="Set the channel where birthday messages are posted.")
    @app_commands.describe(channel="Channel for birthday announcements")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Enable announcements in the given channel."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "birthday_channel", channel.id)
        await database.db.set(interaction.guild.id, "birthday_enabled", True)
        await interaction.response.send_message(
            embed=success_embed(f"Birthday announcements will be posted in {channel.mention}."), ephemeral=True
        )

    @birthday_group.command(name="message", description="Set the birthday message template.")
    @app_commands.describe(message="Use {user}, {server}, {age}, {membercount} placeholders")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_message(self, interaction: discord.Interaction, message: str) -> None:
        """Store the birthday message template."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "birthday_message", truncate(message, 1500))
        await interaction.response.send_message(
            embed=success_embed(f"Birthday message saved:\n{truncate(message, 400)}"), ephemeral=True
        )

    @birthday_group.command(name="role", description="Set an optional role given on members' birthdays.")
    @app_commands.describe(role="Role handed out on the birthday (removed the next day)")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Store the celebration role."""
        guild = interaction.guild
        assert guild is not None
        if role >= guild.me.top_role:
            await interaction.response.send_message(
                embed=error_embed("That role is higher than my highest role, so I could not manage it."),
                ephemeral=True,
            )
            return
        await database.db.set(guild.id, "birthday_role", role.id)
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} will be given to members on their birthday."), ephemeral=True
        )

    @birthday_group.command(name="toggle", description="Enable or disable birthday announcements.")
    @app_commands.describe(enabled="Whether birthday announcements are sent")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the birthday system."""
        assert interaction.guild is not None
        await database.db.set(interaction.guild.id, "birthday_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Birthday announcements are now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- background task -----------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the birthday check loop after a restart."""
        if not self.birthday_loop.is_running():
            self.birthday_loop.start()

    @tasks.loop(minutes=30)
    async def birthday_loop(self) -> None:
        """Announce today's birthdays once per guild per day."""
        today = _today()
        today_str = today.isoformat()
        yesterday = today - datetime.timedelta(days=1)
        for guild in list(self.bot.guilds):
            try:
                await self._process_guild(guild, today, today_str, yesterday)
            except Exception:
                log.exception("Birthday check failed for guild %s", guild.id)

    @birthday_loop.before_loop
    async def before_birthday_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_guild(
        self,
        guild: discord.Guild,
        today: datetime.date,
        today_str: str,
        yesterday: datetime.date,
    ) -> None:
        """Announce today's birthdays and clean up yesterday's role."""
        if not await database.db.get_bool(guild.id, "birthday_enabled", False):
            return
        channel_id = await database.db.get(guild.id, "birthday_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        if await database.db.get(guild.id, "birthday_last_run") == today_str:
            return
        # write the guard immediately so a guild is only processed once per day
        await database.db.set(guild.id, "birthday_last_run", today_str)

        role_id = await database.db.get(guild.id, "birthday_role")
        role = guild.get_role(int(role_id)) if role_id else None
        template = str(await database.db.get(guild.id, "birthday_message", DEFAULT_MESSAGE))

        if role is not None:
            previous = await database.db.fetchall(
                "SELECT * FROM birthdays WHERE guild_id = ? AND day = ? AND month = ?",
                (guild.id, yesterday.day, yesterday.month),
            )
            for row in previous:
                member = guild.get_member(int(row["user_id"]))
                if member is None or role not in member.roles:
                    continue
                try:
                    await member.remove_roles(role, reason="Birthday role cleanup")
                except discord.HTTPException:
                    log.warning("Could not remove birthday role from %s", member)

        rows = await database.db.fetchall(
            "SELECT * FROM birthdays WHERE guild_id = ? AND day = ? AND month = ?",
            (guild.id, today.day, today.month),
        )
        for row in rows:
            member = guild.get_member(int(row["user_id"]))
            if member is None:
                continue
            text = _format_message(template, member, guild, int(row["year"]) if row["year"] else None)
            try:
                await channel.send(content=text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                log.warning("Could not send birthday message in guild %s", guild.id)
            if role is not None and role < guild.me.top_role:
                try:
                    await member.add_roles(role, reason="Happy birthday!")
                except discord.HTTPException:
                    log.warning("Could not add birthday role to %s", member)


async def setup(bot: commands.Bot) -> None:
    """Load the birthday cog."""
    await bot.add_cog(Birthday(bot))
