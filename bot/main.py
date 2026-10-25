"""Entry point for the all-in-one Discord bot.

Creates the client, loads every cog, syncs slash commands, sets the
presence and installs the global error handler. All tracebacks are
written to ``logs/bot.log``.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import logging.handlers
import sys
import traceback
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db
from utils.errors import handle_app_command_error

log = logging.getLogger("bot")


def setup_logging() -> None:
    """Configure console + rotating file logging with detailed tracebacks."""
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] %(name)-22s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # discord.py is chatty at DEBUG; INFO keeps useful gateway info only
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


class AllInOneBot(commands.Bot):
    """The bot client: cog loader, command sync, presence and error handling."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=config.build_intents(),
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False),
            help_command=None,  # we ship our own /help
            chunk_guilds_at_startup=False,
        )
        self.tree.on_error = self._on_app_command_error  # type: ignore[method-assign]
        self._presence_started = False
        self.uptime: Optional[datetime.datetime] = None

    # -- error handling -----------------------------------------------------
    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await handle_app_command_error(interaction, error)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:  # type: ignore[override]
        """Log uncaught event errors (non-command) to the file handler."""
        log.error("Uncaught exception in event %s", event_method, exc_info=sys.exc_info())

    # -- startup --------------------------------------------------------------
    async def setup_hook(self) -> None:
        """Initialise the database, load cogs and sync application commands."""
        await db.init()
        await self._load_cogs()
        if config.DEV_GUILD_IDS:
            for guild_id in config.DEV_GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                try:
                    synced = await self.tree.sync(guild=guild)
                    log.info("Synced %d commands to guild %s", len(synced), guild_id)
                except discord.HTTPException:
                    log.exception("Failed to sync commands to guild %s", guild_id)
        else:
            try:
                synced = await self.tree.sync()
                log.info("Synced %d global commands (may take up to an hour to appear)", len(synced))
            except discord.HTTPException:
                log.exception("Failed to sync global commands")
        self._load_persistent_views()

    async def _load_cogs(self) -> None:
        """Import and load every module inside the ``cogs`` package."""
        cog_dir = config.BASE_DIR / "cogs"
        for path in sorted(cog_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s", extension)
            except Exception:
                log.error("Failed to load extension %s\n%s", extension, traceback.format_exc())

    def _load_persistent_views(self) -> None:
        """Register persistent views so buttons/selects survive restarts.

        Every cog module may expose ``persistent_views(bot) -> list[View]``.
        """
        import importlib

        for path in sorted((config.BASE_DIR / "cogs").glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = importlib.import_module(f"cogs.{path.stem}")
            factory = getattr(module, "persistent_views", None)
            if factory is None:
                continue
            for view in factory(self):
                self.add_view(view)
        log.info("Persistent views registered")

    # -- lifecycle --------------------------------------------------------------
    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s) | %d guilds", self.user, self.user.id, len(self.guilds))
        if self.uptime is None:
            self.uptime = discord.utils.utcnow()
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=f"{len(self.guilds)} servers • /help")
        )
        if not self._presence_started:
            self.presence_loop.start()
            self._presence_started = True

    @tasks.loop(minutes=10)
    async def presence_loop(self) -> None:
        """Rotate the bot's presence so it always looks alive."""
        activities = [
            discord.Activity(type=discord.ActivityType.watching, name=f"{len(self.guilds)} servers • /help"),
            discord.Activity(type=discord.ActivityType.listening, name="/help"),
            discord.Activity(type=discord.ActivityType.playing, name=f"{len(self.users)} members"),
        ]
        statuses = [discord.Status.online, discord.Status.dnd, discord.Status.idle]
        try:
            index = (self.presence_loop.current_iteration or 0) % len(activities)
            await self.change_presence(activity=activities[index], status=statuses[index])
        except discord.HTTPException:
            pass  # presence updates are best-effort

    @presence_loop.before_loop
    async def before_presence_loop(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        """Clean shutdown: stop tasks and close the database."""
        if self.presence_loop.is_running():
            self.presence_loop.cancel()
        await db.close()
        await super().close()


def main() -> None:
    """Validate configuration, set up logging and run the bot."""
    setup_logging()
    try:
        config.validate()
    except RuntimeError as exc:
        log.critical("%s", exc)
        raise SystemExit(1) from exc
    bot = AllInOneBot()
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Shutdown requested (KeyboardInterrupt)")
    except discord.LoginFailure:
        log.critical("Invalid DISCORD_TOKEN: the token was rejected by Discord.")
        raise SystemExit(1)
    except Exception:
        log.critical("Fatal error\n%s", traceback.format_exc())
        raise SystemExit(1)


if __name__ == "__main__":
    main()
