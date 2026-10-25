"""Fun commands (8ball, coinflip, dice, choose, rps, meme, trivia) and utility
commands (embed builder, announce, say, roleinfo, channelinfo, icon, banner)."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import urllib.parse
from typing import Any, Callable, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import format_ts, truncate

log = logging.getLogger("cogs.fun")

# ---------------------------------------------------------------------------
# Static data
# ---------------------------------------------------------------------------
EIGHT_BALL_ANSWERS: tuple[str, ...] = (
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes — definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
    "No way.",
    "I wouldn't bet on it.",
    "Ask your other bot.",
    "The crystal ball is cloudy. 🌫️",
)

RPS_MOVES: tuple[str, ...] = ("rock", "paper", "scissors")
RPS_EMOJI: dict[str, str] = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
_RPS_BEATS: dict[str, str] = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

MEME_SUBREDDITS: tuple[str, ...] = ("memes", "dankmemes", "me_irl", "funny", "wholesomememes")
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")

MEME_FALLBACKS: tuple[str, ...] = (
    "The meme API is taking a break, so here is a free one: your smile is the best meme I know. 😄",
    "404: memes not found. Have you tried laughing at yourself?",
    "I asked the meme machine for a joke and it handed me this. You're welcome.",
    "Somewhere a meme is born every second — this one is playing hide and seek.",
    "This meme is so exclusive, even I haven't seen it.",
    "Error: meme not found. Solution: touch grass. 🌱",
    "When the Wi-Fi dies the memes do not flow. Here is a consolation line instead.",
    "I would tell you a joke, but the API said no. Imagine the funniest thing possible.",
)

# Open Trivia DB category name -> id (a small hand-picked selection)
TRIVIA_CATEGORIES: dict[str, int] = {
    "general knowledge": 9,
    "books": 10,
    "film": 11,
    "music": 12,
    "science & nature": 17,
    "science: computers": 18,
    "mathematics": 19,
    "mythology": 20,
    "sports": 21,
    "geography": 22,
    "history": 23,
    "animals": 27,
    "vehicles": 28,
}

# Built-in fallback: (question, correct, wrong 1, wrong 2, wrong 3)
LOCAL_TRIVIA: tuple[tuple[str, str, str, str, str], ...] = (
    ("What is the capital of France?", "Paris", "London", "Berlin", "Madrid"),
    ("How many continents are there?", "7", "5", "6", "8"),
    ("Which planet is known as the Red Planet?", "Mars", "Venus", "Jupiter", "Saturn"),
    ("H2O is the chemical formula for which substance?", "Water", "Oxygen", "Salt", "Hydrogen peroxide"),
    ("Who painted the Mona Lisa?", "Leonardo da Vinci", "Vincent van Gogh", "Pablo Picasso", "Michelangelo"),
    ("What is the largest ocean on Earth?", "Pacific Ocean", "Atlantic Ocean", "Indian Ocean", "Arctic Ocean"),
    ("How many days are in a leap year?", "366", "365", "364", "367"),
    ("What is the chemical symbol for gold?", "Au", "Ag", "Fe", "Gd"),
    ("Which language has the most native speakers?", "Mandarin Chinese", "English", "Spanish", "Hindi"),
    ("What is the tallest living animal?", "Giraffe", "Elephant", "Camel", "Moose"),
    ("How many strings does a standard guitar have?", "6", "4", "5", "7"),
    ("In which year did the first Moon landing happen?", "1969", "1965", "1972", "1959"),
    ("What is the smallest prime number?", "2", "1", "3", "5"),
    ("Which gas do plants absorb from the air?", "Carbon dioxide", "Oxygen", "Nitrogen", "Helium"),
)

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------
def _parse_hex_color(text: str) -> Optional[discord.Color]:
    """Parse ``#RRGGBB`` (or ``RRGGBB``) into a colour, else None."""
    match = _HEX_COLOR_RE.match(text.strip())
    if not match:
        return None
    return discord.Color.from_str(f"#{match.group(1)}")


