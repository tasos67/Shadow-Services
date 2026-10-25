"""Reusable embed paginator with navigation buttons."""
from __future__ import annotations

from typing import Optional, Sequence

import discord

import config
from utils.embeds import base_embed, error_embed


class Paginator(discord.ui.View):
    """A button-driven paginator over a list of embeds.

    Attributes:
        pages: the embeds to paginate through.
    """

    def __init__(self, pages: Sequence[discord.Embed], *, owner_id: int, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.pages: list[discord.Embed] = list(pages)
        self.owner_id = owner_id
        self.index = 0
        self.message: Optional[discord.Message] = None
        if len(self.pages) <= 1:
            for child in self.children:
                if isinstance(child, (discord.ui.Button, discord.ui.Select)) and child.custom_id != "paginator:close":
                    child.disabled = True

    # -- UI -----------------------------------------------------------------
    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="paginator:first")
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = 0
        await self._update(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, custom_id="paginator:prev")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        await self._update(interaction)

    @discord.ui.button(label="Page", style=discord.ButtonStyle.secondary, disabled=True, custom_id="paginator:page")
    async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, custom_id="paginator:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._update(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="paginator:last")
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = len(self.pages) - 1
        await self._update(interaction)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="paginator:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass

    # -- internals -----------------------------------------------------------
    async def _update(self, interaction: discord.Interaction) -> None:
        self.page_label.label = f"{self.index + 1}/{len(self.pages)}"
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id != "paginator:close":
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def paginate(interaction: discord.Interaction, pages: Sequence[discord.Embed]) -> None:
    """Send ``pages`` as a paginated response to an interaction."""
    if not pages:
        pages = [base_embed(title="Nothing to show", description="There is no data to display.")]
    view = Paginator(pages, owner_id=interaction.user.id)
    first = pages[0]
    if len(pages) > 1:
        view.page_label.label = f"1/{len(pages)}"
    try:
        if interaction.response.is_done():
            message = await interaction.followup.send(embed=first, view=view, wait=True)
        else:
            message = await interaction.response.send_message(embed=first, view=view, wait=True)
        view.message = message
    except discord.HTTPException as exc:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed(f"Could not send the list: {exc}"), ephemeral=True)


def paginate_embeds(
    items: Sequence,
    *,
    title: str,
    description_builder,
    per_page: int = 5,
    footer_extra: str = "",
) -> list[discord.Embed]:
    """Build pages of field-embeds from a list of items.

    ``description_builder(item)`` returns ``(name, value)`` for one item.
    """
    pages: list[discord.Embed] = []
    for start in range(0, len(items), per_page):
        chunk = items[start : start + per_page]
        embed = base_embed(title=title)
        for item in chunk:
            name, value = description_builder(item)
            embed.add_field(name=name, value=value, inline=False)
        total_pages = (len(items) + per_page - 1) // per_page
        embed.set_footer(
            text=f"Page {start // per_page + 1}/{total_pages}" + (f" • {footer_extra}" if footer_extra else "")
        )
        pages.append(embed)
    return pages
