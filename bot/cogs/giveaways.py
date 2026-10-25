"""Giveaway system: persistent join button, requirements (role / account age /
level), pause & resume, rerolls, blacklist and bypass roles, and automatic
ending that survives restarts (state lives in SQLite)."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import format_duration, format_ts, parse_duration, parse_id, remaining, truncate
from utils.paginator import paginate

log = logging.getLogger("cogs.giveaways")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def _participants(row: dict) -> list[int]:
    try:
        data = json.loads(row["participants"])
        return [int(x) for x in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _requirements(row: dict) -> dict:
    try:
        data = json.loads(row["requirements"])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _get_giveaway(giveaway_id: int) -> Optional[dict]:
    row = await database.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
    return dict(row) if row else None


def _giveaway_embed(row: dict, *, winner_text: Optional[str] = None) -> discord.Embed:
    """Build the live embed for a giveaway."""
    participants = _participants(row)
    req = _requirements(row)
    req_lines: list[str] = []
    if req.get("role_ids"):
        req_lines.append("• Roles: " + ", ".join(f"<@&{rid}>" for rid in req["role_ids"]))
    if req.get("min_account_age_days"):
        req_lines.append(f"• Account age: {req['min_account_age_days']} days")
    if req.get("min_level"):
        req_lines.append(f"• Level: {req['min_level']}+")
    embed = base_embed(
        title="🎉 Giveaway",
        description=(
            f"**Prize:** {row['prize']}\n"
            f"**Winners:** {row['winners']}\n"
            f"**Hosted by:** <@{row['host_id']}>\n"
        ),
        color=config.SUCCESS_COLOR,
    )
    if row["ended"]:
        embed.add_field(name="Status", value="Ended", inline=True)
    elif row["paused"]:
        embed.add_field(name="Status", value=f"Paused ({format_duration(row['paused_left'] or 0)} left)", inline=True)
    else:
        embed.add_field(name="Ends", value=f"{format_ts(int(row['ends_at']))} ({remaining(row['ends_at'])})", inline=True)
    embed.add_field(name="Entries", value=str(len(participants)), inline=True)
    if req_lines:
        embed.add_field(name="Requirements", value="\n".join(req_lines), inline=False)
    if winner_text:
        embed.add_field(name="Winners", value=winner_text, inline=False)
    embed.set_footer(text=f"Giveaway #{row['id']} • click the button to enter")
    return embed


async def _eligible(
    guild: discord.Guild, member: discord.Member, req: dict, bypass: set[int]
) -> tuple[bool, str]:
    """Check whether a member meets the giveaway requirements."""
    if not req:
        return True, ""
    if any(role.id in bypass for role in member.roles):
        return True, ""
    role_ids = req.get("role_ids") or []
    if role_ids and not any(role.id in role_ids for role in member.roles):
        missing = ", ".join(f"<@&{rid}>" for rid in role_ids)
        return False, f"You need one of these roles: {missing}"
    min_age = int(req.get("min_account_age_days") or 0)
    if min_age:
        age_days = (discord.utils.utcnow() - member.created_at).days
        if age_days < min_age:
            return False, f"Your account must be at least **{min_age} days** old."
    min_level = int(req.get("min_level") or 0)
    if min_level:
        row = await database.db.fetchone(
            "SELECT level FROM xp WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
        )
        level = int(row["level"]) if row else 0
        if level < min_level:
            return False, f"You need to be at least **level {min_level}** (you are level {level})."
    return True, ""


async def _pick_winners(guild: discord.Guild, row: dict) -> list[discord.Member]:
    """Pick random eligible winners."""
    participants = _participants(row)
    blacklist: list[int] = await database.db.get(guild.id, "giveaway_blacklist", [])
    bypass_ids = await database.db.get(guild.id, "giveaway_bypass_roles", [])
    bypass = set(int(x) for x in bypass_ids)
    req = _requirements(row)
    pool: list[discord.Member] = []
    for user_id in participants:
        if user_id in blacklist:
            continue
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        ok, _ = await _eligible(guild, member, req, bypass)
        if ok:
            pool.append(member)
    if not pool:
        return []
    count = min(int(row["winners"]), len(pool))
    return random.sample(pool, count)


async def _finish_giveaway(bot: commands.Bot, row: dict, *, reroll: bool = False, ended_by: Optional[int] = None) -> list[discord.Member]:
    """Announce winners, DM them and mark the giveaway as ended."""
    guild = bot.get_guild(int(row["guild_id"]))
    if guild is None:
        return []
    winners = await _pick_winners(guild, row)
    winner_text = ", ".join(w.mention for w in winners) if winners else "No valid entries 😢"

    channel = guild.get_channel(int(row["channel_id"]))
    message: Optional[discord.Message] = None
    if isinstance(channel, discord.TextChannel):
        try:
            message = await channel.fetch_message(int(row["message_id"]))
        except discord.HTTPException:
            message = None

    if winners:
        congrats = base_embed(
            title="🎉 Giveaway ended!",
            description=f"**Prize:** {row['prize']}\n**Winner(s):** {winner_text}",
            color=config.SUCCESS_COLOR,
        )
        if message is not None:
            try:
                await message.reply(content=", ".join(w.mention for w in winners), embed=congrats)
            except discord.HTTPException:
                pass
    else:
        if message is not None:
            try:
                await message.reply(embed=warn_embed("The giveaway ended but there were no valid entries."))
            except discord.HTTPException:
                pass

    if message is not None:
        try:
            await message.edit(
                embed=_giveaway_embed({**row, "ended": 1, "paused": 0}, winner_text=winner_text), view=None
            )
        except discord.HTTPException:
            pass

    for winner in winners:
        try:
            await winner.send(
                embed=base_embed(
                    title="🎉 You won a giveaway!",
                    description=f"You won **{row['prize']}** in **{guild.name}**!\nMessage the host to claim your prize.",
                    color=config.SUCCESS_COLOR,
                )
            )
        except discord.HTTPException:
            pass

    await database.db.execute(
        "UPDATE giveaways SET ended = 1, paused = 0, ended_by = ? WHERE id = ?",
        (ended_by if ended_by is not None else row.get("ended_by"), row["id"]),
    )
    return winners


async def _apply_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save giveaway settings from the /setup wizard."""
    lines: list[str] = []
    bypass_ids = [rid for part in values.get("bypass_roles", "").replace(",", " ").split() if (rid := parse_id(part))]
    if bypass_ids:
        await database.db.set(guild.id, "giveaway_bypass_roles", bypass_ids)
        lines.append("Bypass roles: " + ", ".join(f"<@&{rid}>" for rid in bypass_ids))
    blacklist = [uid for part in values.get("blacklist", "").replace(",", " ").split() if (uid := parse_id(part))]
    if blacklist:
        await database.db.set(guild.id, "giveaway_blacklist", blacklist)
        lines.append("Blacklist: " + ", ".join(f"<@{uid}>" for uid in blacklist))
    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="giveaways",
        label="Giveaways",
        emoji="🎉",
        description="Bypass roles and blacklisted users.",
        fields=[
            ("Bypass roles", "@Staff @Booster (optional)", False, "bypass_roles"),
            ("Blacklisted users", "@user or IDs (optional)", False, "blacklist"),
        ],
        handler=_apply_settings,
    )
)


