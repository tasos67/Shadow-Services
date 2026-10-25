"""Small shared helpers: durations, placeholders, cooldowns and more."""
from __future__ import annotations

import re
import time
import random
import string
from typing import Optional, Union

import discord

_DURATION_RE = re.compile(
    r"(?:(?P<weeks>\d+)\s*w)?\s*(?:(?P<days>\d+)\s*d)?\s*(?:(?P<hours>\d+)\s*h)?"
    r"\s*(?:(?P<minutes>\d+)\s*m)?\s*(?:(?P<seconds>\d+)\s*s)?",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(text: str) -> Optional[int]:
    """Parse a human duration like ``1d2h30m`` into seconds.

    Supports w(eeks), d(ays), h(ours), m(inutes), s(econds), with or without
    spaces. Returns ``None`` when the text contains no valid unit.
    """
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    total = 0
    found = False
    for match in re.finditer(r"(\d+)\s*([wdhms])", text):
        total += int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        found = True
    return total if found else None


def format_duration(seconds: int) -> str:
    """Format seconds into a compact human string such as ``2d 3h 4m``."""
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "0s"
    parts: list[str] = []
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{label}")
    return " ".join(parts[:3]) if len(parts) > 1 else parts[0]


def format_ts(epoch: Optional[int], style: str = "F") -> str:
    """Format an epoch into a Discord timestamp string, or 'never'."""
    if not epoch:
        return "never"
    return f"<t:{int(epoch)}:{style}>"


def remaining(ends_at: float) -> str:
    """Human friendly remaining time until an epoch timestamp."""
    diff = int(ends_at - time.time())
    if diff <= 0:
        return "any moment now"
    return f"{format_duration(diff)} left"


def truncate(text: str, limit: int = 1024) -> str:
    """Truncate text to a limit, appending an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def clean(text: Optional[str], limit: int = 4000) -> str:
    """Sanitise user input for embeds (strip mentions, truncate)."""
    if not text:
        return ""
    return truncate(str(text), limit)


_ID_RE = re.compile(r"^\d{15,25}$")
_MENTION_RE = re.compile(r"<[@#&!]?\d{15,25}>")


def parse_id(text: Optional[str]) -> Optional[int]:
    """Extract a snowflake id from a raw id or mention string."""
    if not text:
        return None
    text = text.strip()
    if _ID_RE.match(text):
        return int(text)
    match = _MENTION_RE.search(text)
    if match:
        digits = re.sub(r"\D", "", match.group(0))
        if digits:
            return int(digits)
    return None


def placeholder_format(
    text: str,
    *,
    member: Optional[discord.Member] = None,
    guild: Optional[discord.Guild] = None,
    extra: Optional[dict[str, str]] = None,
) -> str:
    """Replace ``{user}``, ``{server}``, ``{membercount}`` and friends.

    Supported placeholders:
      {user}, {user_mention}, {user_name}, {display_name}, {server},
      {member_count}, {channel}, {boost_count}, plus anything in ``extra``.
    """
    if not text:
        return text
    values: dict[str, str] = {}
    if guild is not None:
        values["server"] = guild.name
        values["server_name"] = guild.name
        values["member_count"] = str(guild.member_count)
        values["membercount"] = str(guild.member_count)
        values["boost_count"] = str(guild.premium_subscription_count)
    if member is not None:
        values["user"] = member.mention
        values["user_mention"] = member.mention
        values["user_name"] = str(member)
        values["display_name"] = member.display_name
        values["id"] = str(member.id)
    values.update(extra or {})
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


# ---------------------------------------------------------------------------
# In-memory per-user cooldowns (rate-limit friendly, no DB writes)
# ---------------------------------------------------------------------------
class _CooldownEntry:
    __slots__ = ("expires", "uses")

    def __init__(self, expires: float) -> None:
        self.expires = expires
        self.uses = 1


_COOLDOWNS: dict[str, _CooldownEntry] = {}


def cooldown(key: Union[str, int], command: str, seconds: float) -> Optional[float]:
    """Register a usage of ``command`` for ``key``.

    Returns ``None`` when allowed, otherwise the seconds left until the
    cooldown expires.
    """
    now = time.time()
    cache_key = f"{key}:{command}"
    entry = _COOLDOWNS.get(cache_key)
    if entry is None or entry.expires <= now:
        _COOLDOWNS[cache_key] = _CooldownEntry(now + seconds)
        return None
    entry.expires = max(entry.expires, now + seconds)
    return entry.expires - now


def check_cooldown(key: Union[str, int], command: str, seconds: float) -> Optional[float]:
    """Check a cooldown without consuming it. Returns seconds left or None."""
    now = time.time()
    entry = _COOLDOWNS.get(f"{key}:{command}")
    if entry is None or entry.expires <= now:
        return None
    return entry.expires - now


def random_string(length: int = 12) -> str:
    """Return a random alphanumeric string (used for temp file names)."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def chunk_list(items: list, size: int) -> list[list]:
    """Split a list into chunks of ``size``."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def ordinal(n: int) -> str:
    """Return an ordinal number string such as ``1st``."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def level_from_xp(xp: int) -> int:
    """Compute the level for a total XP amount (quadratic curve)."""
    level = 0
    while xp >= _xp_for_level(level + 1):
        level += 1
    return level


def xp_for_level(level: int) -> int:
    """Total XP required to reach ``level``."""
    return _xp_for_level(level)


def _xp_for_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100