def _is_http_url(text: str) -> bool:
    """Accept only absolute http/https URLs."""
    try:
        parts = urllib.parse.urlsplit(text.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# ---------------------------------------------------------------------------
# Trivia answer view (one-shot, 60s)
# ---------------------------------------------------------------------------
class TriviaView(discord.ui.View):
    """Four answer buttons (A–D) for a single trivia question.

    ``builder(result_text)`` produces the embed for the current state of the
    question: empty string for the untouched question, a result line afterwards.
    """

    LETTERS: tuple[str, ...] = ("A", "B", "C", "D")

    def __init__(
        self,
        *,
        owner_id: int,
        answers: list[str],
        correct_index: int,
        builder: Callable[[str], discord.Embed],
    ) -> None:
        super().__init__(timeout=60.0)
        self.owner_id = owner_id
        self.answers = answers
        self.correct_index = correct_index
        self.builder = builder
        self.answerer: Optional[discord.abc.User] = None
        self.message: Optional[discord.Message] = None
        for index, child in enumerate(self.children):
            if isinstance(child, discord.ui.Button):
                child.label = f"{self.LETTERS[index]}. {truncate(answers[index], 80)}"

    @discord.ui.button(label="A", style=discord.ButtonStyle.secondary, custom_id="trivia:answer:a")
    async def answer_a(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._resolve(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.secondary, custom_id="trivia:answer:b")
    async def answer_b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._resolve(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.secondary, custom_id="trivia:answer:c")
    async def answer_c(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._resolve(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.secondary, custom_id="trivia:answer:d")
    async def answer_d(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._resolve(interaction, 3)

    # -- internals -----------------------------------------------------------
    async def _resolve(self, interaction: discord.Interaction, index: int) -> None:
        """Lock the question in and reveal the result."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("Only the person who asked the question can answer it."), ephemeral=True
            )
            return
        self.answerer = interaction.user
        self.stop()
        self._apply_styles(index)
        try:
            await interaction.response.edit_message(embed=self.builder(self._result_text(index)), view=self)
        except discord.HTTPException:
            log.exception("Failed to update the trivia message")

    def _apply_styles(self, picked: Optional[int]) -> None:
        """Disable every button and colour the correct (and picked) answer."""
        for index, child in enumerate(self.children):
            if not isinstance(child, discord.ui.Button):
                continue
            child.disabled = True
            if index == self.correct_index:
                child.style = discord.ButtonStyle.success
            elif picked is not None and index == picked:
                child.style = discord.ButtonStyle.danger

    def _result_text(self, picked: Optional[int]) -> str:
        correct = self.LETTERS[self.correct_index]
        who = f" — answered by {self.answerer.mention}" if self.answerer else ""
        if picked is None:
            return f"⌛ Time's up! The correct answer was **{correct}**."
        if picked == self.correct_index:
            return f"✅ Correct! The answer is **{correct}**{who}."
        return f"❌ Wrong! The correct answer was **{correct}**{who}."

    async def on_timeout(self) -> None:
        self._apply_styles(None)
        if self.message is not None:
            try:
                await self.message.edit(embed=self.builder(self._result_text(None)), view=self)
            except discord.HTTPException:
                log.exception("Failed to time out the trivia message")


# ---------------------------------------------------------------------------
# /embed modal
# ---------------------------------------------------------------------------
class EmbedModal(discord.ui.Modal):
    """Collect the parts of a custom embed and send it to a channel."""

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__(title="Embed builder", timeout=300)
        self.channel = channel
        self.title_input = discord.ui.TextInput(label="Title", required=False, max_length=256)
        self.description_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=True, max_length=4000
        )
        self.color_input = discord.ui.TextInput(
            label="Colour hex", placeholder="#5865F2 (optional)", required=False, max_length=7
        )
        self.media_input = discord.ui.TextInput(
            label="Image & thumbnail URLs",
            placeholder="image url | thumbnail url (optional)",
            required=False,
            max_length=1000,
        )
        self.footer_input = discord.ui.TextInput(label="Footer", required=False, max_length=200)
        for item in (
            self.title_input,
            self.description_input,
            self.color_input,
            self.media_input,
            self.footer_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color: Optional[discord.Color] = None
        raw_color = str(self.color_input.value or "").strip()
        if raw_color:
            color = _parse_hex_color(raw_color)
            if color is None:
                await interaction.response.send_message(
                    embed=error_embed("Colour must be a hex code like `#5865F2`. Please try again."), ephemeral=True
                )
                return

        media = [part.strip() for part in str(self.media_input.value or "").split("|")]
        image = media[0] if media and media[0] else None
        thumbnail = media[1] if len(media) > 1 and media[1] else None
        for label, url in (("image", image), ("thumbnail", thumbnail)):
            if url is not None and not _is_http_url(url):
                await interaction.response.send_message(
                    embed=error_embed(f"The {label} URL must start with `http://` or `https://`."), ephemeral=True
                )
                return

        embed = base_embed(
            title=str(self.title_input.value or "").strip() or None,
            description=str(self.description_input.value or "").strip(),
            color=color,
            image=image,
            thumbnail=thumbnail,
            footer=str(self.footer_input.value or "").strip() or None,
        )
        try:
            await self.channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to send a custom embed to channel %s", self.channel.id)
            await interaction.response.send_message(
                embed=error_embed("I could not send the embed there. Please check my permissions."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"Your embed was posted in {self.channel.mention}."), ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Embed modal failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=error_embed("Something went wrong while building the embed."), ephemeral=True
            )


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Fun(commands.Cog):
    """Fun games and general purpose utility commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._http: Optional[aiohttp.ClientSession] = None

    async def cog_unload(self) -> None:
        """Close the shared HTTP session when the cog is unloaded."""
        if self._http is not None and not self._http.closed:
            await self._http.close()

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a lazily created shared session with a 10s timeout."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    # =======================================================================
    # Fun
    # =======================================================================
    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question.")
    @app_commands.describe(question="Your yes/no question")
    async def eight_ball(self, interaction: discord.Interaction, question: str) -> None:
        """Answer a question with a classic 8-ball reply."""
        answer = random.choice(EIGHT_BALL_ANSWERS)
        embed = base_embed(description=answer, color=config.THEME_COLOR)
        embed.set_author(name=f"🎱 {truncate(question, 200)}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Flip a coin.")
    @app_commands.describe(bet="Optional imaginary coin bet (wagered on heads)")
    async def coinflip(
        self,
        interaction: discord.Interaction,
        bet: Optional[app_commands.Range[int, 1, 1_000_000]] = None,
    ) -> None:
        """Flip a coin and narrate the result."""
        heads = random.choice((True, False))
        side = "Heads" if heads else "Tails"
        lines = [f"The coin spins high into the air... and lands on **{side}**!"]
        color = config.THEME_COLOR
        if bet is not None:
            if heads:
                lines.append(f"You bet **{bet:,} coins** on heads — **you win**! 🎉")
                color = config.SUCCESS_COLOR
            else:
                lines.append(f"You bet **{bet:,} coins** on heads — **you lose**. 😔")
                color = config.ERROR_COLOR
            lines.append("*Imagination coins only. No refunds.*")
        await interaction.response.send_message(embed=base_embed(description="\n".join(lines), color=color))

    @app_commands.command(description="Roll one or more dice.")
    @app_commands.describe(sides="Number of sides per die", count="How many dice to roll")
    async def dice(
        self,
        interaction: discord.Interaction,
        sides: app_commands.Range[int, 2, 1000] = 6,
        count: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        """Show every roll and the total."""
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        rolls_text = ", ".join(f"**{value}**" for value in rolls)
        description = f"🎲 Rolled {count}d{sides}\n**Rolls:** {rolls_text}\n**Total:** **{total}**"
        await interaction.response.send_message(
            embed=base_embed(description=description, color=config.THEME_COLOR)
        )

    @app_commands.command(description="Let the bot pick one of your options.")
    @app_commands.describe(options="Options separated by | — e.g. pizza|burger|sushi (2 to 20)")
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        """Pick one random option from a pipe separated list."""
        parts = [part.strip() for part in options.split("|") if part.strip()]
        if len(parts) < 2 or len(parts) > 20:
            await interaction.response.send_message(
                embed=error_embed("Give me between **2 and 20** options separated by `|`, e.g. `pizza|burger|sushi`."),
                ephemeral=True,
            )
            return
        pick = random.choice(parts)
        embed = base_embed(
            title="🎯 The verdict is in",
            description=f"I pick... **{truncate(pick, 500)}**!\n\n*From {len(parts)} option(s).*",
            color=config.SUCCESS_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Play rock, paper, scissors against the bot.")
    @app_commands.describe(choice="Your move")
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="🪨 Rock", value="rock"),
            app_commands.Choice(name="📄 Paper", value="paper"),
            app_commands.Choice(name="✂️ Scissors", value="scissors"),
        ]
    )
    async def rps(self, interaction: discord.Interaction, choice: app_commands.Choice[str]) -> None:
        """Best of one against the bot."""
        user_move = choice.value
        bot_move = random.choice(RPS_MOVES)
        if bot_move == user_move:
            outcome, color, flavour = "It's a tie!", config.WARNING_COLOR, "Great minds think alike."
        elif _RPS_BEATS[user_move] == bot_move:
            outcome, color, flavour = "You win!", config.SUCCESS_COLOR, "Lucky shot!"
        else:
            outcome, color, flavour = "I win!", config.ERROR_COLOR, "Better luck next time."
        embed = base_embed(
            title="Rock, Paper, Scissors",
            description=(
                f"**You:** {RPS_EMOJI[user_move]} {user_move.title()}\n"
                f"**Me:** {RPS_EMOJI[bot_move]} {bot_move.title()}\n\n"
                f"**{outcome}** {flavour}"
            ),
            color=color,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Post a random meme from Reddit.")
    @app_commands.describe(subreddit="Subreddit to fetch from (defaults to a popular one)")
    async def meme(self, interaction: discord.Interaction, subreddit: Optional[str] = None) -> None:
        """Fetch a meme, falling back to a local joke on failure."""
        target = (subreddit or "").strip() or random.choice(MEME_SUBREDDITS)
        if not _SUBREDDIT_RE.match(target):
            await interaction.response.send_message(
                embed=error_embed(
                    "That does not look like a valid subreddit name. Try one of: "
                    + ", ".join(f"`{name}`" for name in MEME_SUBREDDITS)
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        data = await self._fetch_meme(target)
        if data is None:
            await self._send_local_meme(interaction)
            return
        title = truncate(str(data.get("title") or "Untitled meme"), 250)
        image = str(data.get("url") or "")
        source = str(data.get("subreddit") or target)
        author = str(data.get("author") or "unknown")
        ups = data.get("ups")
        footer = f"r/{source} • u/{author}" + (f" • {ups} 👍" if isinstance(ups, int) else "")
        embed = base_embed(title=title, color=config.WARNING_COLOR, image=image)
        embed.set_footer(text=footer)
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to send meme embed")
            await self._send_local_meme(interaction)

    async def _fetch_meme(self, subreddit: str) -> Optional[dict[str, Any]]:
        """Fetch one meme from the meme-api endpoint, or None on failure."""
        url = f"https://meme-api.com/gimme/{urllib.parse.quote(subreddit)}"
        try:
            async with self._get_session().get(url) as response:
                if response.status != 200:
                    log.info("meme-api returned HTTP %s for r/%s", response.status, subreddit)
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.exception("Failed to fetch a meme from r/%s", subreddit)
            return None
        if not isinstance(data, dict) or not data.get("url"):
            return None
        return data

    async def _send_local_meme(self, interaction: discord.Interaction) -> None:
        """Send the offline fallback meme embed (never fails the command)."""
        embed = base_embed(
            title="🎭 Meme machine on a break",
            description=random.choice(MEME_FALLBACKS),
            color=config.WARNING_COLOR,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to send the fallback meme embed")

    @app_commands.command(description="Answer a multiple choice trivia question.")
    @app_commands.describe(category="Which category to quiz you on")
    @app_commands.choices(
        category=[
            app_commands.Choice(name=name.title(), value=name) for name in TRIVIA_CATEGORIES
        ]
    )
    async def trivia(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Ask a trivia question with A–D answer buttons."""
        label = category.name if category else "General Knowledge"
        category_id = TRIVIA_CATEGORIES.get(category.value, 9) if category else 9
        await interaction.response.defer()
        data = await self._fetch_trivia(category_id)
        if data is not None:
            question = html.unescape(str(data.get("question") or ""))
            correct = html.unescape(str(data.get("correct_answer") or ""))
            incorrect = [html.unescape(str(item)) for item in (data.get("incorrect_answers") or [])]
        else:
            question, correct, incorrect = "", "", []
        if not question or not correct or len(incorrect) != 3:
            question, correct, *incorrect = random.choice(LOCAL_TRIVIA)
            label = "Local edition"
        await self._send_trivia(
            interaction, question=question, correct=correct, incorrect=incorrect, category_label=label
        )

    async def _fetch_trivia(self, category_id: int) -> Optional[dict[str, Any]]:
        """Fetch one question from the Open Trivia DB, or None on failure."""
        params = {"amount": 1, "type": "multiple", "category": category_id}
        try:
            async with self._get_session().get("https://opentdb.com/api.php", params=params) as response:
                if response.status != 200:
                    log.info("OpenTDB returned HTTP %s", response.status)
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.exception("Failed to fetch a trivia question")
            return None
        if not isinstance(data, dict) or data.get("response_code") != 0:
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        return results[0]

    async def _send_trivia(
        self,
        interaction: discord.Interaction,
        *,
        question: str,
        correct: str,
        incorrect: list[str],
        category_label: str,
    ) -> None:
        """Post the question with four answer buttons (interaction deferred)."""
        answers = incorrect + [correct]
        random.shuffle(answers)
        correct_index = answers.index(correct)
        listing = "\n".join(
            f"**{TriviaView.LETTERS[index]}.** {truncate(answer, 300)}"
            for index, answer in enumerate(answers)
        )

        def build(result: str = "") -> discord.Embed:
            description = f"**{truncate(question, 800)}**\n\n{listing}"
            if result:
                description += f"\n\n{result}"
            return base_embed(
                title="🧠 Trivia time",
                description=description,
                color=config.INFO_COLOR,
                footer=f"{category_label} • 60 seconds to answer",
            )

        view = TriviaView(
            owner_id=interaction.user.id,
            answers=answers,
            correct_index=correct_index,
            builder=build,
        )
        try:
            message = await interaction.followup.send(embed=build(), view=view, wait=True)
            view.message = message
        except discord.HTTPException:
            log.exception("Failed to send the trivia question")

    # =======================================================================
    # Utility
    # =======================================================================
    @app_commands.command(description="Build and post a custom embed in a channel.")
    @app_commands.describe(channel="Where to post the embed")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def embed(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """Open the embed builder modal."""
        if not channel.permissions_for(interaction.guild.me).send_messages:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=error_embed(f"I cannot send messages in {channel.mention}."), ephemeral=True
            )
            return
        await interaction.response.send_modal(EmbedModal(channel))

    @app_commands.command(description="Post an announcement in a channel.")
    @app_commands.describe(
        channel="Where to post the announcement",
        message="The announcement text",
        title="Embed title (embed mode only)",
        ping_everyone="Ping @everyone in the announcement",
        use_embed="Send as an embed (default: true)",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        title: Optional[str] = None,
        ping_everyone: bool = False,
        use_embed: bool = True,
    ) -> None:
        """Send an announcement with an optional @everyone ping."""
        allowed = discord.AllowedMentions(everyone=ping_everyone, users=True, roles=False, replied_user=False)
        content: Optional[str] = None
        embed: Optional[discord.Embed] = None
        if use_embed:
            embed = base_embed(
                title=title or "Announcement",
                description=truncate(message, 4000),
                color=config.WARNING_COLOR,
            )
        else:
            content = message
        try:
            sent = await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException:
            log.exception("Failed to post an announcement in channel %s", channel.id)
            await interaction.response.send_message(
                embed=error_embed("I could not post the announcement there. Please check my permissions."),
                ephemeral=True,
            )
            return
        jump = f"https://discord.com/channels/{interaction.guild.id}/{sent.channel.id}/{sent.id}"  # type: ignore[union-attr]
        await interaction.response.send_message(
            embed=success_embed(f"Announcement posted in {channel.mention}: [jump to message]({jump})"),
            ephemeral=True,
        )

    @app_commands.command(description="Send text through the bot in a channel.")
    @app_commands.describe(channel="Where to send the text", text="What the bot should say",
                           use_embed="Send the text inside an embed")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        text: str,
        use_embed: bool = False,
    ) -> None:
        """Relay text as the bot and clean up the interaction response."""
        try:
            if use_embed:
                await channel.send(embed=base_embed(description=truncate(text, 4000)))
            else:
                await channel.send(content=text)
        except discord.HTTPException:
            log.exception("Failed to relay a message to channel %s", channel.id)
            await interaction.response.send_message(
                embed=error_embed("I could not send that message there. Please check my permissions."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"Message sent to {channel.mention}."), ephemeral=True
        )
        await asyncio.sleep(2.0)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass  # already gone — nothing to clean up

    @app_commands.command(description="Show information about a role.")
    @app_commands.describe(role="The role to inspect")
    @app_commands.guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """Overview of a role's colour, position and permissions."""
        granted = [name.replace("_", " ").title() for name, value in role.permissions if value and name]
        summary = truncate(", ".join(granted[:15]) or "None", 900)
        if len(granted) > 15:
            summary = truncate(f"{summary} (+{len(granted) - 15} more)", 1000)
        embed = base_embed(
            title=role.name,
            color=role.color if role.color != discord.Color.default() else None,
            thumbnail=role.icon.url if role.icon else None,
        )
        embed.set_author(name="Role information")
        embed.add_field(name="ID", value=str(role.id), inline=True)
        embed.add_field(name="Mention", value=role.mention, inline=True)
        embed.add_field(
            name="Colour", value=str(role.color) if role.color != discord.Color.default() else "Default", inline=True
        )
        embed.add_field(name="Position", value=str(role.position), inline=True)
        embed.add_field(name="Members", value=str(len(role.members)), inline=True)
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(name="Managed", value="Yes" if role.managed else "No", inline=True)
        embed.add_field(name="Created", value=format_ts(int(role.created_at.timestamp())), inline=True)
        embed.add_field(name=f"Permissions ({len(granted)})", value=summary, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show information about a channel.")
    @app_commands.describe(channel="The channel to inspect (defaults to the current one)")
    @app_commands.guild_only()
    async def channelinfo(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        """Overview of a channel's type, settings and creation date."""
        channel = channel or interaction.channel  # type: ignore[assignment]
        assert isinstance(channel, discord.abc.GuildChannel)
        kind = getattr(getattr(channel, "type", None), "name", str(getattr(channel, "type", "unknown")))
        embed = base_embed(title=f"#{channel.name}", color=config.INFO_COLOR)
        embed.set_author(name="Channel information")
        embed.add_field(name="ID", value=str(channel.id), inline=True)
        embed.add_field(name="Type", value=kind.replace("_", " ").title(), inline=True)
        embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
        position = getattr(channel, "position", None)
        embed.add_field(name="Position", value=str(position) if position is not None else "—", inline=True)
        topic = getattr(channel, "topic", None)
        if topic:
            embed.add_field(name="Topic", value=truncate(str(topic), 300), inline=False)
        slowmode = getattr(channel, "slowmode_delay", None)
        embed.add_field(name="Slowmode", value=f"{slowmode}s" if slowmode else "Off", inline=True)
        nsfw = getattr(channel, "nsfw", None)
        if nsfw is not None:
            embed.add_field(name="NSFW", value="Yes" if nsfw else "No", inline=True)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            embed.add_field(name="Bitrate", value=f"{channel.bitrate // 1000} kbps", inline=True)
            embed.add_field(name="User limit", value=str(channel.user_limit) or "Unlimited", inline=True)
        embed.add_field(name="Created", value=format_ts(int(channel.created_at.timestamp())), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show the server icon.")
    @app_commands.guild_only()
    async def icon(self, interaction: discord.Interaction) -> None:
        """Post the guild icon at full size."""
        guild = interaction.guild
        assert guild is not None
        if guild.icon is None:
            await interaction.response.send_message(
                embed=info_embed("This server does not have an icon yet."), ephemeral=True
            )
            return
        url = guild.icon.with_size(4096).url
        embed = base_embed(title=f"{guild.name} — icon", color=config.THEME_COLOR, image=url)
        embed.add_field(name="Link", value=f"[open in browser]({url})", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show the server banner.")
    @app_commands.guild_only()
    async def banner(self, interaction: discord.Interaction) -> None:
        """Post the guild banner at full size."""
        guild = interaction.guild
        assert guild is not None
        if guild.banner is None:
            await interaction.response.send_message(
                embed=warn_embed(
                    "This server does not have a banner. Banners unlock at **boost level 1** "
                    "with enough boosts."
                ),
                ephemeral=True,
            )
            return
        url = guild.banner.with_size(4096).url
        embed = base_embed(title=f"{guild.name} — banner", color=config.THEME_COLOR, image=url)
        embed.add_field(name="Link", value=f"[open in browser]({url})", inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Load the fun cog."""
    await bot.add_cog(Fun(bot))
