"""Ticket transcript generation (styled HTML + plain text)."""
from __future__ import annotations

import html
import io
import time
from datetime import datetime, timezone
from typing import Optional, Sequence

import discord

import config


def _format_message_plain(message: discord.Message) -> str:
    """One plain-text line block for a message."""
    ts = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    author = f"{message.author} ({message.author.id})"
    parts = [f"[{ts}] {author}: {message.content or ''}"]
    for attachment in message.attachments:
        parts.append(f"    [attachment] {attachment.url}")
    for embed in message.embeds:
        summary = embed.title or embed.description or "[embed]"
        parts.append(f"    [embed] {str(summary)[:200]}")
    for sticker in message.stickers:
        parts.append(f"    [sticker] {sticker.name}")
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        parts.append(f"    [reply to {getattr(ref, 'author', '?')}]")
    return "\n".join(parts)


def build_text_transcript(channel: discord.abc.Messageable, messages: Sequence[discord.Message]) -> str:
    """Build a plain text transcript of a channel's history."""
    header = (
        f"Transcript of #{getattr(channel, 'name', 'unknown-channel')}\n"
        f"Guild: {channel.guild.name if getattr(channel, 'guild', None) else 'DM'}\n"
        f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"Messages: {len(messages)}\n"
        + "=" * 60 + "\n"
    )
    body = "\n".join(_format_message_plain(m) for m in messages)
    return header + body + "\n"


