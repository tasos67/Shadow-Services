"""Shared permission checks used by admin commands across every cog."""
from __future__ import annotations

from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

import database

__all__ = ["is_bot_owner", "is_moderator", "is_admin", "ensure_guild"]


def is_bot_owner() -> Callable:
    """Allow only the configured bot owner (OWNER_ID) or Discord application owner."""

    async def predicate(interaction: discord.Interaction) -> bool:
        import config

        app_owner_id = None
        try:
            app = await interaction.client.application_info()
            app_owner_id = app.owner.id
        except discord.HTTPException:
            pass
        if interaction.user.id == config.OWNER_ID or interaction.user.id == app_owner_id:
            return True
        raise app_commands.CheckFailure("This command is restricted to the bot owner.")

    return app_commands.check(predicate)


def ensure_guild() -> Callable:
    """Reject commands used in DMs."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        return True

    return app_commands.check(predicate)


def _member_has_mod_perms(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return any(
        (
            perms.administrator,
            perms.manage_guild,
            perms.manage_messages,
            perms.kick_members,
            perms.ban_members,
            perms.moderate_members,
        )
    )


def is_moderator() -> Callable:
    """Allow users with standard moderation permissions or the configured staff role."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        assert isinstance(interaction.user, discord.Member)
        member: discord.Member = interaction.user
        if await interaction.client.is_owner(member):
            return True
        if _member_has_mod_perms(member):
            return True
        mod_role_ids: list[int] = await database.db.get(interaction.guild.id, "mod_role_ids", [])
        if any(role.id in mod_role_ids for role in member.roles):
            return True
        raise app_commands.CheckFailure(
            "You need the **Moderate Members**, **Manage Messages**, **Manage Server** "
            "permissions or the configured staff role to use this command."
        )

    return app_commands.check(predicate)


def is_admin() -> Callable:
    """Allow users with the Administrator permission."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        assert isinstance(interaction.user, discord.Member)
        if interaction.user.guild_permissions.administrator:
            return True
        if await interaction.client.is_owner(interaction.user):
            return True
        raise app_commands.CheckFailure("You need the **Administrator** permission for this command.")

    return app_commands.check(predicate)


async def is_whitelisted(member: discord.Member) -> bool:
    """Check whether a member (or one of their roles) is whitelisted for security."""
    users: list[int] = await database.db.get(member.guild.id, "whitelist_users", [])
    roles: list[int] = await database.db.get(member.guild.id, "whitelist_roles", [])
    if member.id in users:
        return True
    return any(role.id in roles for role in member.roles)
