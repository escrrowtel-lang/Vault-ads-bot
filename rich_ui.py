"""Telegram rich table messages (Bot API sendRichMessage / editMessageText rich_message).

Rich tables are sent through the raw API so this works with any python-telegram-bot
version. Every helper falls back to plain HTML text when Telegram rejects the rich message.
"""
import logging

from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger("rich_ui")


def rich_cell(text, header=False, align="left"):
    return {"text": str(text), "is_header": header, "colspan": 1, "rowspan": 1,
            "align": align, "valign": "middle"}


def rich_table(headers, rows, aligns=None):
    aligns = aligns or ["left"] * len(headers)
    cells = [[rich_cell(h, True, a) for h, a in zip(headers, aligns)]]
    cells += [[rich_cell(c, False, a) for c, a in zip(r, aligns)] for r in rows]
    return {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True, "is_compact": True}


def heading(text, size=2):
    return {"type": "heading", "text": str(text), "size": size}


def para(text):
    return {"type": "paragraph", "text": str(text)}


def footer(text):
    return {"type": "footer", "text": str(text)}


async def post_rich(message, blocks, fallback, markup=None):
    """Send a rich message to message's chat. Falls back to HTML text on failure."""
    payload = {"chat_id": message.chat_id, "rich_message": {"blocks": blocks}}
    if markup is not None:
        payload["reply_markup"] = markup.to_dict()
    try:
        await message.get_bot()._post("sendRichMessage", payload)
        return
    except Exception as exc:
        log.warning("rich send failed, using HTML fallback: %s", type(exc).__name__)
    await message.reply_text(fallback, parse_mode=ParseMode.HTML, reply_markup=markup)


async def edit_rich(message, blocks, fallback, markup=None):
    """Edit an existing message into a rich table. Falls back to HTML text on failure."""
    payload = {"chat_id": message.chat_id, "message_id": message.message_id,
               "rich_message": {"blocks": blocks}}
    if markup is not None:
        payload["reply_markup"] = markup.to_dict()
    try:
        await message.get_bot()._post("editMessageText", payload)
        return
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        log.warning("rich edit failed, using HTML fallback: %s", type(exc).__name__)
    try:
        await message.edit_text(fallback, parse_mode=ParseMode.HTML, reply_markup=markup,
                                disable_web_page_preview=True)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
