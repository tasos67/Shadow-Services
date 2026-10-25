"""Security & protection.

Text auto-moderation (anti-spam, anti-link, anti-invite, anti-caps,
anti-mass-mention and bad words), anti-raid (join floods + account age gate),
anti-nuke (destructive audit-log actions + mass bans), whitelists, manual
lockdowns and the per-guild security configuration commands.

Global kill switch: ``sec_enabled``. Every protection reads its own ``sec_*``
setting and is a no-op unless security is enabled.
"""
from __future__ import annotations

import collections
import logging
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database
from cogs.core import SetupCategory, register_setup_category
from utils import checks, moderation
from utils.embeds import base_embed, error_embed, info_embed, success_embed, warn_embed
from utils.helpers import parse_id, truncate
from utils.paginator import paginate

log = logging.getLogger("cogs.security")

# ---------------------------------------------------------------------------
# Defaults (all overridable per guild through /security …)
# ---------------------------------------------------------------------------
DEFAULT_ANTISPAM_THRESHOLD = 5
DEFAULT_ANTISPAM_WINDOW = 5
DEFAULT_CAPS_PERCENT = 70
DEFAULT_CAPS_MIN_LENGTH = 12
DEFAULT_MENTION_LIMIT = 5
DEFAULT_ANTIRAID_JOINS = 10
DEFAULT_ANTIRAID_WINDOW = 15
DEFAULT_ANTIRAID_ACTION = "kick"
DEFAULT_MIN_ACCOUNT_AGE = 7
DEFAULT_ANTINUKE_THRESHOLD = 3
DEFAULT_ANTINUKE_WINDOW = 10
DEFAULT_ANTINUKE_ACTION = "remove_permissions"
DEFAULT_PUNISHMENT = "delete"
DEFAULT_TIMEOUT_SECONDS = 600

PUNISHMENTS = ("delete", "warn", "timeout", "kick", "ban")
RAID_ACTIONS = ("kick", "ban", "timeout")
NUKE_ACTIONS = ("remove_permissions", "ban")

# Toggle choice value -> settings key (``master``/``all`` are handled in code)
RULE_TOGGLES: dict[str, str] = {
    "antispam": "sec_antispam",
    "antilink": "sec_antilink",
    "antinvite": "sec_antinvite",
    "anticaps": "sec_anticaps",
    "antimention": "sec_antimention",
    "badwords": "sec_badwords",
    "antiraid": "sec_antiraid",
    "antinuke": "sec_antinuke",
}

# Per-rule punishment override keys
PUNISHMENT_OVERRIDES: dict[str, str] = {
    "link": "sec_antilink_punishment",
    "invite": "sec_antinvite_punishment",
    "badwords": "sec_badwords_punishment",
}

# Threshold choice value -> settings key
THRESHOLD_KEYS: dict[str, str] = {
    "spam": "sec_antispam_threshold",
    "spam_window": "sec_antispam_window",
    "caps": "sec_anticaps_percent",
    "caps_min_length": "sec_anticaps_min_length",
    "mentions": "sec_antimention_limit",
    "antiraid_joins": "sec_antiraid_joins",
    "antiraid_window": "sec_antiraid_window",
    "antinuke_threshold": "sec_antinuke_threshold",
    "antinuke_window": "sec_antinuke_window",
}

# Destructive audit-log actions tracked by anti-nuke
NUKE_AUDIT_ACTIONS: dict[discord.AuditLogAction, str] = {
    discord.AuditLogAction.channel_delete: "channel delete",
    discord.AuditLogAction.channel_create: "channel create",
    discord.AuditLogAction.role_delete: "role delete",
    discord.AuditLogAction.role_create: "role create",
    discord.AuditLogAction.ban: "member ban",
    discord.AuditLogAction.kick: "member kick",
    discord.AuditLogAction.webhook_create: "webhook create",
    discord.AuditLogAction.member_prune: "member prune",
    discord.AuditLogAction.emoji_delete: "emoji delete",
}

DANGEROUS_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "ban_members",
    "manage_webhooks",
)

LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/\S+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# In-memory sliding-window state (never persisted; pruned by cleanup_loop)
# ---------------------------------------------------------------------------
_SPAM: dict[tuple[int, int], collections.deque[float]] = {}
_NUKE: dict[tuple[int, int], collections.deque[float]] = {}
_BANS: dict[tuple[int, int], collections.deque[float]] = {}
_JOINS: dict[int, collections.deque[tuple[float, int]]] = {}
# guild_id -> {channel_id: previous send_messages overwrite value}
_LOCKDOWN: dict[int, dict[int, Optional[bool]]] = {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _extract_domain(url: str) -> str:
    """Return the bare domain (no scheme, www, port or path) of a URL."""
    domain = str(url).strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0]
    domain = domain.rsplit("@", 1)[-1]
    domain = domain.split(":", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _first_disallowed_link(content: str, allowed: list) -> Optional[str]:
    """Return the first URL whose domain is not on the allow-list, if any."""
    allow = {d for d in (_extract_domain(entry) for entry in allowed or []) if d}
    for match in LINK_RE.finditer(content):
        url = match.group(0).rstrip(").,!?\"'")
        domain = _extract_domain(url)
        if not domain:
            continue
        if any(domain == entry or domain.endswith("." + entry) for entry in allow):
            continue
        return url
    return None


def _match_badword(content: str, words: list) -> Optional[str]:
    """Return the first forbidden word found in the content (word boundaries)."""
    for word in words or []:
        cleaned = str(word).strip()
        if not cleaned:
            continue
        if re.search(r"\b" + re.escape(cleaned) + r"\b", content, re.IGNORECASE):
            return cleaned
    return None


def _is_excessive_caps(content: str, percent: int, min_length: int) -> bool:
    """Whether the message is mostly uppercase and long enough to matter."""
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) < max(1, min_length):
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return (upper / len(letters)) * 100 >= percent


def _unique_mentions(message: discord.Message) -> int:
    """Number of distinct members/roles mentioned in a message."""
    ids = {member.id for member in message.mentions}
    ids |= {role.id for role in message.role_mentions}
    if message.mention_everyone:
        ids.add(0)
    return len(ids)


def _role_is_dangerous(role: discord.Role) -> bool:
    """Whether a role grants any permission considered dangerous by anti-nuke."""
    perms = role.permissions
    return any(getattr(perms, name, False) for name in DANGEROUS_PERMISSIONS)