# ---------------------------------------------------------------------------
# Persistent join button
# ---------------------------------------------------------------------------
class GiveawayJoinButton(discord.ui.Button):
    """Join / leave a giveaway."""

    def __init__(self) -> None:
        super().__init__(
            label="Enter giveaway", emoji="🎉", style=discord.ButtonStyle.success, custom_id="giveaway:join"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or interaction.message is None:
            await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
            return
        row = await database.db.fetchone("SELECT * FROM giveaways WHERE message_id = ?", (interaction.message.id,))
        if row is None:
            await interaction.response.send_message(
                embed=error_embed("This giveaway no longer exists."), ephemeral=True
            )
            return
        giveaway = dict(row)
        if giveaway["ended"]:
            await interaction.response.send_message(embed=error_embed("This giveaway has ended."), ephemeral=True)
            return
        if giveaway["paused"]:
            await interaction.response.send_message(
                embed=error_embed("This giveaway is currently paused. Try again later."), ephemeral=True
            )
            return
        blacklist: list[int] = await database.db.get(guild.id, "giveaway_blacklist", [])
        if member.id in blacklist:
            await interaction.response.send_message(
                embed=error_embed("You are blacklisted from joining giveaways on this server."), ephemeral=True
            )
            return
        bypass_ids = set(int(x) for x in await database.db.get(guild.id, "giveaway_bypass_roles", []))
        ok, reason = await _eligible(guild, member, _requirements(giveaway), bypass_ids)
        if not ok:
            await interaction.response.send_message(embed=error_embed(reason), ephemeral=True)
            return

        participants = _participants(giveaway)
        if member.id in participants:
            participants.remove(member.id)
            action = "left"
        else:
            participants.append(member.id)
            action = "joined"
        await database.db.execute(
            "UPDATE giveaways SET participants = ? WHERE id = ?", (json.dumps(participants), giveaway["id"])
        )
        giveaway["participants"] = json.dumps(participants)
        await interaction.response.send_message(
            embed=success_embed(f"You **{action}** the giveaway for **{giveaway['prize']}**.", title="Giveaway"),
            ephemeral=True,
        )
        try:
            await interaction.message.edit(embed=_giveaway_embed(giveaway))
        except discord.HTTPException:
            pass


class GiveawayJoinView(discord.ui.View):
    """Persistent view containing the giveaway join button."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(GiveawayJoinButton())


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Giveaways(commands.Cog):
    """Giveaway commands."""

    giveaway_group = app_commands.Group(name="giveaway", description="Giveaway system.", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- start ---------------------------------------------------------------
    @giveaway_group.command(name="start", description="Start a new giveaway.")
    @app_commands.describe(
        prize="What is being given away",
        duration="How long it runs, e.g. 2h, 1d, 30m",
        winners="How many winners",
        channel="Channel for the giveaway embed",
        required_roles="Roles the winner must have (mentions/IDs, space separated)",
        min_account_age="Minimum account age in days",
        min_level="Minimum level (0 = none)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_start(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 100] = 1,
        channel: Optional[discord.TextChannel] = None,
        required_roles: Optional[str] = None,
        min_account_age: app_commands.Range[int, 0, 3650] = 0,
        min_level: app_commands.Range[int, 0, 1000] = 0,
    ) -> None:
        """Create a giveaway that ends automatically."""
        guild = interaction.guild
        assert guild is not None
        seconds = parse_duration(duration)
        if not seconds or seconds < 30:
            await interaction.response.send_message(
                embed=error_embed("Please give a valid duration of at least 30 seconds, e.g. `10m`, `2h`, `1d`."),
                ephemeral=True,
            )
            return
        channel = channel or interaction.channel  # type: ignore[assignment]
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(embed=error_embed("Pick a text channel."), ephemeral=True)
            return

        role_ids = [rid for part in (required_roles or "").replace(",", " ").split() if (rid := parse_id(part))]
        requirements = {
            "role_ids": role_ids,
            "min_account_age_days": int(min_account_age),
            "min_level": int(min_level),
        }
        ends_at = int(time.time()) + seconds
        await interaction.response.defer(ephemeral=True)
        placeholder_row = {
            "id": 0,
            "prize": prize,
            "winners": winners,
            "host_id": interaction.user.id,
            "ends_at": ends_at,
            "ended": 0,
            "paused": 0,
            "paused_left": None,
            "participants": "[]",
            "requirements": json.dumps(requirements),
        }
        try:
            message = await channel.send(embed=_giveaway_embed(placeholder_row), view=GiveawayJoinView())
        except discord.HTTPException:
            await interaction.followup.send(
                embed=error_embed("I could not post the giveaway there. Check my permissions."), ephemeral=True
            )
            return
        giveaway_id = await database.db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, prize, host_id, winners, ends_at, participants, requirements) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?)",
            (guild.id, channel.id, message.id, prize, interaction.user.id, winners, ends_at, json.dumps(requirements)),
        )
        row = await _get_giveaway(giveaway_id)
        if row is not None:
            try:
                await message.edit(embed=_giveaway_embed(row))
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            embed=success_embed(f"Giveaway **#{giveaway_id}** started in {channel.mention}."), ephemeral=True
        )

    # -- end / reroll / pause ------------------------------------------------
    @giveaway_group.command(name="end", description="End a giveaway immediately.")
    @app_commands.describe(giveaway_id="Giveaway ID", winner_count="Only pick this many winners (optional)")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_end(
        self, interaction: discord.Interaction, giveaway_id: int, winner_count: Optional[int] = None
    ) -> None:
        """Finish a giveaway and draw winners."""
        row = await _get_giveaway(giveaway_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(embed=error_embed("Giveaway not found."), ephemeral=True)
            return
        if row["ended"]:
            await interaction.response.send_message(embed=error_embed("That giveaway has already ended."), ephemeral=True)
            return
        if winner_count:
            await database.db.execute("UPDATE giveaways SET winners = ? WHERE id = ?", (winner_count, giveaway_id))
            row["winners"] = winner_count
        await interaction.response.defer(ephemeral=True)
        winners = await _finish_giveaway(self.bot, row, ended_by=interaction.user.id)
        text = ", ".join(w.mention for w in winners) if winners else "no valid entries"
        await interaction.followup.send(
            embed=success_embed(f"Giveaway **#{giveaway_id}** ended. Winners: {text}"), ephemeral=True
        )

    @giveaway_group.command(name="reroll", description="Draw new winners for an ended giveaway.")
    @app_commands.describe(giveaway_id="Giveaway ID", winner_count="How many new winners")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_reroll(
        self, interaction: discord.Interaction, giveaway_id: int, winner_count: app_commands.Range[int, 1, 100] = 1
    ) -> None:
        """Pick fresh winners without reopening the giveaway."""
        row = await _get_giveaway(giveaway_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(embed=error_embed("Giveaway not found."), ephemeral=True)
            return
        row["winners"] = winner_count
        row["ended"] = 1
        await interaction.response.defer(ephemeral=True)
        winners = await _finish_giveaway(self.bot, row, reroll=True, ended_by=interaction.user.id)
        text = ", ".join(w.mention for w in winners) if winners else "no valid entries"
        await interaction.followup.send(
            embed=success_embed(f"Rerolled giveaway **#{giveaway_id}**. New winners: {text}"), ephemeral=True
        )

    @giveaway_group.command(name="pause", description="Pause a running giveaway.")
    @app_commands.describe(giveaway_id="Giveaway ID")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_pause(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        """Freeze the countdown."""
        row = await _get_giveaway(giveaway_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(embed=error_embed("Giveaway not found."), ephemeral=True)
            return
        if row["ended"] or row["paused"]:
            await interaction.response.send_message(
                embed=error_embed("That giveaway is already ended or paused."), ephemeral=True
            )
            return
        left = max(0, int(row["ends_at"] - time.time()))
        await database.db.execute(
            "UPDATE giveaways SET paused = 1, paused_left = ? WHERE id = ?", (left, giveaway_id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Giveaway **#{giveaway_id}** paused with **{format_duration(left)}** left."),
            ephemeral=True,
        )

    @giveaway_group.command(name="resume", description="Resume a paused giveaway.")
    @app_commands.describe(giveaway_id="Giveaway ID")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_resume(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        """Restart the countdown from where it was paused."""
        row = await _get_giveaway(giveaway_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(embed=error_embed("Giveaway not found."), ephemeral=True)
            return
        if not row["paused"] or row["ended"]:
            await interaction.response.send_message(embed=error_embed("That giveaway is not paused."), ephemeral=True)
            return
        new_end = int(time.time()) + int(row["paused_left"] or 0)
        await database.db.execute(
            "UPDATE giveaways SET paused = 0, paused_left = NULL, ends_at = ? WHERE id = ?", (new_end, giveaway_id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Giveaway **#{giveaway_id}** resumed — ends {format_ts(new_end, 'R')}."),
            ephemeral=True,
        )

    @giveaway_group.command(name="delete", description="Delete a giveaway from the database.")
    @app_commands.describe(giveaway_id="Giveaway ID")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_delete(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        """Remove a giveaway and its message."""
        row = await _get_giveaway(giveaway_id)
        if row is None or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message(embed=error_embed("Giveaway not found."), ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        channel = guild.get_channel(int(row["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(int(row["message_id"]))
                await message.delete()
            except discord.HTTPException:
                pass
        await database.db.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))
        await interaction.response.send_message(embed=success_embed(f"Giveaway **#{giveaway_id}** deleted."), ephemeral=True)

    @giveaway_group.command(name="list", description="List giveaways on this server.")
    @app_commands.describe(active_only="Only show running giveaways")
    async def giveaway_list(self, interaction: discord.Interaction, active_only: bool = True) -> None:
        """Paginated list of giveaways."""
        guild = interaction.guild
        assert guild is not None
        sql = "SELECT * FROM giveaways WHERE guild_id = ?"
        if active_only:
            sql += " AND ended = 0"
        sql += " ORDER BY id DESC"
        rows = await database.db.fetchall(sql, (guild.id,))
        if not rows:
            await interaction.response.send_message(embed=info_embed("No giveaways found."), ephemeral=True)
            return
        pages = []
        for start in range(0, len(rows), 5):
            embed = base_embed(title="🎉 Giveaways")
            for row in rows[start : start + 5]:
                data = dict(row)
                status = "ended" if data["ended"] else ("paused" if data["paused"] else f"ends {format_ts(int(data['ends_at']), 'R')}")
                embed.add_field(
                    name=f"#{data['id']} • {truncate(data['prize'], 60)}",
                    value=(
                        f"Status: {status}\nEntries: {len(_participants(data))}\n"
                        f"Winners: {data['winners']}\n[jump](https://discord.com/channels/{guild.id}/{data['channel_id']}/{data['message_id']})"
                    ),
                    inline=False,
                )
            pages.append(embed)
        await paginate(interaction, pages)

    # -- blacklist / bypass --------------------------------------------------
    @giveaway_group.command(name="blacklist", description="Add or remove a user from the giveaway blacklist.")
    @app_commands.describe(member="User to blacklist or unblacklist", remove="True to remove them from the blacklist")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_blacklist(
        self, interaction: discord.Interaction, member: discord.Member, remove: bool = False
    ) -> None:
        """Manage the blacklist."""
        guild = interaction.guild
        assert guild is not None
        current: list[int] = [int(x) for x in await database.db.get(guild.id, "giveaway_blacklist", [])]
        if remove:
            current = [uid for uid in current if uid != member.id]
            action = "removed from"
        else:
            if member.id not in current:
                current.append(member.id)
            action = "added to"
        await database.db.set(guild.id, "giveaway_blacklist", current)
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} was {action} the giveaway blacklist."), ephemeral=True
        )

    @giveaway_group.command(name="bypass", description="Add or remove a bypass role for giveaway requirements.")
    @app_commands.describe(role="Role to add or remove", remove="True to remove the bypass")
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_bypass(self, interaction: discord.Interaction, role: discord.Role, remove: bool = False) -> None:
        """Manage bypass roles."""
        guild = interaction.guild
        assert guild is not None
        current: list[int] = [int(x) for x in await database.db.get(guild.id, "giveaway_bypass_roles", [])]
        if remove:
            current = [rid for rid in current if rid != role.id]
            action = "removed from"
        else:
            if role.id not in current:
                current.append(role.id)
            action = "added to"
        await database.db.set(guild.id, "giveaway_bypass_roles", current)
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} was {action} the bypass roles."), ephemeral=True
        )

    # -- background loop -----------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Ensure the giveaway checker keeps running after restarts."""
        if not self.finish_loop.is_running():
            self.finish_loop.start()

    @tasks.loop(seconds=20)
    async def finish_loop(self) -> None:
        """End expired giveaways (works across restarts)."""
        now = int(time.time())
        rows = await database.db.fetchall(
            "SELECT * FROM giveaways WHERE ended = 0 AND paused = 0 AND ends_at <= ?", (now,)
        )
        for row in rows:
            data = dict(row)
            try:
                await _finish_giveaway(self.bot, data)
            except Exception:
                log.exception("Failed to finish giveaway %s", data["id"])

    @finish_loop.before_loop
    async def before_finish_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the giveaways cog."""
    await bot.add_cog(Giveaways(bot))


def persistent_views(bot: commands.Bot) -> list[discord.ui.View]:
    """Views that must keep working after a restart."""
    return [GiveawayJoinView()]
