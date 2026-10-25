"""Global application-command error handling with clean user-facing embeds.

Every traceback is written to the log file; users only ever see a tidy embed.
"""
from __future__ import annotations

import logging
import traceback
import uuid

import discord
from discord import app_commands

from utils.embeds import error_embed, warn_embed

log = logging.getLogger("errors")


def _format_missing(perms: list[str]) -> str:
    return ", ".join(f"`{p.replace('_', ' ').title()}`" for p in perms)


async def respond_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Reply ephemerally with an embed, whichever state the interaction is in."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver error embed for %s", interaction.command)


async def handle_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Tree-level error handler: maps exceptions to friendly embeds."""
    error = getattr(error, "original", error)
    command_name = interaction.command.qualified_path if interaction.command else "unknown"

    if isinstance(error, app_commands.MissingPermissions):
        await respond_error(
            interaction,
            error_embed(
                f"You are missing the following permissions: {_format_missing(error.missing_permissions)}",
                title="Missing permissions",
            ),
        )
        return

    if isinstance(error, app_commands.BotMissingPermissions):
        await respond_error(
            interaction,
            error_embed(
                f"I am missing the following permissions: "
                f"{_format_missing(error.missing_permissions)}. Please fix my role's permissions.",
                title="Missing bot permissions",
            ),
        )
        return

    if isinstance(error, app_commands.MissingRole):
        await respond_error(
            interaction,
            error_embed(
                f"You need the {error.missing_role} role to use this command.", title="Missing role"
            ),
        )
        return

    if isinstance(error, app_commands.MissingAnyRole):
        roles = ", ".join(error.missing_roles)
        await respond_error(
            interaction, error_embed(f"You need one of these roles: {roles}", title="Missing role")
        )
        return

    if isinstance(error, app_commands.NoPrivateMessage):
        await respond_error(
            interaction, error_embed("This command can only be used inside a server.", title="Server only")
        )
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        await respond_error(
            interaction,
            warn_embed(
                f"Slow down — try again in **{error.retry_after:.1f} seconds**.", title="Cooldown"
            ),
        )
        return

    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "You cannot use this command."
        await respond_error(interaction, error_embed(message, title="Not allowed"))
        return

    if isinstance(error, app_commands.TransformError):
        await respond_error(
            interaction,
            error_embed(
                "One of the arguments has an invalid format. Please check the command and try again.",
                title="Invalid argument",
            ),
        )
        return

    # Fallback: unexpected error. Log the full traceback and tell the user.
    error_id = uuid.uuid4().hex[:8]
    log.error(
        "Unhandled error in /%s (id %s)\n%s",
        command_name,
        error_id,
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    )
    await respond_error(
        interaction,
        error_embed(
            f"Something went wrong while running this command.\n"
            f"The error has been logged (id `{error_id}`).",
            title="Unexpected error",
        ),
    )
