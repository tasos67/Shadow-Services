"""Async SQLite storage layer (aiosqlite).

`Database` owns a single connection and exposes:
  * low level helpers (`execute`, `fetchone`, `fetchall`)
  * a JSON key/value store for per-guild settings (`get`, `set`, `delete`)
  * `init()` which creates every table used by the bot automatically.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

log = logging.getLogger("database")

# guild_id used for bot-wide (global) settings stored in the settings table
GLOBAL_SCOPE = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);

-- Tickets -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL UNIQUE,
    owner_id      INTEGER NOT NULL,
    claimer_id    INTEGER,
    reason        TEXT,
    category      TEXT,
    status        TEXT    NOT NULL DEFAULT 'open',
    counter       INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    closed_at     INTEGER,
    last_activity INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id, status);

CREATE TABLE IF NOT EXISTS ticket_counters (
    guild_id INTEGER PRIMARY KEY,
    counter  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket_panels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    style      TEXT    NOT NULL DEFAULT 'button',
    options    TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS ticket_ratings (
    guild_id   INTEGER NOT NULL,
    ticket_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, ticket_id, user_id)
);

-- Reaction / button / select roles -----------------------------------------
CREATE TABLE IF NOT EXISTS reaction_role_panels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    type       TEXT    NOT NULL DEFAULT 'reaction', -- reaction | button | select
    entries    TEXT    NOT NULL DEFAULT '[]'
);

-- Giveaways -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS giveaways (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    prize        TEXT    NOT NULL,
    host_id      INTEGER NOT NULL,
    winners      INTEGER NOT NULL DEFAULT 1,
    ends_at      INTEGER NOT NULL,
    ended        INTEGER NOT NULL DEFAULT 0,
    paused       INTEGER NOT NULL DEFAULT 0,
    paused_left  INTEGER,
    participants TEXT    NOT NULL DEFAULT '[]',
    requirements TEXT    NOT NULL DEFAULT '{}',
    ended_by     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_giveaways_guild ON giveaways(guild_id, ended);

-- Security / auto-moderation ------------------------------------------------
CREATE TABLE IF NOT EXISTS whitelist (
    guild_id   INTEGER NOT NULL,
    target_type TEXT   NOT NULL, -- user | role
    target_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS automod_actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rule       TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    reason     TEXT,
    created_at INTEGER NOT NULL
);

-- Moderation ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    mod_id     INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    case_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    mod_id     INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    reason     TEXT,
    duration   TEXT,
    expires_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_guild ON cases(guild_id, case_id);

-- Leveling ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS xp (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    xp        INTEGER NOT NULL DEFAULT 0,
    level     INTEGER NOT NULL DEFAULT 0,
    messages  INTEGER NOT NULL DEFAULT 0,
    voice_xp  INTEGER NOT NULL DEFAULT 0,
    last_msg  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Economy -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS economy (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    cash         INTEGER NOT NULL DEFAULT 0,
    bank         INTEGER NOT NULL DEFAULT 0,
    daily_streak INTEGER NOT NULL DEFAULT 0,
    last_daily   INTEGER NOT NULL DEFAULT 0,
    last_work    INTEGER NOT NULL DEFAULT 0,
    last_beg     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS shop_items (
    guild_id   INTEGER NOT NULL,
    item       TEXT    NOT NULL,
    price      INTEGER NOT NULL,
    description TEXT   NOT NULL DEFAULT '',
    role_id    INTEGER,
    quantity   INTEGER NOT NULL DEFAULT -1, -- -1 = unlimited stock
    PRIMARY KEY (guild_id, item)
);

CREATE TABLE IF NOT EXISTS inventory (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item     TEXT    NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id, item)
);

-- Custom commands / tags / auto-responders ----------------------------------
CREATE TABLE IF NOT EXISTS custom_commands (
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    response   TEXT    NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS autoresponders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    trigger    TEXT    NOT NULL,
    response   TEXT    NOT NULL,
    match_mode TEXT    NOT NULL DEFAULT 'contains', -- contains | equals | starts
    created_by INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    guild_id INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    content  TEXT    NOT NULL,
    owner_id INTEGER NOT NULL,
    uses     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, name)
);

-- Suggestions ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending', -- pending|approved|denied|implemented
    staff_note TEXT,
    created_at  INTEGER NOT NULL,
    handled_by  INTEGER
);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    vote          INTEGER NOT NULL, -- 1 up, -1 down
    PRIMARY KEY (suggestion_id, user_id)
);

-- Starboard ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS starboard_messages (
    guild_id        INTEGER NOT NULL,
    original_id     INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    star_message_id INTEGER,
    stars           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, original_id)
);

-- Reminders / polls / AFK ----------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    remind_at  INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    message_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    question   TEXT    NOT NULL,
    options    TEXT    NOT NULL DEFAULT '[]',
    ends_at    INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    choice     INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS afk (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    reason   TEXT    NOT NULL,
    set_at   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Birthdays ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS birthdays (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    day      INTEGER NOT NULL,
    month    INTEGER NOT NULL,
    year     INTEGER,
    PRIMARY KEY (guild_id, user_id)
);

-- Temporary voice channels ---------------------------------------------------
CREATE TABLE IF NOT EXISTS temp_voice (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- Counting game ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS counting (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,
    current    INTEGER NOT NULL DEFAULT 0,
    last_user  INTEGER
);

-- Sticky messages -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sticky_messages (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (guild_id, channel_id)
);
"""