def _classify_target(guild: discord.Guild, text: str) -> tuple[Optional[str], Optional[int], str]:
    """Resolve a mention/ID/name into a ``("user"|"role", id, label)`` triple."""
    text = (text or "").strip()
    if not text:
        return None, None, ""
    if text.startswith("<@&"):
        role_id = parse_id(text)
        role = guild.get_role(role_id) if role_id else None
        return "role", role_id, role.mention if role else f"<@&{role_id}>"
    if text.startswith("<@"):
        user_id = parse_id(text)
        member = guild.get_member(user_id) if user_id else None
        return "user", user_id, member.mention if member else f"<@{user_id}>"
    snowflake = parse_id(text)
    if snowflake is None:
        role = discord.utils.find(lambda r: r.name.lower() == text.lower(), guild.roles)
        if role is not None:
            return "role", role.id, role.mention
        member = discord.utils.find(
            lambda m: m.name.lower() == text.lower() or m.display_name.lower() == text.lower(),
            guild.members,
        )
        if member is not None:
            return "user", member.id, member.mention
        return None, None, ""
    role = guild.get_role(snowflake)
    if role is not None:
        return "role", snowflake, role.mention
    member = guild.get_member(snowflake)
    if member is not None:
        return "user", snowflake, member.mention
    return "user", snowflake, f"`{snowflake}`"


# ---------------------------------------------------------------------------
# /setup wizard category
# ---------------------------------------------------------------------------
async def _apply_security_settings(guild: discord.Guild, values: dict[str, str]) -> str:
    """Parse and save the security settings supplied by the /setup wizard."""
    lines: list[str] = []

    enabled_raw = values.get("enabled", "").strip().lower()
    if enabled_raw:
        enabled = enabled_raw in {"yes", "y", "true", "on", "1", "enable", "enabled"}
        await database.db.set(guild.id, "sec_enabled", enabled)
        lines.append(f"Security: {'enabled' if enabled else 'disabled'}")

    punishment = values.get("punishment", "").strip().lower()
    if punishment in PUNISHMENTS:
        await database.db.set(guild.id, "sec_punishment", punishment)
        lines.append(f"Punishment: {punishment}")
    elif punishment:
        lines.append(f"Punishment: ignored (use {'/'.join(PUNISHMENTS)})")

    age_raw = values.get("min_account_age", "").strip()
    if age_raw.isdigit():
        age = int(age_raw)
        await database.db.set(guild.id, "sec_min_account_age", age)
        lines.append(f"Minimum account age: {age} day(s)")

    log_channel_id = parse_id(values.get("log_channel", ""))
    if log_channel_id:
        await database.db.set(guild.id, "sec_log_channel", log_channel_id)
        lines.append(f"Log channel: <#{log_channel_id}>")

    role_ids = [
        rid
        for part in values.get("whitelist_roles", "").replace(",", " ").split()
        if (rid := parse_id(part))
    ]
    if role_ids:
        await database.db.set(guild.id, "whitelist_roles", role_ids)
        lines.append("Whitelisted roles: " + ", ".join(f"<@&{rid}>" for rid in role_ids))

    return "\n".join(lines) or "No changes were made."


