"""Small reusable views shared across cogs."""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord


class ConfirmView(discord.ui.View):
    """Yes/No confirmation view.

    ``on_confirm`` is awaited when the user presses ✅, ``on_deny`` on ❎.
    Only the given ``user_id`` may interact.
    """

    def __init__(
        self,
        *,
        user_id: int,
        on_confirm: Optional[Callable[[], Awaitable[None]]] = None,
        on_deny: Optional[Callable[[], Awaitable[None]]] = None,
        timeout: float = 60.0,
        confirm_label: str = "Confirm",
        deny_label: str = "Cancel",
    ) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.on_deny = on_deny
        self.message: Optional[discord.Message] = None
        self._confirm.label = confirm_label
        self._deny.label = deny_label

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.danger, custom_id="confirm:yes")
    async def _confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command invoker can confirm this.", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        if self.on_confirm:
            await self.on_confirm()

    @discord.ui.button(label="Cancel", emoji="❎", style=discord.ButtonStyle.secondary, custom_id="confirm:no")
    async def _deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command invoker can confirm this.", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        if self.on_deny:
            await self.on_deny()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[union-attr]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
