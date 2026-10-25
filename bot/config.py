"""Central configuration loaded from environment variables (.env).

All runtime values (token, owner id, dev guilds) come from the environment.
Paths used for storage, logs and transcripts are defined here as well.
"""
from __future__ import annotations

import os
from pathlib import Path

import discord
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
DATABASE_PATH = DATA_DIR / "bot.db"
LOG_FILE = LOG_DIR / "bot.log"

for _dir in (DATA_DIR, LOG_DIR, TRANSCRIPT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID: int = int(os.getenv("OWNER_ID", "0") or 0)


def _parse_dev_guilds() -> list[int]:
    """Parse the optional DEV_GUILD_IDS env variable (comma separated)."""
    raw = os.getenv("DEV_GUILD_IDS", "")
    guilds: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            guilds.append(int(part))
    return guilds


DEV_GUILD_IDS: list[int] = _parse_dev_guilds()


def validate() -> None:
    """Raise a clear error when required configuration is missing."""
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your-bot-token-here":
        raise RuntimeError(
            "DISCORD_TOKEN is not configured. Copy .env.example to .env and set your bot token."
        )
    if OWNER_ID == 0:
        raise RuntimeError("OWNER_ID is not configured. Copy .env.example to .env and set your user id.")


# ---------------------------------------------------------------------------
# Branding / theme
# ---------------------------------------------------------------------------
THEME_COLOR = discord.Color.from_str("#5865F2")   # embeds everywhere
SUCCESS_COLOR = discord.Color.from_str("#57F287")
ERROR_COLOR = discord.Color.from_str("#ED4245")
WARNING_COLOR = discord.Color.from_str("#FEE75C")
INFO_COLOR = discord.Color.from_str("#00A8FF")
DANGER_COLOR = discord.Color.from_str("#E0443F")

BOT_NAME = "AllInOne"
FOOTER_TEXT = f"{BOT_NAME} • built with discord.py"

# ---------------------------------------------------------------------------
# Behaviour defaults (overridable per guild through the settings system)
# ---------------------------------------------------------------------------
DEFAULT_OPEN_TICKETS_PER_USER = 1
DEFAULT_TICKET_INACTIVITY_MINUTES = 60 * 24  # auto-close after 24h inactivity
DEFAULT_XP_MIN = 15
DEFAULT_XP_MAX = 35
DEFAULT_XP_COOLDOWN = 60          # seconds between two XP grants
DEFAULT_VOICE_XP_INTERVAL = 60   # seconds of voice time per XP grant
DEFAULT_LEVEL_UP_ROLE_CAP = 15   # how many level-up roles can be configured
DEFAULT_SUGGESTION_EMOJI = "💡"
STARBOARD_DEFAULT_COUNT = 3
GIVEAWAY_DEFAULT_MINUTES = 60 * 24
AUTO_RESPONSE_LIMIT = 200
TAG_LIMIT = 300
SHOP_ITEM_LIMIT = 50


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------
def build_intents() -> discord.Intents:
    """Return the intents the bot needs.

    The following **privileged** intents must be enabled in the Discord
    Developer Portal (Bot -> Privileged Gateway Intents):
      * SERVER MEMBERS INTENT
      * MESSAGE CONTENT INTENT
      * PRESENCE INTENT (optional, used for /botinfo presence data)
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True          # privileged
    intents.messages = True
    intents.message_content = True  # privileged
    intents.reactions = True
    intents.invites = True
    intents.voice_states = True
    intents.moderation = True
    intents.presences = True         # privileged (presence intent)
    return intents
