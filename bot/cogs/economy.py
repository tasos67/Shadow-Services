"""Economy system: wallet & bank balances, daily rewards, work, begging,
transfers, a server shop with inventory and gambling games.

Money cooldowns use the persisted ``economy`` timestamps so they survive
restarts; :func:`utils.helpers.cooldown` is only used for transient rate limits.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.core import SetupCategory, register_setup_category
from database import db
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import cooldown as rate_limit
from utils.helpers import format_duration, format_ts, truncate
from utils.paginator import Paginator, paginate

log = logging.getLogger("cogs.economy")

DEFAULT_CURRENCY = "coins"
DEFAULT_SYMBOL = "🪙"
DEFAULT_DAILY = 100
DEFAULT_WORK_MIN = 50
DEFAULT_WORK_MAX = 150
DEFAULT_WORK_COOLDOWN = 3600
DEFAULT_DAILY_COOLDOWN = 86400
DEFAULT_START_BALANCE = 100
DEFAULT_GAMBLE_MAX = 1000
BEG_COOLDOWN = 300
PAY_COOLDOWN = 30
LEADERBOARD_PAGE_SIZE = 10
STREAK_WINDOW = 48 * 3600

WORK_JOBS = [
    "You worked a double shift at the diner",
    "You delivered pizzas across town",
    "You walked a pack of very happy dogs",
    "You fixed a friend's buggy code",
    "You bartended at the local pub",
    "You worked the front desk at a hotel",
    "You mowed a few lawns this afternoon",
    "You helped a neighbour move apartments",
    "You tutored students after school",
    "You freelance-wrote a handful of articles",
    "You restocked shelves at the grocery store",
    "You drove a taxi through rush hour",
]

BEG_FLAVOURS = [
    "A kind stranger dropped a few coins in your cup.",
    "A passerby took pity on you and handed you some change.",
    "You found loose change behind the sofa cushions.",
    "A friendly traveller gave you a small tip.",
    "You returned a lost wallet and earned a modest reward.",
    "Someone bought you a snack and left the change.",
]

SLOTS_REEL = [("🍋", 34), ("🍒", 26), ("🔔", 18), ("⭐", 13), ("7️⃣", 9)]
SLOTS_PAYOUT = {"🍋": 3.0, "🍒": 5.0, "🔔": 10.0, "⭐": 15.0, "7️⃣": 25.0}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
async def _get_account(guild_id: int, user_id: int) -> dict:
    """Return a member's economy row, creating it with the start balance."""
    start = await db.get_int(guild_id, "economy_start_balance", DEFAULT_START_BALANCE)
    await db.execute(
        "INSERT OR IGNORE INTO economy "
        "(guild_id, user_id, cash, bank, daily_streak, last_daily, last_work, last_beg) "
        "VALUES (?, ?, ?, 0, 0, 0, 0, 0)",
        (guild_id, user_id, start),
    )
    row = await db.fetchone(
        "SELECT * FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )
    if row is not None:
        return dict(row)
    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "cash": start,
        "bank": 0,
        "daily_streak": 0,
        "last_daily": 0,
        "last_work": 0,
        "last_beg": 0,
    }


async def _update_cash(guild_id: int, user_id: int, delta: int) -> None:
    """Adjust a member's wallet by ``delta`` (callers validate funds first)."""
    if delta == 0:
        return
    await db.execute(
        "UPDATE economy SET cash = cash + ? WHERE guild_id = ? AND user_id = ?",
        (delta, guild_id, user_id),
    )


async def _update_bank(guild_id: int, user_id: int, delta: int) -> None:
    """Adjust a member's bank by ``delta`` (callers validate funds first)."""
    if delta == 0:
        return
    await db.execute(
        "UPDATE economy SET bank = bank + ? WHERE guild_id = ? AND user_id = ?",
        (delta, guild_id, user_id),
    )


async def _currency(guild_id: int) -> tuple[str, str]:
    """Return the configured ``(name, symbol)`` for a guild."""
    name = str(await db.get(guild_id, "economy_currency", DEFAULT_CURRENCY))
    symbol = str(await db.get(guild_id, "economy_symbol", DEFAULT_SYMBOL))
    return name, symbol


async def _find_shop_item(guild_id: int, item: str) -> Optional[dict]:
    """Look up a shop item by exact name, then case-insensitively."""
    row = await db.fetchone(
        "SELECT * FROM shop_items WHERE guild_id = ? AND item = ?", (guild_id, item)
    )
    if row is None:
        row = await db.fetchone(
            "SELECT * FROM shop_items WHERE guild_id = ? AND LOWER(item) = LOWER(?)", (guild_id, item)
        )
    return dict(row) if row else None


def _parse_amount(text: str, available: int) -> Optional[int]:
    """Parse a numeric amount or the keywords ``all``/``max``/``*``."""
    cleaned = (text or "").strip().lower()
    if cleaned in {"all", "max", "*"}:
        return available
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_range(text: str) -> Optional[tuple[int, int]]:
    """Parse a ``min/max`` pair (also accepts spaces, commas or a dash)."""
    if not text:
        return None
    cleaned = text.replace("/", " ").replace(",", " ").replace("-", " ")
    parts = [part for part in cleaned.split() if part.isdigit()]
    if len(parts) < 2:
        return None
    first, second = int(parts[0]), int(parts[1])
    if first <= 0 or second <= 0:
        return None
    return (min(first, second), max(first, second))