_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #1e1f22; color: #dbdee1; font-family: 'gg sans','Noto Sans','Helvetica Neue',Helvetica,Arial,sans-serif; padding: 24px; }
.header { background: #2b2d31; border-radius: 8px; padding: 18px 22px; margin-bottom: 18px; border-left: 5px solid #5865F2; }
.header h1 { font-size: 20px; color: #f2f3f5; margin-bottom: 4px; }
.header p { font-size: 13px; color: #b5bac1; }
.message { display: flex; gap: 12px; padding: 8px 10px; border-radius: 6px; }
.message:hover { background: #2e3035; }
.message.group-start { margin-top: 10px; }
.avatar { width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0; }
.meta { min-width: 0; flex: 1; }
.author { font-weight: 600; font-size: 15px; }
.bot-tag { background: #5865F2; color: #fff; font-size: 10px; border-radius: 3px; padding: 1px 4px; margin-left: 5px; vertical-align: middle; }
.timestamp { font-size: 11px; color: #949ba4; margin-left: 6px; }
.content { font-size: 14px; line-height: 1.4; word-wrap: break-word; white-space: pre-wrap; }
.attachment, .embed-link { font-size: 13px; color: #00a8fc; margin-top: 4px; display: block; }
.embed { border-left: 4px solid #5865F2; background: #2b2d31; border-radius: 4px; padding: 10px; margin-top: 6px; max-width: 520px; }
.embed .title { font-weight: 600; color: #f2f3f5; font-size: 14px; margin-bottom: 4px; }
.embed .desc { font-size: 13px; color: #dbdee1; white-space: pre-wrap; }
.embed .field-name { font-size: 13px; font-weight: 600; color: #f2f3f5; }
.embed .field-value { font-size: 13px; color: #dbdee1; margin-bottom: 4px; white-space: pre-wrap; }
.reactions { margin-top: 4px; }
.reaction { background: #2b2d31; border: 1px solid #3f4147; border-radius: 8px; font-size: 12px; padding: 1px 6px; margin-right: 4px; }
.reply { font-size: 12px; color: #b5bac1; margin-bottom: 2px; }
.footer { margin-top: 20px; text-align: center; font-size: 12px; color: #949ba4; }
"""


def build_html_transcript(
    channel: discord.abc.Messageable,
    messages: Sequence[discord.Message],
    *,
    closed_by: Optional[discord.abc.User] = None,
    reason: Optional[str] = None,
) -> str:
    """Build a self-contained, styled HTML transcript of a channel."""
    guild = getattr(channel, "guild", None)
    channel_name = getattr(channel, "name", "unknown-channel")
    export_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    rows: list[str] = []
    last_author: Optional[int] = None
    last_time = 0
    for message in messages:
        author = message.author
        color = author.color.to_rgb() if isinstance(author, discord.Member) and author.color != discord.Colour.default() else (219, 222, 225)
        color_hex = "#%02x%02x%02x" % color
        group_start = author.id != last_author or (message.created_at.timestamp() - last_time) > 300
        avatar = author.display_avatar.url if author.display_avatar else ""
        ts = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        parts: list[str] = [f'<div class="message{" group-start" if group_start else ""}">']
        parts.append(f'<img class="avatar" src="{html.escape(avatar, quote=True)}" alt="avatar">')
        parts.append('<div class="meta">')
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            ref = message.reference.resolved
            parts.append(
                f'<div class="reply">↩ replying to <b>{html.escape(str(ref.author))}</b>: '
                f'{html.escape((ref.content or "[no text]")[:120])}</div>'
            )
        bot_tag = '<span class="bot-tag">APP</span>' if author.bot else ""
        parts.append(
            f'<span class="author" style="color: {color_hex}">{html.escape(str(author))}</span>'
            f"{bot_tag}<span class='timestamp'>{ts}</span>"
        )
        if message.content:
            parts.append(f'<div class="content">{html.escape(message.content)}</div>')
        for attachment in message.attachments:
            parts.append(f'<a class="attachment" href="{html.escape(attachment.url, quote=True)}">📎 {html.escape(attachment.filename)}</a>')
        for sticker in message.stickers:
            parts.append(f'<span class="attachment">🏷️ sticker: {html.escape(sticker.name)}</span>')
        for embed in message.embeds:
            parts.append('<div class="embed">')
            if embed.title:
                parts.append(f'<div class="title">{html.escape(embed.title)}</div>')
            if embed.description:
                parts.append(f'<div class="desc">{html.escape(embed.description[:1500])}</div>')
            for field in embed.fields[:16]:
                parts.append(
                    f'<div class="field-name">{html.escape(field.name)}</div>'
                    f'<div class="field-value">{html.escape(str(field.value)[:500])}</div>'
                )
            if embed.url:
                parts.append(f'<a class="embed-link" href="{html.escape(embed.url, quote=True)}">link</a>')
            parts.append("</div>")
        if message.reactions:
            parts.append('<div class="reactions">')
            for reaction in message.reactions[:20]:
                emoji = reaction.emoji
                symbol = str(emoji) if isinstance(emoji, (str, discord.Emoji)) else "[sticker]"
                parts.append(f'<span class="reaction">{html.escape(symbol)} {reaction.count}</span>')
            parts.append("</div>")
        parts.append("</div></div>")
        rows.append("".join(parts))
        last_author = author.id
        last_time = message.created_at.timestamp()

    reason_html = f"<p><b>Closed by:</b> {html.escape(str(closed_by))}" if closed_by else ""
    if reason:
        reason_html += f" &nbsp; <b>Reason:</b> {html.escape(reason)}"
    if reason_html:
        reason_html += "</p>"

    title = f"#{html.escape(channel_name)}"
    guild_name = html.escape(guild.name) if guild else "Direct Message"
    body = "\n".join(rows) or "<p>No messages were recorded in this channel.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transcript • {title}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="header">
  <h1>Transcript {title}</h1>
  <p>Server: {guild_name} • Exported: {export_dt} • Messages: {len(messages)}</p>
  {reason_html}
</div>
{body}
<div class="footer">Generated by {config.BOT_NAME}</div>
</body>
</html>"""


async def fetch_history(
    channel: discord.abc.Messageable, *, limit: int = 500
) -> list[discord.Message]:
    """Fetch channel history oldest-first, tolerating missing access."""
    messages: list[discord.Message] = []
    try:
        async for message in channel.history(limit=limit, oldest_first=True):
            messages.append(message)
    except (discord.Forbidden, discord.HTTPException):
        pass
    return messages


async def send_transcript(
    channel: discord.abc.Messageable,
    messages: Sequence[discord.Message],
    *,
    log_channel: Optional[discord.TextChannel] = None,
    user: Optional[discord.abc.User] = None,
    closed_by: Optional[discord.abc.User] = None,
    reason: Optional[str] = None,
) -> None:
    """Build an HTML transcript, send it to the log channel and DM it to the user."""
    html_content = build_html_transcript(channel, messages, closed_by=closed_by, reason=reason)
    text_content = build_text_transcript(channel, messages)
    channel_name = getattr(channel, "name", "channel")
    stamp = int(time.time())
    html_file = discord.File(
        io.BytesIO(html_content.encode("utf-8")),
        filename=f"transcript-{channel_name}-{stamp}.html",
    )
    if log_channel is not None:
        try:
            await log_channel.send(
                content=f"Transcript for **#{channel_name}**" + (f" — closed by {closed_by.mention}" if closed_by else ""),
                file=html_file,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    if user is not None:
        try:
            text_file = discord.File(
                io.BytesIO(text_content.encode("utf-8")),
                filename=f"transcript-{channel_name}-{stamp}.txt",
            )
            await user.send(
                content=f"Here is the transcript of your ticket in "
                f"**{getattr(getattr(channel, 'guild', None), 'name', 'a server')}**.",
                file=text_file,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