register_setup_category(
    SetupCategory(
        key="security",
        label="Security",
        emoji="🔒",
        description="Auto-moderation, punishment, account age, log channel and whitelist.",
        fields=[
            ("Enable security", "yes / no", True, "enabled"),
            ("Punishment", "delete / warn / timeout / kick / ban", False, "punishment"),
            ("Minimum account age (days)", "7", False, "min_account_age"),
            ("Security log channel", "#security-logs", False, "log_channel"),
            ("Whitelisted roles", "@Staff @Moderator", False, "whitelist_roles"),
        ],
        handler=_apply_security_settings,
    )
)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class Security(commands.Cog):
    """Auto-moderation, anti-raid, anti-nuke and security configuration."""

    security_group = app_commands.Group(
        name="security",
        description="Security, anti-raid, anti-nuke and auto-moderation.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Manage the user/role whitelist used by every protection.",
        parent=security_group,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def cog_unload(self) -> None:
        """Stop the periodic cleanup task when the cog unloads."""
        self.cleanup_loop.cancel()

    # -- small helpers ------------------------------------------------------
    def _moderator(self, guild: discord.Guild) -> discord.abc.User:
        """The bot user, used as the moderator on automated cases."""
        return guild.me or self.bot.user  # type: ignore[return-value]

    async def _security_active(self, guild: discord.Guild) -> bool:
        """Whether the global security kill switch is on for a guild."""
        return await database.db.get_bool(guild.id, "sec_enabled", False)

    async def _rule_punishment(self, guild_id: int, rule: str) -> str:
        """Resolve the punishment for a text rule (per-rule override or default)."""
        override_key = PUNISHMENT_OVERRIDES.get(rule)
        if override_key:
            value = await database.db.get(guild_id, override_key)
            if value in PUNISHMENTS:
                return str(value)
        value = await database.db.get(guild_id, "sec_punishment", DEFAULT_PUNISHMENT)
        return str(value) if value in PUNISHMENTS else DEFAULT_PUNISHMENT

    async def _alert(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Send an embed to the configured security log channel."""
        channel_id = await database.db.get(guild.id, "sec_log_channel")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                log.warning("Could not deliver security alert in guild %s", guild.id)

    @staticmethod
    async def _record(guild_id: int, user_id: int, rule: str, action: str, reason: str) -> None:
        """Persist one automod trigger into the automod_actions table."""
        await database.db.execute(
            "INSERT INTO automod_actions (guild_id, user_id, rule, action, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, rule, action, truncate(reason, 500), int(time.time())),
        )

    def _register_spam(self, guild_id: int, user_id: int, threshold: int, window: int) -> bool:
        """Track a message for anti-spam; True when the threshold is exceeded."""
        now = time.monotonic()
        bucket = _SPAM.setdefault((guild_id, user_id), collections.deque())
        bucket.append(now)
        cutoff = now - max(1, window)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max(2, threshold):
            bucket.clear()
            return True
        return False

    # -- message auto-moderation -------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Run every enabled text rule against a guild message."""
        try:
            await self._process_message(message)
        except Exception:
            log.exception("Security on_message handler failed")

    async def _process_message(self, message: discord.Message) -> None:
        """Apply anti-spam/link/invite/caps/mention/bad-words to a message."""
        guild = message.guild
        author = message.author
        if guild is None or author.bot or not isinstance(author, discord.Member):
            return
        if not await self._security_active(guild):
            return
        # staff and whitelisted members are never auto-moderated
        if author.guild_permissions.manage_messages:
            return
        if await checks.is_whitelisted(author):
            return

        ignore = [int(cid) for cid in await database.db.get(guild.id, "sec_ignore_channels", [])]
        if message.channel.id in ignore:
            return

        content = message.content or ""

        if await database.db.get_bool(guild.id, "sec_antispam", False):
            threshold = await database.db.get_int(guild.id, "sec_antispam_threshold", DEFAULT_ANTISPAM_THRESHOLD)
            window = await database.db.get_int(guild.id, "sec_antispam_window", DEFAULT_ANTISPAM_WINDOW)
            if self._register_spam(guild.id, author.id, threshold, window):
                await self._punish_message(
                    message, "spam", f"Sending messages too quickly ({threshold} in {window}s)"
                )
                return

        if await database.db.get_bool(guild.id, "sec_antinvite", False) and INVITE_RE.search(content):
            await self._punish_message(message, "invite", "Posting a Discord invite link")
            return

        if await database.db.get_bool(guild.id, "sec_antilink", False):
            allowed = await database.db.get(guild.id, "sec_antilink_allow", [])
            url = _first_disallowed_link(content, allowed)
            if url:
                await self._punish_message(message, "link", f"Posting a link (`{truncate(url, 120)}`)")
                return

        if await database.db.get_bool(guild.id, "sec_anticaps", False):
            percent = await database.db.get_int(guild.id, "sec_anticaps_percent", DEFAULT_CAPS_PERCENT)
            min_length = await database.db.get_int(guild.id, "sec_anticaps_min_length", DEFAULT_CAPS_MIN_LENGTH)
            if _is_excessive_caps(content, percent, min_length):
                await self._punish_message(message, "caps", "Excessive use of capital letters")
                return

        if await database.db.get_bool(guild.id, "sec_antimention", False):
            limit = await database.db.get_int(guild.id, "sec_antimention_limit", DEFAULT_MENTION_LIMIT)
            mentions = _unique_mentions(message)
            if mentions >= max(2, limit):
                await self._punish_message(message, "mention", f"Mass mentioning ({mentions} mentions)")
                return

        if await database.db.get_bool(guild.id, "sec_badwords", False):
            badwords = await database.db.get(guild.id, "sec_badwords_list", [])
            hit = _match_badword(content, badwords)
            if hit:
                await self._punish_message(message, "badwords", f"Use of a forbidden word (`{hit}`)")
                return

    async def _punish_message(self, message: discord.Message, rule: str, reason: str) -> None:
        """Apply the configured punishment for a text auto-moderation trigger."""
        guild = message.guild
        if guild is None or not isinstance(message.author, discord.Member):
            return
        member = message.author
        punishment = await self._rule_punishment(guild.id, rule)
        await self._record(guild.id, member.id, rule, punishment, reason)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        moderator = self._moderator(guild)
        try:
            if punishment == "delete":
                try:
                    await message.channel.send(
                        embed=warn_embed(
                            f"{member.mention}, your message was removed (**{reason}**).",
                            title="Auto-moderation",
                        ),
                        delete_after=6,
                        allowed_mentions=discord.AllowedMentions(
                            users=True, roles=False, everyone=False
                        ),
                    )
                except discord.HTTPException:
                    pass
            elif punishment == "warn":
                await database.db.execute(
                    "INSERT INTO warnings (guild_id, user_id, mod_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (guild.id, member.id, moderator.id, reason, int(time.time())),
                )
                await moderation.log_action(
                    guild, user=member, moderator=moderator, action="warn", reason=reason
                )
                await moderation.apply_escalation(guild, member, moderator)
            elif punishment == "timeout":
                await moderation.timeout_member(member, DEFAULT_TIMEOUT_SECONDS, reason)
                await moderation.log_action(
                    guild,
                    user=member,
                    moderator=moderator,
                    action="timeout",
                    reason=reason,
                    duration=f"{DEFAULT_TIMEOUT_SECONDS // 60} minutes",
                )
            elif punishment == "kick":
                await member.kick(reason=reason)
                await moderation.log_action(
                    guild, user=member, moderator=moderator, action="kick", reason=reason
                )
            elif punishment == "ban":
                await member.ban(reason=reason, delete_message_days=0)
                await moderation.log_action(
                    guild, user=member, moderator=moderator, action="ban", reason=reason
                )
        except discord.HTTPException:
            log.exception("Auto-moderation %s failed for %s in guild %s", rule, member.id, guild.id)

        await self._alert(
            guild,
            base_embed(
                title="🛡️ Auto-moderation triggered",
                color=config.WARNING_COLOR,
                fields=[
                    ("User", f"{member.mention} (`{member.id}`)", True),
                    ("Rule", rule, True),
                    ("Action", punishment, True),
                    ("Channel", message.channel.mention, True),
                    ("Reason", truncate(reason, 500), False),
                ],
            ),
        )

    # -- anti-raid -----------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Track joins for anti-raid and enforce the account-age gate."""
        try:
            await self._handle_join(member)
        except Exception:
            log.exception("Security on_member_join handler failed")

    async def _handle_join(self, member: discord.Member) -> None:
        """Account-age gate + join-flood detection for a new member."""
        guild = member.guild
        if not await self._security_active(guild):
            return
        if not await database.db.get_bool(guild.id, "sec_antiraid", False):
            return

        action = await database.db.get(guild.id, "sec_antiraid_action", DEFAULT_ANTIRAID_ACTION)
        if action not in RAID_ACTIONS:
            action = DEFAULT_ANTIRAID_ACTION

        min_age = await database.db.get_int(guild.id, "sec_min_account_age", DEFAULT_MIN_ACCOUNT_AGE)
        if min_age > 0:
            age_days = (discord.utils.utcnow() - member.created_at).days
            if age_days < min_age:
                gate_action = "ban" if action == "ban" else "kick"
                await self._punish_join(
                    member, gate_action, f"Account younger than {min_age} day(s)"
                )
                return

        joins = await database.db.get_int(guild.id, "sec_antiraid_joins", DEFAULT_ANTIRAID_JOINS)
        window = await database.db.get_int(guild.id, "sec_antiraid_window", DEFAULT_ANTIRAID_WINDOW)
        now = time.monotonic()
        bucket = _JOINS.setdefault(guild.id, collections.deque())
        bucket.append((now, member.id))
        cutoff = now - max(1, window)
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()
        if len(bucket) < max(2, joins):
            return

        recent = [user_id for _, user_id in bucket]
        bucket.clear()
        await self._trigger_raid(guild, action, recent, window)

    async def _trigger_raid(
        self, guild: discord.Guild, action: str, user_ids: list[int], window: int
    ) -> None:
        """Respond to a raid: optionally lock down, then punish the joiners."""
        lockdown = await database.db.get_bool(guild.id, "sec_antiraid_lockdown", True)
        note = ""
        if lockdown:
            locked = await self._set_lockdown(guild, True)
            note = f"\nLocked **{locked}** text channel(s)."

        punished = 0
        for user_id in user_ids:
            member = guild.get_member(user_id)
            if member is None or member.guild_permissions.manage_messages:
                continue
            if await checks.is_whitelisted(member):
                continue
            await self._punish_join(member, action, "Anti-raid: mass join detected")
            punished += 1

        await self._alert(
            guild,
            base_embed(
                title="🚨 Raid detected",
                description=(
                    f"**{len(user_ids)}** account(s) joined within {window}s."
                    f"\nAction: **{action}** • punished: **{punished}**{note}"
                ),
                color=config.ERROR_COLOR,
            ),
        )

    async def _punish_join(self, member: discord.Member, action: str, reason: str) -> None:
        """Kick/ban/timeout a member who tripped anti-raid or the account-age gate."""
        guild = member.guild
        await self._record(guild.id, member.id, "antiraid", action, reason)
        verb = {"ban": "banned", "kick": "kicked", "timeout": "timed out"}.get(action, action)
        try:
            await member.send(
                embed=base_embed(
                    title=f"You were {verb} from {guild.name}",
                    description=reason,
                    color=config.ERROR_COLOR,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        me = guild.me
        if me is None:
            return
        moderator = self._moderator(guild)
        try:
            if action == "ban" and me.guild_permissions.ban_members:
                await moderation.log_action(
                    guild, user=member, moderator=moderator, action="ban", reason=reason, dm=False
                )
                await member.ban(reason=reason, delete_message_days=0)
            elif action == "kick" and me.guild_permissions.kick_members:
                await moderation.log_action(
                    guild, user=member, moderator=moderator, action="kick", reason=reason, dm=False
                )
                await member.kick(reason=reason)
            elif action == "timeout" and me.guild_permissions.moderate_members:
                await moderation.timeout_member(member, DEFAULT_TIMEOUT_SECONDS, reason)
                await moderation.log_action(
                    guild,
                    user=member,
                    moderator=moderator,
                    action="timeout",
                    reason=reason,
                    duration=f"{DEFAULT_TIMEOUT_SECONDS // 60} minutes",
                    dm=False,
                )
        except discord.HTTPException:
            log.exception("Anti-raid action %s failed for %s", action, member.id)

        await self._alert(
            guild,
            base_embed(
                title="🛡️ Anti-raid action",
                color=config.ERROR_COLOR,
                fields=[
                    ("Account", f"{member} (`{member.id}`)", True),
                    ("Action", action, True),
                    ("Reason", truncate(reason, 500), False),
                ],
            ),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Prune stale join records so the raid counter calms down over time."""
        try:
            bucket = _JOINS.get(member.guild.id)
            if not bucket:
                return
            window = await database.db.get_int(
                member.guild.id, "sec_antiraid_window", DEFAULT_ANTIRAID_WINDOW
            )
            cutoff = time.monotonic() - max(1, window)
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()
            if not bucket:
                _JOINS.pop(member.guild.id, None)
        except Exception:
            log.exception("Security on_member_remove handler failed")

    # -- lockdown -----------------------------------------------------------
    async def _set_lockdown(self, guild: discord.Guild, enabled: bool) -> int:
        """Lock or unlock every text channel; returns how many changed."""
        changed = 0
        if enabled:
            stored = _LOCKDOWN.setdefault(guild.id, {})
            for channel in guild.text_channels:
                current = channel.overwrites_for(guild.default_role)
                if current.send_messages is False:
                    continue
                previous = current.send_messages
                current.send_messages = False
                try:
                    await channel.set_permissions(
                        guild.default_role, overwrite=current, reason="Security lockdown enabled"
                    )
                except discord.HTTPException:
                    log.warning("Could not lock channel %s in guild %s", channel.id, guild.id)
                    continue
                stored[channel.id] = previous
                changed += 1
            return changed

        for channel_id, previous in _LOCKDOWN.pop(guild.id, {}).items():
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            current = channel.overwrites_for(guild.default_role)
            current.send_messages = previous
            try:
                await channel.set_permissions(
                    guild.default_role, overwrite=current, reason="Security lockdown lifted"
                )
                changed += 1
            except discord.HTTPException:
                log.warning("Could not unlock channel %s in guild %s", channel_id, guild.id)
        return changed

    # -- anti-nuke ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Track destructive audit-log actions for anti-nuke."""
        try:
            await self._handle_audit_entry(entry)
        except Exception:
            log.exception("Security audit-log handler failed")

    async def _handle_audit_entry(self, entry: discord.AuditLogEntry) -> None:
        """Count destructive actions per actor and trigger anti-nuke if needed."""
        guild = entry.guild
        actor = entry.user
        if guild is None or actor is None:
            return
        label = NUKE_AUDIT_ACTIONS.get(entry.action)
        if label is None:
            return
        if not await self._security_active(guild):
            return
        if not await database.db.get_bool(guild.id, "sec_antinuke", False):
            return

        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return
        if self.bot.user is not None and actor.id == self.bot.user.id:
            return
        if actor.id == guild.owner_id:
            return
        member = guild.get_member(actor.id)
        if member is not None and await checks.is_whitelisted(member):
            return

        threshold = await database.db.get_int(
            guild.id, "sec_antinuke_threshold", DEFAULT_ANTINUKE_THRESHOLD
        )
        window = await database.db.get_int(guild.id, "sec_antinuke_window", DEFAULT_ANTINUKE_WINDOW)
        now = time.monotonic()
        bucket = _NUKE.setdefault((guild.id, actor.id), collections.deque())
        bucket.append(now)
        cutoff = now - max(1, window)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) < max(2, threshold):
            return
        count = len(bucket)
        bucket.clear()
        await self._nuke_response(
            guild, actor, f"{count} destructive actions ({label}) within {window}s"
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Detect mass bans performed by a single actor."""
        try:
            await self._handle_mass_ban(guild, user)
        except Exception:
            log.exception("Security on_member_ban handler failed")

    async def _handle_mass_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Track bans per actor and trigger anti-nuke on a burst."""
        if not await self._security_active(guild):
            return
        if not await database.db.get_bool(guild.id, "sec_antinuke", False):
            return
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return

        entry: Optional[discord.AuditLogEntry] = None
        try:
            async for candidate in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
                entry = candidate
                break
        except discord.HTTPException:
            log.warning("Could not read audit log for mass-ban detection in guild %s", guild.id)
            return
        if entry is None or entry.user is None:
            return
        actor = entry.user
        if self.bot.user is not None and actor.id == self.bot.user.id:
            return
        if actor.id == guild.owner_id:
            return
        member = guild.get_member(actor.id)
        if member is not None and await checks.is_whitelisted(member):
            return

        window = await database.db.get_int(guild.id, "sec_antinuke_window", DEFAULT_ANTINUKE_WINDOW)
        now = time.monotonic()
        bucket = _BANS.setdefault((guild.id, actor.id), collections.deque())
        bucket.append(now)
        cutoff = now - max(1, window)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) < 3:
            return
        count = len(bucket)
        bucket.clear()
        await self._nuke_response(guild, actor, f"{count} bans within {window}s")

    async def _nuke_response(self, guild: discord.Guild, actor: discord.User, reason: str) -> None:
        """Strip dangerous roles or ban a nuker, then alert the owner and logs."""
        action = await database.db.get(guild.id, "sec_antinuke_action", DEFAULT_ANTINUKE_ACTION)
        if action not in NUKE_ACTIONS:
            action = DEFAULT_ANTINUKE_ACTION
        me = guild.me
        offender = guild.get_member(actor.id)
        outcome = "no action (member unavailable)"
        try:
            if action == "ban" and offender is not None and me is not None and me.guild_permissions.ban_members:
                await offender.ban(reason=f"Anti-nuke: {reason}", delete_message_days=0)
                outcome = "banned"
            elif offender is not None:
                removed = await self._strip_dangerous_roles(offender, f"Anti-nuke: {reason}")
                outcome = f"removed {removed} dangerous role(s)"
            elif action == "ban":
                outcome = "could not ban (missing permission)"
        except discord.HTTPException:
            log.exception("Anti-nuke action failed for %s in guild %s", actor.id, guild.id)
            outcome = "action failed"

        await moderation.log_action(
            guild,
            user=actor,
            moderator=self._moderator(guild),
            action="automod",
            reason=f"Anti-nuke: {reason}",
            dm=False,
        )
        embed = base_embed(
            title="💥 Anti-nuke triggered",
            color=config.ERROR_COLOR,
            fields=[
                ("User", f"{actor.mention} (`{actor.id}`)", True),
                ("Action", f"{action} → {outcome}", True),
                ("Reason", truncate(reason, 500), False),
            ],
        )
        await self._alert(guild, embed)
        if await database.db.get_bool(guild.id, "sec_antinuke_alert_owner", True):
            owner = guild.owner or guild.get_member(guild.owner_id)
            if owner is not None:
                try:
                    await owner.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    async def _strip_dangerous_roles(self, member: discord.Member, reason: str) -> int:
        """Remove every role below the bot that grants dangerous permissions."""
        me = member.guild.me
        if me is None:
            return 0
        removed = 0
        for role in list(member.roles):
            if role.is_default() or role.managed or role >= me.top_role:
                continue
            if not _role_is_dangerous(role):
                continue
            try:
                await member.remove_roles(role, reason=reason)
                removed += 1
            except discord.HTTPException:
                log.warning("Could not remove role %s from %s", role.id, member.id)
        return removed

    # =======================================================================
    # Commands
    # =======================================================================
    @security_group.command(name="toggle", description="Enable or disable a protection (or all of them).")
    @app_commands.describe(rule="Which protection to toggle", enabled="Whether it should be on")
    @app_commands.choices(
        rule=[
            app_commands.Choice(name="Master switch", value="master"),
            app_commands.Choice(name="Anti-spam", value="antispam"),
            app_commands.Choice(name="Anti-link", value="antilink"),
            app_commands.Choice(name="Anti-invite", value="antinvite"),
            app_commands.Choice(name="Anti-caps", value="anticaps"),
            app_commands.Choice(name="Anti-mass-mention", value="antimention"),
            app_commands.Choice(name="Bad words", value="badwords"),
            app_commands.Choice(name="Anti-raid", value="antiraid"),
            app_commands.Choice(name="Anti-nuke", value="antinuke"),
            app_commands.Choice(name="All rules", value="all"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_toggle(
        self, interaction: discord.Interaction, rule: app_commands.Choice[str], enabled: bool
    ) -> None:
        """Toggle one protection, the master switch or every rule at once."""
        guild = interaction.guild
        assert guild is not None
        if rule.value == "master":
            await database.db.set(guild.id, "sec_enabled", enabled)
            text = f"Security is now **{'enabled' if enabled else 'disabled'}**."
        elif rule.value == "all":
            for key in RULE_TOGGLES.values():
                await database.db.set(guild.id, key, enabled)
            await database.db.set(guild.id, "sec_enabled", enabled)
            text = f"All protections are now **{'enabled' if enabled else 'disabled'}**."
        else:
            await database.db.set(guild.id, RULE_TOGGLES[rule.value], enabled)
            text = f"**{rule.name}** is now **{'enabled' if enabled else 'disabled'}**."
        await interaction.response.send_message(embed=success_embed(text), ephemeral=True)

    @security_group.command(name="enable", description="Master switch for the whole security system.")
    @app_commands.describe(enabled="Turn all security protections on or off")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_enable(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the global security kill switch."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "sec_enabled", enabled)
        await interaction.response.send_message(
            embed=success_embed(f"Security is now **{'enabled' if enabled else 'disabled'}**."),
            ephemeral=True,
        )

    @security_group.command(name="config", description="Show the current security configuration.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_config(self, interaction: discord.Interaction) -> None:
        """Display every rule, threshold, punishment and whitelist count."""
        guild = interaction.guild
        assert guild is not None
        values = await database.db.get_all(guild.id)

        def flag(key: str, default: bool = False) -> str:
            return "on" if bool(values.get(key, default)) else "off"

        punishment = values.get("sec_punishment", DEFAULT_PUNISHMENT)
        log_channel = values.get("sec_log_channel")
        embed = base_embed(
            title="🔒 Security configuration",
            description=f"Master switch: **{flag('sec_enabled')}**",
            fields=[
                ("Log channel", f"<#{log_channel}>" if log_channel else "Not set", True),
                ("Default punishment", str(punishment), True),
                (
                    "Text rules",
                    f"Anti-spam: {flag('sec_antispam')}\n"
                    f"Anti-link: {flag('sec_antilink')}\n"
                    f"Anti-invite: {flag('sec_antinvite')}\n"
                    f"Anti-caps: {flag('sec_anticaps')}\n"
                    f"Anti-mention: {flag('sec_antimention')}\n"
                    f"Bad words: {flag('sec_badwords')}",
                    True,
                ),
                (
                    "Thresholds",
                    f"Spam: {values.get('sec_antispam_threshold', DEFAULT_ANTISPAM_THRESHOLD)} in "
                    f"{values.get('sec_antispam_window', DEFAULT_ANTISPAM_WINDOW)}s\n"
                    f"Caps: {values.get('sec_anticaps_percent', DEFAULT_CAPS_PERCENT)}% "
                    f"(min {values.get('sec_anticaps_min_length', DEFAULT_CAPS_MIN_LENGTH)})\n"
                    f"Mentions: {values.get('sec_antimention_limit', DEFAULT_MENTION_LIMIT)}",
                    True,
                ),
                (
                    "Per-rule punishment",
                    f"Link: {values.get('sec_antilink_punishment', punishment)}\n"
                    f"Invite: {values.get('sec_antinvite_punishment', punishment)}\n"
                    f"Bad words: {values.get('sec_badwords_punishment', punishment)}",
                    True,
                ),
                (
                    "Anti-raid",
                    f"Status: {flag('sec_antiraid')} • Action: "
                    f"{values.get('sec_antiraid_action', DEFAULT_ANTIRAID_ACTION)}\n"
                    f"Joins: {values.get('sec_antiraid_joins', DEFAULT_ANTIRAID_JOINS)} in "
                    f"{values.get('sec_antiraid_window', DEFAULT_ANTIRAID_WINDOW)}s\n"
                    f"Lockdown: {'on' if values.get('sec_antiraid_lockdown', True) else 'off'} • "
                    f"Min age: {values.get('sec_min_account_age', DEFAULT_MIN_ACCOUNT_AGE)}d",
                    False,
                ),
                (
                    "Anti-nuke",
                    f"Status: {flag('sec_antinuke')} • Action: "
                    f"{values.get('sec_antinuke_action', DEFAULT_ANTINUKE_ACTION)}\n"
                    f"Threshold: {values.get('sec_antinuke_threshold', DEFAULT_ANTINUKE_THRESHOLD)} in "
                    f"{values.get('sec_antinuke_window', DEFAULT_ANTINUKE_WINDOW)}s\n"
                    f"Alert owner: {'on' if values.get('sec_antinuke_alert_owner', True) else 'off'}",
                    False,
                ),
                (
                    "Whitelist",
                    f"Users: {len(values.get('whitelist_users', []))} • "
                    f"Roles: {len(values.get('whitelist_roles', []))}\n"
                    f"Ignored channels: {len(values.get('sec_ignore_channels', []))}",
                    False,
                ),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @security_group.command(name="threshold", description="Set a numeric security threshold.")
    @app_commands.describe(setting="Which threshold to change", value="The new value")
    @app_commands.choices(
        setting=[
            app_commands.Choice(name="Anti-spam messages", value="spam"),
            app_commands.Choice(name="Anti-spam window (seconds)", value="spam_window"),
            app_commands.Choice(name="Anti-caps percentage", value="caps"),
            app_commands.Choice(name="Anti-caps minimum length", value="caps_min_length"),
            app_commands.Choice(name="Anti-mention limit", value="mentions"),
            app_commands.Choice(name="Anti-raid joins", value="antiraid_joins"),
            app_commands.Choice(name="Anti-raid window (seconds)", value="antiraid_window"),
            app_commands.Choice(name="Anti-nuke threshold", value="antinuke_threshold"),
            app_commands.Choice(name="Anti-nuke window (seconds)", value="antinuke_window"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_threshold(
        self,
        interaction: discord.Interaction,
        setting: app_commands.Choice[str],
        value: app_commands.Range[int, 1, 100000],
    ) -> None:
        """Write a numeric value to the matching ``sec_*`` setting."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, THRESHOLD_KEYS[setting.value], int(value))
        await interaction.response.send_message(
            embed=success_embed(f"**{setting.name}** set to **{value}**."), ephemeral=True
        )

    @security_group.command(
        name="punishment", description="Set the default or per-rule auto-moderation punishment."
    )
    @app_commands.describe(
        action="What should happen when a rule triggers",
        rule="Optionally override only this rule",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Delete message", value="delete"),
            app_commands.Choice(name="Warn", value="warn"),
            app_commands.Choice(name="Timeout (10 minutes)", value="timeout"),
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Ban", value="ban"),
        ],
        rule=[
            app_commands.Choice(name="Links", value="link"),
            app_commands.Choice(name="Invites", value="invite"),
            app_commands.Choice(name="Bad words", value="badwords"),
        ],
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_punishment(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        rule: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Set the default punishment or a per-rule override."""
        guild = interaction.guild
        assert guild is not None
        if rule is None:
            await database.db.set(guild.id, "sec_punishment", action.value)
            await interaction.response.send_message(
                embed=success_embed(f"Default punishment set to **{action.name}**."), ephemeral=True
            )
            return
        await database.db.set(guild.id, PUNISHMENT_OVERRIDES[rule.value], action.value)
        await interaction.response.send_message(
            embed=success_embed(f"**{rule.name}** punishment set to **{action.name}**."), ephemeral=True
        )

    @security_group.command(name="badword", description="Add or remove a word from the bad-words filter.")
    @app_commands.describe(action="Add or remove the word", word="The word to add or remove")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_badword(
        self, interaction: discord.Interaction, action: app_commands.Choice[str], word: str
    ) -> None:
        """Maintain a single entry of the bad-words filter."""
        guild = interaction.guild
        assert guild is not None
        cleaned = word.strip().lower()
        if not cleaned or len(cleaned) > 100:
            await interaction.response.send_message(
                embed=error_embed("Provide a word between 1 and 100 characters."), ephemeral=True
            )
            return
        words = [str(w) for w in await database.db.get(guild.id, "sec_badwords_list", [])]
        if action.value == "add":
            if cleaned in words:
                await interaction.response.send_message(
                    embed=info_embed(f"`{cleaned}` is already filtered."), ephemeral=True
                )
                return
            words.append(cleaned)
            await database.db.set(guild.id, "sec_badwords_list", words)
            await interaction.response.send_message(
                embed=success_embed(f"Added `{cleaned}` to the filter ({len(words)} total)."),
                ephemeral=True,
            )
        else:
            if cleaned not in words:
                await interaction.response.send_message(
                    embed=info_embed(f"`{cleaned}` is not in the filter."), ephemeral=True
                )
                return
            words = [w for w in words if w != cleaned]
            await database.db.set(guild.id, "sec_badwords_list", words)
            await interaction.response.send_message(
                embed=success_embed(f"Removed `{cleaned}` from the filter ({len(words)} total)."),
                ephemeral=True,
            )

    @security_group.command(name="badwords", description="List every word in the bad-words filter.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_badwords(self, interaction: discord.Interaction) -> None:
        """Paginate through the bad-words filter."""
        guild = interaction.guild
        assert guild is not None
        words = [str(w) for w in await database.db.get(guild.id, "sec_badwords_list", [])]
        if not words:
            await interaction.response.send_message(
                embed=info_embed("The bad-words filter is empty."), ephemeral=True
            )
            return
        pages = [
            base_embed(
                title="Forbidden words",
                description="\n".join(f"`{word}`" for word in words[start : start + 30]),
                footer=f"{len(words)} word(s)",
            )
            for start in range(0, len(words), 30)
        ]
        await paginate(interaction, pages)

    @security_group.command(name="raid", description="Configure anti-raid protection.")
    @app_commands.describe(
        enabled="Enable or disable anti-raid",
        joins="Joins within the window that trigger a raid",
        window="Detection window in seconds",
        min_account_age="Minimum account age in days (0 disables the gate)",
        action="Action applied to raiders",
        lockdown="Lock all text channels when a raid is detected",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Ban", value="ban"),
            app_commands.Choice(name="Timeout (10 minutes)", value="timeout"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_raid(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        joins: Optional[app_commands.Range[int, 2, 1000]] = None,
        window: Optional[app_commands.Range[int, 1, 3600]] = None,
        min_account_age: Optional[app_commands.Range[int, 0, 3650]] = None,
        action: Optional[app_commands.Choice[str]] = None,
        lockdown: Optional[bool] = None,
    ) -> None:
        """Configure the join-flood threshold, account-age gate and lockdown."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "sec_antiraid", enabled)
        changes = [f"Anti-raid **{'enabled' if enabled else 'disabled'}**"]
        if joins is not None:
            await database.db.set(guild.id, "sec_antiraid_joins", int(joins))
            changes.append(f"joins={joins}")
        if window is not None:
            await database.db.set(guild.id, "sec_antiraid_window", int(window))
            changes.append(f"window={window}s")
        if min_account_age is not None:
            await database.db.set(guild.id, "sec_min_account_age", int(min_account_age))
            changes.append(f"min_age={min_account_age}d")
        if action is not None:
            await database.db.set(guild.id, "sec_antiraid_action", action.value)
            changes.append(f"action={action.value}")
        if lockdown is not None:
            await database.db.set(guild.id, "sec_antiraid_lockdown", lockdown)
            changes.append(f"lockdown={'on' if lockdown else 'off'}")
        await interaction.response.send_message(embed=success_embed(" • ".join(changes)), ephemeral=True)

    @security_group.command(name="nuke", description="Configure anti-nuke protection.")
    @app_commands.describe(
        enabled="Enable or disable anti-nuke",
        threshold="Destructive actions within the window that trigger a response",
        window="Detection window in seconds",
        action="Response applied to the offender",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Remove dangerous roles", value="remove_permissions"),
            app_commands.Choice(name="Ban", value="ban"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_nuke(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        threshold: Optional[app_commands.Range[int, 2, 100]] = None,
        window: Optional[app_commands.Range[int, 1, 3600]] = None,
        action: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Configure destructive-action detection and the response."""
        guild = interaction.guild
        assert guild is not None
        await database.db.set(guild.id, "sec_antinuke", enabled)
        changes = [f"Anti-nuke **{'enabled' if enabled else 'disabled'}**"]
        if threshold is not None:
            await database.db.set(guild.id, "sec_antinuke_threshold", int(threshold))
            changes.append(f"threshold={threshold}")
        if window is not None:
            await database.db.set(guild.id, "sec_antinuke_window", int(window))
            changes.append(f"window={window}s")
        if action is not None:
            await database.db.set(guild.id, "sec_antinuke_action", action.value)
            changes.append(f"action={action.value}")
        await interaction.response.send_message(embed=success_embed(" • ".join(changes)), ephemeral=True)

    @security_group.command(name="ignore", description="Manage channels excluded from auto-moderation.")
    @app_commands.describe(action="Add, remove or list ignored channels", channel="Channel for add/remove")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
            app_commands.Choice(name="List", value="list"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_ignore(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Add/remove/list channels that bypass every text rule."""
        guild = interaction.guild
        assert guild is not None
        current = [int(cid) for cid in await database.db.get(guild.id, "sec_ignore_channels", [])]
        if action.value == "list":
            if not current:
                await interaction.response.send_message(
                    embed=info_embed("No channels are ignored."), ephemeral=True
                )
                return
            mentions = [
                f"<#{cid}>" if guild.get_channel(cid) else f"`{cid}`" for cid in current
            ]
            await interaction.response.send_message(
                embed=base_embed(title="Ignored channels", description="\n".join(mentions)),
                ephemeral=True,
            )
            return
        if channel is None:
            await interaction.response.send_message(
                embed=error_embed("Pick a channel for add/remove."), ephemeral=True
            )
            return
        if action.value == "add":
            if channel.id in current:
                await interaction.response.send_message(
                    embed=info_embed(f"{channel.mention} is already ignored."), ephemeral=True
                )
                return
            current.append(channel.id)
            await database.db.set(guild.id, "sec_ignore_channels", current)
            await interaction.response.send_message(
                embed=success_embed(f"{channel.mention} is now ignored by auto-moderation."),
                ephemeral=True,
            )
        else:
            if channel.id not in current:
                await interaction.response.send_message(
                    embed=info_embed(f"{channel.mention} is not ignored."), ephemeral=True
                )
                return
            current = [cid for cid in current if cid != channel.id]
            await database.db.set(guild.id, "sec_ignore_channels", current)
            await interaction.response.send_message(
                embed=success_embed(f"{channel.mention} is no longer ignored."), ephemeral=True
            )

    @security_group.command(name="lockdown", description="Lock or unlock every text channel manually.")
    @app_commands.describe(enabled="True to lock the server down, False to lift it")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_lockdown(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Flip @everyone's Send Messages permission across all text channels."""
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True)
        changed = await self._set_lockdown(guild, enabled)
        await interaction.followup.send(
            embed=success_embed(
                f"Lockdown **{'enabled' if enabled else 'lifted'}** for **{changed}** text channel(s)."
            ),
            ephemeral=True,
        )

    @security_group.command(name="clear", description="Clear a user's automod action history.")
    @app_commands.describe(user="Whose automod history to clear")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_clear(self, interaction: discord.Interaction, user: discord.User) -> None:
        """Delete every recorded automod trigger for a user in this guild."""
        guild = interaction.guild
        assert guild is not None
        row = await database.db.fetchone(
            "SELECT COUNT(*) AS c FROM automod_actions WHERE guild_id = ? AND user_id = ?",
            (guild.id, user.id),
        )
        count = int(row["c"]) if row else 0
        if count == 0:
            await interaction.response.send_message(
                embed=info_embed(f"No automod actions recorded for {user.mention}."), ephemeral=True
            )
            return
        await database.db.execute(
            "DELETE FROM automod_actions WHERE guild_id = ? AND user_id = ?", (guild.id, user.id)
        )
        await interaction.response.send_message(
            embed=success_embed(f"Cleared **{count}** automod action(s) for {user.mention}."),
            ephemeral=True,
        )

    # -- whitelist ----------------------------------------------------------
    @whitelist_group.command(name="add", description="Whitelist a user or role (mention or ID).")
    @app_commands.describe(target="User/role mention or raw ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def whitelist_add(self, interaction: discord.Interaction, target: str) -> None:
        """Add a user or role to the protection whitelist."""
        guild = interaction.guild
        assert guild is not None
        kind, snowflake, label = _classify_target(guild, target)
        if kind is None or snowflake is None:
            await interaction.response.send_message(
                embed=error_embed("Could not read that user or role."), ephemeral=True
            )
            return
        key = "whitelist_users" if kind == "user" else "whitelist_roles"
        current = [int(x) for x in await database.db.get(guild.id, key, [])]
        if snowflake in current:
            await interaction.response.send_message(
                embed=info_embed(f"{label} is already whitelisted."), ephemeral=True
            )
            return
        current.append(snowflake)
        await database.db.set(guild.id, key, current)
        await interaction.response.send_message(
            embed=success_embed(f"Whitelisted {label}."), ephemeral=True
        )

    @whitelist_group.command(name="remove", description="Remove a user or role from the whitelist.")
    @app_commands.describe(target="User/role mention or raw ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def whitelist_remove(self, interaction: discord.Interaction, target: str) -> None:
        """Remove a user or role from the protection whitelist."""
        guild = interaction.guild
        assert guild is not None
        _, snowflake, label = _classify_target(guild, target)
        if snowflake is None:
            await interaction.response.send_message(
                embed=error_embed("Could not read that user or role."), ephemeral=True
            )
            return
        removed = False
        for key in ("whitelist_users", "whitelist_roles"):
            current = [int(x) for x in await database.db.get(guild.id, key, [])]
            if snowflake in current:
                current = [x for x in current if x != snowflake]
                await database.db.set(guild.id, key, current)
                removed = True
        if not removed:
            await interaction.response.send_message(
                embed=info_embed(f"{label} is not whitelisted."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=success_embed(f"Removed {label} from the whitelist."), ephemeral=True
        )

    @whitelist_group.command(name="list", description="List every whitelisted user and role.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def whitelist_list(self, interaction: discord.Interaction) -> None:
        """Paginate through the protection whitelist."""
        guild = interaction.guild
        assert guild is not None
        users = [int(x) for x in await database.db.get(guild.id, "whitelist_users", [])]
        roles = [int(x) for x in await database.db.get(guild.id, "whitelist_roles", [])]
        if not users and not roles:
            await interaction.response.send_message(
                embed=info_embed("The whitelist is empty."), ephemeral=True
            )
            return
        entries = [
            f"👤 <@{uid}>" if guild.get_member(uid) else f"👤 `{uid}`" for uid in users
        ] + [
            f"🎭 {role.mention}" if (role := guild.get_role(rid)) else f"🎭 `{rid}`" for rid in roles
        ]
        pages = [
            base_embed(
                title="Security whitelist",
                description="\n".join(entries[start : start + 20]),
            )
            for start in range(0, len(entries), 20)
        ]
        await paginate(interaction, pages)

    # -- background maintenance ---------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the periodic cleanup task once the bot is ready."""
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()

    @tasks.loop(minutes=5)
    async def cleanup_loop(self) -> None:
        """Prune stale anti-spam/anti-raid/anti-nuke tracking state."""
        now = time.monotonic()
        for store in (_SPAM, _NUKE, _BANS):
            for key in list(store):
                bucket = store[key]
                while bucket and now - bucket[0] > 7200:
                    bucket.popleft()
                if not bucket:
                    store.pop(key, None)
        for guild_id in list(_JOINS):
            bucket = _JOINS[guild_id]
            while bucket and now - bucket[0][0] > 7200:
                bucket.popleft()
            if not bucket:
                _JOINS.pop(guild_id, None)

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Load the security cog."""
    await bot.add_cog(Security(bot))