class Database:
    """Single-connection async wrapper around the bot's SQLite database."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None

    # -- lifecycle ----------------------------------------------------------
    async def init(self) -> None:
        """Open the database, apply pragmas and create every table."""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        log.info("Database ready at %s", self._path)

    async def close(self) -> None:
        """Close the underlying connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database has not been initialised; call await db.init() first.")
        return self._conn

    # -- low level helpers ---------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute a statement and commit. Returns lastrowid."""
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.lastrowid or 0

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        """Execute a statement for many parameter sets and commit."""
        await self.conn.executemany(sql, [tuple(r) for r in rows])
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        """Return the first row of a query or None."""
        cur = await self.conn.execute(sql, tuple(params))
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Return every row of a query."""
        cur = await self.conn.execute(sql, tuple(params))
        return await cur.fetchall()

    # -- settings (JSON key/value per guild) ---------------------------------
    async def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        """Fetch a per-guild setting, returning `default` when unset."""
        row = await self.fetchone(
            "SELECT value FROM settings WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    async def set(self, guild_id: int, key: str, value: Any) -> None:
        """Store a per-guild setting (JSON serialisable)."""
        payload = json.dumps(value)
        await self.execute(
            "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, payload),
        )

    async def delete(self, guild_id: int, key: str) -> None:
        """Delete a per-guild setting."""
        await self.execute("DELETE FROM settings WHERE guild_id = ? AND key = ?", (guild_id, key))

    async def get_all(self, guild_id: int) -> dict[str, Any]:
        """Return every setting of a guild as a dict."""
        rows = await self.fetchall("SELECT key, value FROM settings WHERE guild_id = ?", (guild_id,))
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    async def clear_guild(self, guild_id: int) -> None:
        """Delete every setting of a guild (used by /settings reset)."""
        await self.execute("DELETE FROM settings WHERE guild_id = ?", (guild_id,))

    # -- convenience shortcuts ------------------------------------------------
    async def get_bool(self, guild_id: int, key: str, default: bool = False) -> bool:
        """Boolean setting shortcut."""
        return bool(await self.get(guild_id, key, default))

    async def get_int(self, guild_id: int, key: str, default: int = 0) -> int:
        """Integer setting shortcut."""
        value = await self.get(guild_id, key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def next_case_id(self, guild_id: int) -> int:
        """Return the next sequential moderation case id for a guild."""
        row = await self.fetchone(
            "SELECT MAX(case_id) AS max_case FROM cases WHERE guild_id = ?", (guild_id,)
        )
        return int(row["max_case"] or 0) + 1 if row else 1


# module level singleton, initialised by the bot during startup
db = Database(Path(__file__).resolve().parent / "data" / "bot.db")
