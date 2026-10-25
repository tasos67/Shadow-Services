"""Rank card rendering with Pillow for the leveling system.

The heavy PIL work runs in a worker thread via ``asyncio.to_thread`` so the
event loop is never blocked. The font discovery mirrors ``utils.imagecard``.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("C:/Windows/Fonts/Microsoft"),
]
_FONT_CANDIDATES = {
    True: ["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "calibrib.ttf", "tahomabd.ttf"],
    False: ["segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf"],
}

CARD_WIDTH = 900
CARD_HEIGHT = 280
AVATAR_SIZE = 170
AVATAR_X = 45
AVATAR_Y = 55
TEXT_X = 250
BAR_X = 250
BAR_Y = 165
BAR_HEIGHT = 36


def _find_font(bold: bool) -> Optional[str]:
    """Locate a usable TTF font on the system, if any."""
    for directory in _FONT_DIRS:
        for name in _FONT_CANDIDATES[bold]:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Load a TrueType font, falling back to Pillow's bitmap font."""
    path = _find_font(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def _circle_crop(avatar: Image.Image, size: int) -> Image.Image:
    """Return a circular-cropped RGBA avatar of the requested size."""
    avatar = avatar.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    avatar.putalpha(mask)
    return avatar


def _truncate_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int
) -> str:
    """Ellipsize text so it fits within max_width pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _render_rank_card_sync(
    avatar_bytes: Optional[bytes],
    username: str,
    level: int,
    rank: int,
    xp: int,
    next_level_xp: int,
    background: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> bytes:
    """Synchronous rank card renderer (CPU bound — run in a thread)."""
    width, height = CARD_WIDTH, CARD_HEIGHT
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # Rounded background + accent strip along the top
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=26, fill=background + (255,))
    draw.rounded_rectangle((0, 0, width, 8), radius=4, fill=accent + (255,))

    # Avatar (circular, with an accent ring behind it)
    ring_box = (AVATAR_X - 8, AVATAR_Y - 8, AVATAR_X + AVATAR_SIZE + 8, AVATAR_Y + AVATAR_SIZE + 8)
    draw.ellipse(ring_box, fill=accent + (255,))
    painted = False
    if avatar_bytes:
        try:
            avatar = _circle_crop(Image.open(io.BytesIO(avatar_bytes)), AVATAR_SIZE)
            card.alpha_composite(avatar, (AVATAR_X, AVATAR_Y))
            painted = True
        except Exception:
            painted = False
    if not painted:
        initial_font = _load_font(80, bold=True)
        initial = _truncate_text(draw, (username or "?")[:1], initial_font, AVATAR_SIZE - 20)
        draw.text(
            (AVATAR_X + AVATAR_SIZE / 2, AVATAR_Y + AVATAR_SIZE / 2),
            initial,
            font=initial_font,
            fill=(255, 255, 255, 255),
            anchor="mm",
        )

    username_font = _load_font(44, bold=True)
    meta_font = _load_font(26, bold=True)
    small_font = _load_font(20)

    name = _truncate_text(draw, username or "Unknown", username_font, width - TEXT_X - 40)
    draw.text((TEXT_X, 42), name, font=username_font, fill=(255, 255, 255, 255))
    draw.text((TEXT_X, 100), f"RANK #{rank}", font=meta_font, fill=accent + (255,))
    draw.text((TEXT_X + 220, 100), f"LEVEL {level}", font=meta_font, fill=(181, 186, 193, 255))

    # XP progress bar
    bar_width = (width - 40) - BAR_X
    draw.rounded_rectangle(
        (BAR_X, BAR_Y, BAR_X + bar_width, BAR_Y + BAR_HEIGHT),
        radius=BAR_HEIGHT // 2,
        fill=(45, 47, 54, 255),
    )
    safe_total = 1 if next_level_xp <= 0 else next_level_xp
    progress = max(0.0, min(1.0, xp / safe_total))
    fill_width = int(bar_width * progress)
    if fill_width >= 4:
        draw.rounded_rectangle(
            (BAR_X, BAR_Y, BAR_X + fill_width, BAR_Y + BAR_HEIGHT),
            radius=BAR_HEIGHT // 2,
            fill=accent + (255,),
        )
    label = f"{xp} / {next_level_xp} XP"
    label_width = draw.textlength(label, font=small_font)
    draw.text(
        (BAR_X + (bar_width - label_width) / 2, BAR_Y + (BAR_HEIGHT - 20) / 2),
        label,
        font=small_font,
        fill=(255, 255, 255, 255),
    )

    output = io.BytesIO()
    card.save(output, format="PNG")
    return output.getvalue()


async def render_rank_card(
    avatar_bytes: Optional[bytes],
    username: str,
    level: int,
    rank: int,
    xp: int,
    next_level_xp: int,
    background: tuple[int, int, int] = (30, 31, 34),
    accent: tuple[int, int, int] = (88, 101, 242),
) -> bytes:
    """Render a 900x280 rank card off-loop and return PNG bytes.

    ``xp`` / ``next_level_xp`` describe the progress inside the current level.
    """
    return await asyncio.to_thread(
        _render_rank_card_sync,
        avatar_bytes,
        username,
        level,
        rank,
        xp,
        next_level_xp,
        background,
        accent,
    )
