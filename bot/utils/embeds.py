"""Consistent, good-looking embeds for the whole bot.

Every embed produced by the bot goes through this module so colours,
footers and timestamps always match the theme.
"""
from __future__ import annotations

from typing import Optional, Sequence

import discord

import config


def base_embed(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    color: Optional[discord.Color] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    image: Optional[str] = None,
    author: Optional[discord.abc.User] = None,
    fields: Optional[Sequence[tuple[str, str, bool]]] = None,
    timestamp: bool = True,
) -> discord.Embed:
    """Build a themed embed with the standard footer and timestamp."""
    embed = discord.Embed(color=color or config.THEME_COLOR)
    if title:
        embed.title = title
    if description:
        embed.description = description
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    if author:
        embed.set_author(
            name=str(author),
            icon_url=author.display_avatar.url if author.display_avatar else None,
        )
    for name, value, inline in fields or ():
        embed.add_field(name=name, value=value, inline=inline)
    if timestamp:
        embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=footer or config.FOOTER_TEXT)
    return embed


def success_embed(description: str, title: str = "Success", **kwargs) -> discord.Embed:
    """Green success embed."""
    return base_embed(title=f"✅  {title}", description=description, color=config.SUCCESS_COLOR, **kwargs)


def error_embed(description: str, title: str = "Error", **kwargs) -> discord.Embed:
    """Red error embed."""
    return base_embed(title=f"❌  {title}", description=description, color=config.ERROR_COLOR, **kwargs)


def warn_embed(description: str, title: str = "Warning", **kwargs) -> discord.Embed:
    """Yellow warning embed."""
    return base_embed(title=f"⚠️  {title}", description=description, color=config.WARNING_COLOR, **kwargs)


def info_embed(description: str, title: Optional[str] = "Information", **kwargs) -> discord.Embed:
    """Blue information embed."""
    return base_embed(title=title, description=description, color=config.INFO_COLOR, **kwargs)


async def send_embed(
    destination,
    embed: discord.Embed,
    *,
    view: Optional[discord.ui.View] = None,
    ephemeral: bool = False,
    delete_after: Optional[float] = None,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
) -> None:
    """Send an embed as an interaction response, falling back to a followup/edit."""
    kwargs: dict = {"embed": embed, "ephemeral": ephemeral, "delete_after": delete_after}
    if view is not None:
        kwargs["view"] = view
    if allowed_mentions is not None:
        kwargs["allowed_mentions"] = allowed_mentions
    try:
        if isinstance(destination, discord.Interaction):
            if destination.response.is_done():
                await destination.followup.send(**kwargs)
            else:
                await destination.response.send_message(**kwargs)
        else:
            await destination.send(**kwargs)
    except discord.HTTPException:
        # last resort: strip heavy parts so the message still goes through
        embed.set_image(url=None)
        embed.set_thumbnail(url=None)
        if isinstance(destination, discord.Interaction):
            if destination.response.is_done():
                await destination.followup.send(**kwargs)
            else:
                await destination.response.send_message(**kwargs)
        else:
            await destination.send(**kwargs)