async def _economy_enabled(guild_id: int) -> bool:
    """Whether the economy system is enabled for a guild."""
    return await db.get_bool(guild_id, "economy_enabled", True)


async def _apply_economy_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Save economy settings from the /setup wizard and return a summary."""
    lines: list[str] = []

    currency = values.get("currency", "").strip()
    if currency:
        await db.set(guild.id, "economy_currency", truncate(currency, 32))
        lines.append(f"Currency name: **{truncate(currency, 32)}**")

    symbol = values.get("symbol", "").strip()
    if symbol:
        await db.set(guild.id, "economy_symbol", truncate(symbol, 8))
        lines.append(f"Currency symbol: {truncate(symbol, 8)}")

    daily = values.get("daily", "").strip()
    if daily.isdigit() and int(daily) > 0:
        await db.set(guild.id, "economy_daily", int(daily))
        lines.append(f"Daily amount: **{int(daily):,}**")

    start = values.get("start_balance", "").strip()
    if start.isdigit():
        await db.set(guild.id, "economy_start_balance", int(start))
        lines.append(f"Start balance: **{int(start):,}**")

    work = _parse_range(values.get("work", ""))
    if work:
        await db.set(guild.id, "economy_work_min", work[0])
        await db.set(guild.id, "economy_work_max", work[1])
        lines.append(f"Work range: **{work[0]:,} - {work[1]:,}**")

    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="economy",
        label="Economy",
        emoji="💰",
        description="Currency, daily/work amounts and the starting balance.",
        fields=[
            ("Currency name", "coins", True, "currency"),
            ("Currency symbol", "🪙", False, "symbol"),
            ("Daily amount", "100", False, "daily"),
            ("Start balance", "100", False, "start_balance"),
            ("Work amount min/max (e.g. 50/150)", "50/150", False, "work"),
        ],
        handler=_apply_economy_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Economy(commands.Cog):
    """Currency, work, daily rewards, shop and gambling commands."""

    economy_group = app_commands.Group(
        name="economy", description="Economy administration and leaderboard.", guild_only=True
    )
    shop_group = app_commands.Group(
        name="shop", description="Browse the server shop and buy items.", guild_only=True
    )
    gamble_group = app_commands.Group(
        name="gamble", description="Gamble your money for a chance to win big.", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- shared helpers ------------------------------------------------------
    async def _deny_disabled(self, interaction: discord.Interaction) -> bool:
        """Reply and return True when the economy is disabled."""
        guild = interaction.guild
        assert guild is not None
        if await _economy_enabled(guild.id):
            return False
        await interaction.response.send_message(
            embed=error_embed("The economy system is disabled on this server."), ephemeral=True
        )
        return True

    async def _start_bet(self, interaction: discord.Interaction, bet: int) -> bool:
        """Validate a bet, deduct it from the wallet and return True on success."""
        guild = interaction.guild
        assert guild is not None
        _, symbol = await _currency(guild.id)
        max_bet = await db.get_int(guild.id, "economy_gamble_max", DEFAULT_GAMBLE_MAX)
        if bet < 1 or bet > max_bet:
            await interaction.response.send_message(
                embed=error_embed(f"Your bet must be between 1 and {max_bet}."), ephemeral=True
            )
            return False
        account = await _get_account(guild.id, interaction.user.id)
        cash = int(account["cash"])
        if cash < bet:
            await interaction.response.send_message(
                embed=error_embed(f"You only have {symbol} **{cash:,}** in cash."), ephemeral=True
            )
            return False
        await _update_cash(guild.id, interaction.user.id, -bet)
        return True

    # -- /balance ------------------------------------------------------------
    @app_commands.command(name="balance", description="Show your cash, bank balance and net worth.")
    @app_commands.describe(member="Member to check (defaults to you)")
    @app_commands.guild_only()
    async def balance(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        """Display wallet, bank and the member's leaderboard rank."""
        guild = interaction.guild
        assert guild is not None
        target = member or interaction.user
        assert isinstance(target, discord.Member)
        account = await _get_account(guild.id, target.id)
        cash, bank = int(account["cash"]), int(account["bank"])
        total = cash + bank
        name, symbol = await _currency(guild.id)
        embed = base_embed(
            title=f"{symbol} {target.display_name}'s balance",
            description=(
                f"**Cash:** {symbol} {cash:,}\n"
                f"**Bank:** {symbol} {bank:,}\n"
                f"**Net worth:** {symbol} {total:,}"
            ),
            color=config.THEME_COLOR,
            thumbnail=target.display_avatar.url,
        )
        count_row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM economy WHERE guild_id = ?", (guild.id,)
        )
        accounts = int(count_row["c"]) if count_row else 0
        if accounts >= 2:
            rank_row = await db.fetchone(
                "SELECT COUNT(*) AS c FROM economy WHERE guild_id = ? AND (cash + bank) > ?",
                (guild.id, total),
            )
            rank = int(rank_row["c"]) + 1 if rank_row else 1
            embed.add_field(name="Rank", value=f"#{rank} of {accounts}", inline=True)
        embed.set_footer(text=f"Currency: {name}")
        await interaction.response.send_message(embed=embed)

    # -- /daily --------------------------------------------------------------
    @app_commands.command(name="daily", description="Claim your daily reward and build a streak.")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction) -> None:
        """Claim the daily reward once per cooldown, with a streak bonus."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        now = int(time.time())
        account = await _get_account(guild.id, interaction.user.id)
        cooldown_seconds = await db.get_int(guild.id, "economy_daily_cooldown", DEFAULT_DAILY_COOLDOWN)
        last = int(account["last_daily"])
        if last and now - last < cooldown_seconds:
            ready = last + cooldown_seconds
            await interaction.response.send_message(
                embed=warn_embed(f"You already claimed your daily reward — come back {format_ts(ready, 'R')}."),
                ephemeral=True,
            )
            return
        streak_bonus = await db.get_bool(guild.id, "economy_daily_streak_bonus", True)
        base_amount = await db.get_int(guild.id, "economy_daily", DEFAULT_DAILY)
        old_streak = int(account["daily_streak"])
        streak = old_streak + 1 if (last and now - last <= STREAK_WINDOW) else 1
        multiplier = min(2.0, 1.0 + 0.10 * (streak - 1)) if streak_bonus else 1.0
        amount = max(1, int(round(base_amount * multiplier)))
        await _update_cash(guild.id, interaction.user.id, amount)
        await db.execute(
            "UPDATE economy SET daily_streak = ?, last_daily = ? WHERE guild_id = ? AND user_id = ?",
            (streak, now, guild.id, interaction.user.id),
        )
        _, symbol = await _currency(guild.id)
        bonus_line = f"\n*Streak bonus: x{multiplier:.1f}*" if streak_bonus and multiplier > 1.0 else ""
        await interaction.response.send_message(
            embed=success_embed(
                f"You claimed {symbol} **{amount:,}**!\n🔥 Daily streak: **{streak}**{bonus_line}",
                title="Daily reward",
            )
        )

    # -- /work ---------------------------------------------------------------
    @app_commands.command(name="work", description="Work a shift to earn some money.")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction) -> None:
        """Earn a random amount, limited by the persisted work cooldown."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        now = int(time.time())
        account = await _get_account(guild.id, interaction.user.id)
        cooldown_seconds = await db.get_int(guild.id, "economy_work_cooldown", DEFAULT_WORK_COOLDOWN)
        last = int(account["last_work"])
        if last and now - last < cooldown_seconds:
            ready = last + cooldown_seconds
            await interaction.response.send_message(
                embed=warn_embed(f"You are still tired — you can work again {format_ts(ready, 'R')}."),
                ephemeral=True,
            )
            return
        work_min = await db.get_int(guild.id, "economy_work_min", DEFAULT_WORK_MIN)
        work_max = await db.get_int(guild.id, "economy_work_max", DEFAULT_WORK_MAX)
        if work_min > work_max:
            work_min, work_max = work_max, work_min
        amount = random.randint(max(0, work_min), max(work_min, work_max))
        flavour = random.choice(WORK_JOBS)
        await _update_cash(guild.id, interaction.user.id, amount)
        await db.execute(
            "UPDATE economy SET last_work = ? WHERE guild_id = ? AND user_id = ?",
            (now, guild.id, interaction.user.id),
        )
        _, symbol = await _currency(guild.id)
        embed = success_embed(f"{flavour} and earned {symbol} **{amount:,}**!", title="Work")
        embed.add_field(name="Next shift", value=format_ts(now + cooldown_seconds, "R"), inline=True)
        await interaction.response.send_message(embed=embed)

    # -- /beg ----------------------------------------------------------------
    @app_commands.command(name="beg", description="Beg for a small amount of spare change.")
    @app_commands.guild_only()
    async def beg(self, interaction: discord.Interaction) -> None:
        """Earn a small random amount on a short persisted cooldown."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        now = int(time.time())
        account = await _get_account(guild.id, interaction.user.id)
        last = int(account["last_beg"])
        if last and now - last < BEG_COOLDOWN:
            ready = last + BEG_COOLDOWN
            await interaction.response.send_message(
                embed=warn_embed(f"Give it a moment — you can beg again {format_ts(ready, 'R')}."),
                ephemeral=True,
            )
            return
        amount = random.randint(5, 25)
        flavour = random.choice(BEG_FLAVOURS)
        await _update_cash(guild.id, interaction.user.id, amount)
        await db.execute(
            "UPDATE economy SET last_beg = ? WHERE guild_id = ? AND user_id = ?",
            (now, guild.id, interaction.user.id),
        )
        _, symbol = await _currency(guild.id)
        await interaction.response.send_message(
            embed=success_embed(f"{flavour}\nYou received {symbol} **{amount}**.", title="Begging")
        )

    # -- /deposit ------------------------------------------------------------
    @app_commands.command(name="deposit", description="Move money from your wallet into your bank.")
    @app_commands.describe(amount="Amount to deposit, or 'all'")
    @app_commands.guild_only()
    async def deposit(self, interaction: discord.Interaction, amount: str) -> None:
        """Deposit cash into the bank."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        account = await _get_account(guild.id, interaction.user.id)
        cash = int(account["cash"])
        _, symbol = await _currency(guild.id)
        value = _parse_amount(amount, cash)
        if value is None or value <= 0:
            await interaction.response.send_message(
                embed=error_embed("Enter a positive amount or the word `all`."), ephemeral=True
            )
            return
        if value > cash:
            await interaction.response.send_message(
                embed=error_embed(f"You only have {symbol} **{cash:,}** in cash."), ephemeral=True
            )
            return
        await _update_cash(guild.id, interaction.user.id, -value)
        await _update_bank(guild.id, interaction.user.id, value)
        await interaction.response.send_message(
            embed=success_embed(f"Deposited {symbol} **{value:,}** into your bank."), ephemeral=True
        )

    # -- /withdraw -----------------------------------------------------------
    @app_commands.command(name="withdraw", description="Move money from your bank into your wallet.")
    @app_commands.describe(amount="Amount to withdraw, or 'all'")
    @app_commands.guild_only()
    async def withdraw(self, interaction: discord.Interaction, amount: str) -> None:
        """Withdraw cash from the bank."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        account = await _get_account(guild.id, interaction.user.id)
        bank = int(account["bank"])
        _, symbol = await _currency(guild.id)
        value = _parse_amount(amount, bank)
        if value is None or value <= 0:
            await interaction.response.send_message(
                embed=error_embed("Enter a positive amount or the word `all`."), ephemeral=True
            )
            return
        if value > bank:
            await interaction.response.send_message(
                embed=error_embed(f"You only have {symbol} **{bank:,}** in your bank."), ephemeral=True
            )
            return
        await _update_bank(guild.id, interaction.user.id, -value)
        await _update_cash(guild.id, interaction.user.id, value)
        await interaction.response.send_message(
            embed=success_embed(f"Withdrew {symbol} **{value:,}** from your bank."), ephemeral=True
        )

    # -- /pay ----------------------------------------------------------------
    @app_commands.command(name="pay", description="Send money from your wallet to another member.")
    @app_commands.describe(member="Recipient", amount="Amount to send")
    @app_commands.guild_only()
    async def pay(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1000000000],
    ) -> None:
        """Transfer cash to another member (30s transient cooldown)."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                embed=error_embed("You cannot pay yourself."), ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                embed=error_embed("Bots cannot hold money."), ephemeral=True
            )
            return
        retry = rate_limit(interaction.user.id, "economy:pay", PAY_COOLDOWN)
        if retry is not None:
            await interaction.response.send_message(
                embed=warn_embed(f"Slow down — you can pay again in **{retry:.1f}s**."), ephemeral=True
            )
            return
        account = await _get_account(guild.id, interaction.user.id)
        cash = int(account["cash"])
        _, symbol = await _currency(guild.id)
        if int(amount) > cash:
            await interaction.response.send_message(
                embed=error_embed(f"You only have {symbol} **{cash:,}** in cash."), ephemeral=True
            )
            return
        await _update_cash(guild.id, interaction.user.id, -int(amount))
        await _get_account(guild.id, member.id)
        await _update_cash(guild.id, member.id, int(amount))
        await interaction.response.send_message(
            embed=success_embed(f"You sent {symbol} **{int(amount):,}** to {member.mention}.")
        )

    # -- /inventory ----------------------------------------------------------
    @app_commands.command(name="inventory", description="Show the items a member owns.")
    @app_commands.describe(member="Member to inspect (defaults to you)")
    @app_commands.guild_only()
    async def inventory(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        """List inventory rows with their quantities."""
        guild = interaction.guild
        assert guild is not None
        target = member or interaction.user
        assert isinstance(target, discord.Member)
        rows = await db.fetchall(
            "SELECT item, quantity FROM inventory WHERE guild_id = ? AND user_id = ? ORDER BY item",
            (guild.id, target.id),
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(f"{target.display_name} owns no items yet."), ephemeral=True
            )
            return
        pages: list[discord.Embed] = []
        per_page = 12
        total_pages = (len(rows) + per_page - 1) // per_page
        for start in range(0, len(rows), per_page):
            chunk = rows[start : start + per_page]
            embed = base_embed(
                title=f"🎒 {target.display_name}'s inventory",
                description="\n".join(f"**{row['item']}** × {int(row['quantity'])}" for row in chunk),
            )
            embed.set_footer(text=f"Page {start // per_page + 1}/{total_pages}")
            pages.append(embed)
        await paginate(interaction, pages)

    # -- /shop list ----------------------------------------------------------
    @shop_group.command(name="list", description="Browse the items available in the shop.")
    async def shop_list(self, interaction: discord.Interaction) -> None:
        """Paginated shop listing with prices, stock and role rewards."""
        guild = interaction.guild
        assert guild is not None
        rows = await db.fetchall(
            "SELECT * FROM shop_items WHERE guild_id = ? ORDER BY price", (guild.id,)
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("The shop is empty. Admins can add items with `/shop add`."), ephemeral=True
            )
            return
        _, symbol = await _currency(guild.id)
        pages: list[discord.Embed] = []
        per_page = 6
        total_pages = (len(rows) + per_page - 1) // per_page
        for start in range(0, len(rows), per_page):
            embed = base_embed(title="🛒 Shop")
            for row in rows[start : start + per_page]:
                stock = "∞" if int(row["quantity"]) < 0 else str(int(row["quantity"]))
                value = f"Price: {symbol} **{int(row['price']):,}**\nStock: {stock}"
                if row["description"]:
                    value += f"\n{truncate(str(row['description']), 150)}"
                if row["role_id"]:
                    value += f"\nRole: <@&{int(row['role_id'])}>"
                embed.add_field(name=str(row["item"]), value=value, inline=False)
            embed.set_footer(text=f"Page {start // per_page + 1}/{total_pages}")
            pages.append(embed)
        await paginate(interaction, pages)

    # -- /shop add -----------------------------------------------------------
    @shop_group.command(name="add", description="Add an item to the shop (or update an existing one).")
    @app_commands.describe(
        item="Unique item name",
        price="Price in the server currency",
        description="Short description shown in the shop",
        role="Role granted when this item is bought",
        stock="Available stock (-1 for unlimited)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def shop_add(
        self,
        interaction: discord.Interaction,
        item: str,
        price: app_commands.Range[int, 1, 1000000000],
        description: Optional[str] = None,
        role: Optional[discord.Role] = None,
        stock: app_commands.Range[int, -1, 1000000] = -1,
    ) -> None:
        """Create or update a shop item, respecting the per-guild limit."""
        guild = interaction.guild
        assert guild is not None
        name = truncate(item.strip(), 100)
        if not name:
            await interaction.response.send_message(
                embed=error_embed("Please provide an item name."), ephemeral=True
            )
            return
        existing = await db.fetchone(
            "SELECT item FROM shop_items WHERE guild_id = ? AND item = ?", (guild.id, name)
        )
        if existing is None:
            count_row = await db.fetchone(
                "SELECT COUNT(*) AS c FROM shop_items WHERE guild_id = ?", (guild.id,)
            )
            if (int(count_row["c"]) if count_row else 0) >= config.SHOP_ITEM_LIMIT:
                await interaction.response.send_message(
                    embed=error_embed(
                        f"This shop already has the maximum of **{config.SHOP_ITEM_LIMIT}** items."
                    ),
                    ephemeral=True,
                )
                return
        await db.execute(
            "INSERT INTO shop_items (guild_id, item, price, description, role_id, quantity) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, item) DO UPDATE SET price = excluded.price, "
            "description = excluded.description, role_id = excluded.role_id, quantity = excluded.quantity",
            (guild.id, name, int(price), truncate(description or "", 400), role.id if role else None, int(stock)),
        )
        action = "updated" if existing else "added"
        await interaction.response.send_message(
            embed=success_embed(f"Item **{name}** {action} for **{int(price):,}**."), ephemeral=True
        )

    # -- /shop remove --------------------------------------------------------
    @shop_group.command(name="remove", description="Remove an item from the shop.")
    @app_commands.describe(item="Name of the item to remove")
    @app_commands.default_permissions(manage_guild=True)
    async def shop_remove(self, interaction: discord.Interaction, item: str) -> None:
        """Delete a shop item."""
        guild = interaction.guild
        assert guild is not None
        found = await _find_shop_item(guild.id, item.strip())
        if found is None:
            await interaction.response.send_message(
                embed=error_embed(f"No shop item named **{truncate(item, 100)}** was found."), ephemeral=True
            )
            return
        await db.execute(
            "DELETE FROM shop_items WHERE guild_id = ? AND item = ?", (guild.id, found["item"])
        )
        await interaction.response.send_message(
            embed=success_embed(f"Removed **{found['item']}** from the shop."), ephemeral=True
        )

    # -- /shop buy -----------------------------------------------------------
    @shop_group.command(name="buy", description="Buy an item from the shop.")
    @app_commands.describe(item="Name of the item to buy")
    async def shop_buy(self, interaction: discord.Interaction, item: str) -> None:
        """Purchase an item, granting its role and adding it to the inventory."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        member = interaction.user
        assert isinstance(member, discord.Member)
        found = await _find_shop_item(guild.id, item.strip())
        if found is None:
            await interaction.response.send_message(
                embed=error_embed(f"No shop item named **{truncate(item, 100)}** was found."), ephemeral=True
            )
            return
        price = int(found["price"])
        stock = int(found["quantity"])
        _, symbol = await _currency(guild.id)
        if stock == 0:
            await interaction.response.send_message(
                embed=error_embed(f"**{found['item']}** is sold out."), ephemeral=True
            )
            return
        account = await _get_account(guild.id, member.id)
        cash = int(account["cash"])
        if cash < price:
            await interaction.response.send_message(
                embed=error_embed(
                    f"**{found['item']}** costs {symbol} **{price:,}** but you only have {symbol} **{cash:,}**."
                ),
                ephemeral=True,
            )
            return
        role = guild.get_role(int(found["role_id"])) if found["role_id"] else None
        if found["role_id"] and role is None:
            await interaction.response.send_message(
                embed=error_embed("That item grants a role that no longer exists."), ephemeral=True
            )
            return
        if role is not None and role in member.roles:
            await interaction.response.send_message(
                embed=error_embed(f"You already have {role.mention}."), ephemeral=True
            )
            return
        if role is not None:
            try:
                await member.add_roles(role, reason="Shop purchase", atomic=True)
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not grant shop role to %s in guild %s", member.id, guild.id)
                await interaction.response.send_message(
                    embed=error_embed("I could not grant you that role — please contact an admin."),
                    ephemeral=True,
                )
                return
        await _update_cash(guild.id, member.id, -price)
        if stock > 0:
            await db.execute(
                "UPDATE shop_items SET quantity = quantity - 1 WHERE guild_id = ? AND item = ?",
                (guild.id, found["item"]),
            )
        await db.execute(
            "INSERT INTO inventory (guild_id, user_id, item, quantity) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id, item) DO UPDATE SET quantity = quantity + 1",
            (guild.id, member.id, found["item"]),
        )
        await interaction.response.send_message(
            embed=success_embed(f"You bought **{found['item']}** for {symbol} **{price:,}**!"),
            ephemeral=True,
        )

    # -- /gamble coinflip ----------------------------------------------------
    @gamble_group.command(name="coinflip", description="Bet on a coin flip — win double your bet.")
    @app_commands.describe(bet="Amount to bet", choice="Heads or tails")
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="Heads", value="heads"),
            app_commands.Choice(name="Tails", value="tails"),
        ]
    )
    async def gamble_coinflip(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 1000000],
        choice: app_commands.Choice[str],
    ) -> None:
        """Flip a coin; a correct call pays 2x the bet."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        if not await self._start_bet(interaction, int(bet)):
            return
        result = random.choice(("heads", "tails"))
        _, symbol = await _currency(guild.id)
        if result == choice.value:
            payout = int(bet) * 2
            await _update_cash(guild.id, interaction.user.id, payout)
            embed = success_embed(
                f"The coin landed on **{result}**. You won {symbol} **{int(bet):,}** "
                f"(paid out {symbol} {payout:,}).",
                title="🪙 Coin flip",
            )
        else:
            embed = error_embed(
                f"The coin landed on **{result}**. You lost {symbol} **{int(bet):,}**.",
                title="🪙 Coin flip",
            )
        await interaction.response.send_message(embed=embed)

    # -- /gamble dice --------------------------------------------------------
    @gamble_group.command(name="dice", description="Roll a die — guess the result for 6x your bet.")
    @app_commands.describe(bet="Amount to bet", guess="Your guess (1-6); without a guess you must roll a 6")
    async def gamble_dice(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 1000000],
        guess: Optional[app_commands.Range[int, 1, 6]] = None,
    ) -> None:
        """Roll a die; a correct guess pays 6x, otherwise only a 6 wins."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        if not await self._start_bet(interaction, int(bet)):
            return
        roll = random.randint(1, 6)
        won = roll == int(guess) if guess is not None else roll == 6
        _, symbol = await _currency(guild.id)
        guess_text = f"you guessed **{int(guess)}**" if guess is not None else "you did not guess"
        if won:
            payout = int(bet) * 6
            await _update_cash(guild.id, interaction.user.id, payout)
            embed = success_embed(
                f"You rolled a **{roll}** and {guess_text} — you won {symbol} **{int(bet) * 5:,}** "
                f"(paid out {symbol} {payout:,}).",
                title="🎲 Dice",
            )
        else:
            embed = error_embed(
                f"You rolled a **{roll}** and {guess_text}. You lost {symbol} **{int(bet):,}**.",
                title="🎲 Dice",
            )
        await interaction.response.send_message(embed=embed)

    # -- /gamble slots -------------------------------------------------------
    @gamble_group.command(name="slots", description="Spin the slot machine for symbol-based payouts.")
    @app_commands.describe(bet="Amount to bet")
    async def gamble_slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 1000000]) -> None:
        """Spin three reels; three matching symbols or any pair pays out."""
        guild = interaction.guild
        assert guild is not None
        if await self._deny_disabled(interaction):
            return
        if not await self._start_bet(interaction, int(bet)):
            return
        symbols = [symbol for symbol, _ in SLOTS_REEL]
        weights = [weight for _, weight in SLOTS_REEL]
        reels = random.choices(symbols, weights=weights, k=3)
        multiplier = 0.0
        if reels[0] == reels[1] == reels[2]:
            multiplier = SLOTS_PAYOUT.get(reels[0], 0.0)
        elif len(set(reels)) == 2:
            multiplier = 1.5
        _, symbol = await _currency(guild.id)
        reel_text = " | ".join(reels)
        if multiplier > 0:
            payout = int(int(bet) * multiplier)
            await _update_cash(guild.id, interaction.user.id, payout)
            embed = success_embed(
                f"{reel_text}\nYou won {symbol} **{payout - int(bet):,}** (x{multiplier:g} — "
                f"paid out {symbol} {payout:,}).",
                title="🎰 Slots",
            )
        else:
            embed = error_embed(
                f"{reel_text}\nNo match — you lost {symbol} **{int(bet):,}**.", title="🎰 Slots"
            )
        await interaction.response.send_message(embed=embed)

    # -- /economy leaderboard ------------------------------------------------
    @economy_group.command(name="leaderboard", description="Show the richest members on the server.")
    @app_commands.describe(page="Page to show (starts at 1)")
    async def economy_leaderboard(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 10000] = 1
    ) -> None:
        """Paginated ranking by combined cash + bank."""
        guild = interaction.guild
        assert guild is not None
        rows = await db.fetchall(
            "SELECT user_id, cash, bank FROM economy WHERE guild_id = ? ORDER BY (cash + bank) DESC",
            (guild.id,),
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Nobody has any money yet."), ephemeral=True
            )
            return
        _, symbol = await _currency(guild.id)
        medals = ("🥇", "🥈", "🥉")
        pages: list[discord.Embed] = []
        total_pages = (len(rows) + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE
        for start in range(0, len(rows), LEADERBOARD_PAGE_SIZE):
            embed = base_embed(title="💰 Richest members")
            lines: list[str] = []
            for offset, row in enumerate(rows[start : start + LEADERBOARD_PAGE_SIZE]):
                position = start + offset + 1
                badge = medals[position - 1] if position <= 3 else f"`#{position}`"
                total = int(row["cash"]) + int(row["bank"])
                lines.append(f"{badge} <@{row['user_id']}> — {symbol} **{total:,}**")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {start // LEADERBOARD_PAGE_SIZE + 1}/{total_pages}")
            pages.append(embed)
        page = min(max(1, page), len(pages))
        if len(pages) > 1:
            view = Paginator(pages, owner_id=interaction.user.id)
            view.page_label.label = f"{page}/{len(pages)}"
            view.index = page - 1
            await interaction.response.send_message(embed=pages[page - 1], view=view)
        else:
            await interaction.response.send_message(embed=pages[0])

    # -- /economy give -------------------------------------------------------
    @economy_group.command(name="give", description="Give a member money.")
    @app_commands.describe(member="Recipient", amount="Amount to give")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1000000000],
    ) -> None:
        """Add money to a member's wallet (admin command)."""
        guild = interaction.guild
        assert guild is not None
        await _get_account(guild.id, member.id)
        await _update_cash(guild.id, member.id, int(amount))
        _, symbol = await _currency(guild.id)
        await interaction.response.send_message(
            embed=success_embed(f"Gave {symbol} **{int(amount):,}** to {member.mention}."), ephemeral=True
        )

    # -- /economy remove -----------------------------------------------------
    @economy_group.command(name="remove", description="Remove money from a member's wallet.")
    @app_commands.describe(member="Member to take from", amount="Amount to remove")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1000000000],
    ) -> None:
        """Remove money from a member's wallet, never below zero (admin command)."""
        guild = interaction.guild
        assert guild is not None
        account = await _get_account(guild.id, member.id)
        cash = int(account["cash"])
        removed = min(cash, int(amount))
        await _update_cash(guild.id, member.id, -removed)
        _, symbol = await _currency(guild.id)
        await interaction.response.send_message(
            embed=success_embed(f"Removed {symbol} **{removed:,}** from {member.mention}."), ephemeral=True
        )

    # -- /economy reset ------------------------------------------------------
    @economy_group.command(name="reset", description="Reset a member's entire economy profile.")
    @app_commands.describe(member="Member whose balance should be wiped")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_reset(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Zero a member's cash, bank and cooldown timestamps (admin command)."""
        guild = interaction.guild
        assert guild is not None
        await db.execute(
            "INSERT INTO economy "
            "(guild_id, user_id, cash, bank, daily_streak, last_daily, last_work, last_beg) "
            "VALUES (?, ?, 0, 0, 0, 0, 0, 0) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET cash = 0, bank = 0, daily_streak = 0, "
            "last_daily = 0, last_work = 0, last_beg = 0",
            (guild.id, member.id),
        )
        await interaction.response.send_message(
            embed=success_embed(f"Reset the economy profile of {member.mention}."), ephemeral=True
        )

    # -- /economy config -----------------------------------------------------
    @economy_group.command(name="config", description="Show the current economy configuration.")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_config(self, interaction: discord.Interaction) -> None:
        """Display every economy setting."""
        guild = interaction.guild
        assert guild is not None
        name, symbol = await _currency(guild.id)
        enabled = await _economy_enabled(guild.id)
        daily = await db.get_int(guild.id, "economy_daily", DEFAULT_DAILY)
        daily_cd = await db.get_int(guild.id, "economy_daily_cooldown", DEFAULT_DAILY_COOLDOWN)
        streak = await db.get_bool(guild.id, "economy_daily_streak_bonus", True)
        work_min = await db.get_int(guild.id, "economy_work_min", DEFAULT_WORK_MIN)
        work_max = await db.get_int(guild.id, "economy_work_max", DEFAULT_WORK_MAX)
        work_cd = await db.get_int(guild.id, "economy_work_cooldown", DEFAULT_WORK_COOLDOWN)
        start = await db.get_int(guild.id, "economy_start_balance", DEFAULT_START_BALANCE)
        gamble_max = await db.get_int(guild.id, "economy_gamble_max", DEFAULT_GAMBLE_MAX)
        count_row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM shop_items WHERE guild_id = ?", (guild.id,)
        )
        embed = base_embed(
            title="💰 Economy settings",
            fields=[
                ("Enabled", "Yes" if enabled else "No", True),
                ("Currency", f"{symbol} {name}", True),
                ("Daily amount", f"{daily:,}", True),
                ("Daily cooldown", format_duration(daily_cd), True),
                ("Streak bonus", "Enabled" if streak else "Disabled", True),
                ("Work range", f"{work_min:,} - {work_max:,}", True),
                ("Work cooldown", format_duration(work_cd), True),
                ("Start balance", f"{start:,}", True),
                ("Max gamble", f"{gamble_max:,}", True),
                ("Shop items", str(int(count_row["c"]) if count_row else 0), True),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- /economy enable -----------------------------------------------------
    @economy_group.command(name="enable", description="Enable or disable the whole economy system.")
    @app_commands.describe(enabled="Whether the economy commands should work")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_enable(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the economy master switch."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "economy_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"The economy is now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    # -- /economy currency ---------------------------------------------------
    @economy_group.command(name="currency", description="Set the currency name and symbol.")
    @app_commands.describe(name="Currency name, e.g. coins", symbol="Optional symbol or emoji")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_currency(
        self, interaction: discord.Interaction, name: str, symbol: Optional[str] = None
    ) -> None:
        """Configure the display name and symbol of the currency."""
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        await db.set(guild_id, "economy_currency", truncate(name, 32))
        lines = [f"Currency name set to **{truncate(name, 32)}**."]
        if symbol:
            await db.set(guild_id, "economy_symbol", truncate(symbol, 8))
            lines.append(f"Symbol set to {truncate(symbol, 8)}.")
        await interaction.response.send_message(embed=success_embed("\n".join(lines)), ephemeral=True)

    # -- /economy daily ------------------------------------------------------
    @economy_group.command(name="daily", description="Set the base daily reward amount.")
    @app_commands.describe(amount="Base amount granted by /daily")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_daily(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000000]
    ) -> None:
        """Configure the daily reward base amount."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "economy_daily", int(amount))
        await interaction.response.send_message(
            embed=success_embed(f"The daily reward is now **{int(amount):,}**."), ephemeral=True
        )

    # -- /economy work -------------------------------------------------------
    @economy_group.command(name="work", description="Set the work reward range.")
    @app_commands.describe(min_amount="Minimum work payout", max_amount="Maximum work payout")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_work(
        self,
        interaction: discord.Interaction,
        min_amount: app_commands.Range[int, 1, 1000000],
        max_amount: app_commands.Range[int, 1, 1000000],
    ) -> None:
        """Configure the random work payout range."""
        assert interaction.guild is not None
        if min_amount > max_amount:
            await interaction.response.send_message(
                embed=error_embed("The minimum must not be greater than the maximum."), ephemeral=True
            )
            return
        await db.set(interaction.guild.id, "economy_work_min", int(min_amount))
        await db.set(interaction.guild.id, "economy_work_max", int(max_amount))
        await interaction.response.send_message(
            embed=success_embed(f"Work now pays between **{int(min_amount):,}** and **{int(max_amount):,}**."),
            ephemeral=True,
        )

    # -- /economy startbalance ----------------------------------------------
    @economy_group.command(name="startbalance", description="Set the balance given to new members.")
    @app_commands.describe(amount="Starting cash for members without an account")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_startbalance(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 0, 1000000000]
    ) -> None:
        """Configure the starting balance."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "economy_start_balance", int(amount))
        await interaction.response.send_message(
            embed=success_embed(f"New members now start with **{int(amount):,}**."), ephemeral=True
        )

    # -- /economy gamblemax --------------------------------------------------
    @economy_group.command(name="gamblemax", description="Set the maximum gamble bet.")
    @app_commands.describe(amount="Highest allowed bet in gambling games")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_gamblemax(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000000000]
    ) -> None:
        """Configure the maximum bet."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "economy_gamble_max", int(amount))
        await interaction.response.send_message(
            embed=success_embed(f"The maximum gamble bet is now **{int(amount):,}**."), ephemeral=True
        )

    # -- /economy streakbonus -----------------------------------------------
    @economy_group.command(name="streakbonus", description="Toggle the daily streak bonus.")
    @app_commands.describe(enabled="Whether consecutive daily claims give a bonus")
    @app_commands.default_permissions(manage_guild=True)
    async def economy_streakbonus(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the daily streak bonus."""
        assert interaction.guild is not None
        await db.set(interaction.guild.id, "economy_daily_streak_bonus", enabled)
        await interaction.response.send_message(
            embed=success_embed(
                f"The daily streak bonus is now **{'enabled' if enabled else 'disabled'}**."
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    """Load the economy cog."""
    await bot.add_cog(Economy(bot))
