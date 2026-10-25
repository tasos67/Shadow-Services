"""Welcome image-card rendering with Pillow.

The heavy PIL work runs in a worker thread via ``asyncio.to_thread`` so the
event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("C:/Windows/Fonts/Microsoft"),
]
_FONT_CANDIDATES = {
    True: ["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "calibrib.ttf", "tahomabd.ttf"],
    False: ["segoeui.ttf", "arial.ttf", "calibri.ttf", "tahoma.ttf"],
}


def _find_font(bold: bool) -> Optional[str]:
    """Locate a usable TTF font on the system, if any."""
    for directory in _FONT_DIRS:
        for name in _FONT_CANDIDATES[bold]:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
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


def _truncate_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    """Ellipsize text so it fits within max_width pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _render_card_sync(
    avatar_bytes: bytes,
    username: str,
    server_name: str,
    member_count: int,
    *,
    accent: tuple[int, int, int] = (88, 101, 242),
    title: str = "Welcome!",
    subtitle: str = "We're glad to have you here.",
) -> bytes:
    """Synchronous card renderer (CPU bound — run in a thread)."""
    width, height = 1000, 360
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # Background with rounded corners
    draw.rounded_rectangle((0, 0, width, height), radius=28, fill=(49, 51, 56, 255))
    # Accent strip along the top
    draw.rounded_rectangle((0, 0, width, 70), radius=28, fill=accent + (255,))
    draw.rectangle((0, 40, width, 70), fill=accent + (255,))

    # Avatar (circular, with accent ring)
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes))
        avatar = _circle_crop(avatar, 200)
        ring = Image.new("RGBA", (224, 224), (0, 0, 0, 0))
        ring_draw = ImageDraw.Draw(ring)
        ring_draw.ellipse((0, 0, 223, 223), fill=accent + (255,))
        card.alpha_composite(ring, (60, 90))
        card.alpha_composite(avatar, (72, 102))
    except Exception:
        # Placeholder circle when the avatar cannot be fetched
        draw.ellipse((60, 90, 284, 314), fill=accent + (255,))

    # Text
    title_font = _load_font(46, bold=True)
    subtitle_font = _load_font(26)
    count_font = _load_font(24, bold=True)

    title_text = _truncate_text(draw, f"Welcome, {username}!", title_font, width - 380)
    server_text = _truncate_text(draw, server_name, subtitle_font, width - 380)
    draw.text((320, 130), title_text, font=title_font, fill=(255, 255, 255, 255))
    draw.text((320, 195), server_text, font=subtitle_font, fill=(181, 186, 193, 255))
    draw.text(
        (320, 240),
        _truncate_text(draw, subtitle, subtitle_font, width - 380),
        font=subtitle_font,
        fill=(148, 155, 164, 255),
    )
    draw.text(
        (320, 285),
        _truncate_text(draw, f"Member #{member_count}", count_font, width - 380),
        font=count_font,
        fill=accent + (255,),
    )

    output = io.BytesIO()
    card.save(output, format="PNG")
    return output.getvalue()


async def render_welcome_card(
    avatar_bytes: Optional[bytes],
    username: str,
    server_name: str,
    member_count: int,
    *,
    accent: tuple[int, int, int] = (88, 101, 242),
    title: str = "Welcome!",
    subtitle: str = "We're glad to have you here.",
) -> bytes:
    """Render a welcome card off-loop and return PNG bytes."""
    if avatar_bytes is None:
        avatar_bytes = b""
    return await asyncio.to_thread(
        _render_card_sync,
        avatar_bytes,
        username,
        server_name,
        member_count,
        accent=accent,
        title=title,
        subtitle=subtitle,
    )
