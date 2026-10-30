"""Telegram rich table messages (Bot API sendRichMessage / editMessageText rich_message).

Rich messages are sent through the raw API, so this works with any python-telegram-bot
version. Screens can carry buttons inside table rows (cell_blocks). If Telegram rejects
that shape, the same screen is sent with the buttons under the table instead, and that
choice is remembered. If rich messages fail entirely, the HTML text version is sent.
"""
import logging

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger("rich_ui")

# None = not tried yet, True = in-row buttons accepted, False = rejected by Telegram.
RICH_CELL = {"ok": None}


def rich_cell(text, header=False, align="left"):
    return {"text": str(text), "is_header": header, "colspan": 1, "rowspan": 1,
            "align": align, "valign": "middle"}


def button_cell(label, callback_data, style="primary", align="center"):
    """A table cell holding one button that sends callback_data when tapped."""
    # Per Bot API: a RichTextButton element wraps a RichMessageButton under "button".
    return {"text": [{"type": "button", "button": {"text": label, "callback_data": callback_data, "style": style}}],
            "is_header": False, "colspan": 1, "rowspan": 1, "align": align, "valign": "middle"}


def rich_table(headers, rows, aligns=None):
    """rows: lists of plain values or ready-made cell dicts (e.g. button_cell)."""
    aligns = aligns or ["left"] * len(headers)
    cells = [[rich_cell(h, True, a) for h, a in zip(headers, aligns)]]
    cells += [[c if isinstance(c, dict) else rich_cell(c, False, a) for c, a in zip(r, aligns)] for r in rows]
    return {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True, "is_compact": True}


def heading(text, size=2):
    return {"type": "heading", "text": str(text), "size": size}


def para(text):
    return {"type": "paragraph", "text": str(text)}


def footer(text):
    return {"type": "footer", "text": str(text)}


async def _screen(message, view, markup, edit):
    bot = message.get_bot()
    attempts = []
    if view.get("cell_blocks") is not None and RICH_CELL["ok"] is not False:
        attempts.append((view["cell_blocks"], view.get("cell_markup"), True))
    attempts.append((view["blocks"], markup, False))
    for blocks, mk, is_cell in attempts:
        payload = {"rich_message": {"blocks": blocks}}
        if mk is not None:
            payload["reply_markup"] = mk.to_dict()
        payload["chat_id"] = message.chat_id
        if edit:
            payload["message_id"] = message.message_id
            method = "editMessageText"
        else:
            method = "sendRichMessage"
        try:
            await bot._post(method, payload)
            if is_cell:
                RICH_CELL["ok"] = True
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            if is_cell:
                RICH_CELL["ok"] = False
                log.warning("rich table buttons rejected, using buttons under the table: %s", exc)
            else:
                log.warning("rich message rejected, using HTML text: %s", exc)
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("rich message failed (%s), trying next option", type(exc).__name__)
    if edit:
        try:
            await message.edit_text(view["fallback"], parse_mode=ParseMode.HTML, reply_markup=markup,
                                    disable_web_page_preview=True)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("HTML fallback edit failed: %s", exc)
    else:
        await message.reply_text(view["fallback"], parse_mode=ParseMode.HTML, reply_markup=markup)


async def send_screen(message, view, markup):
    """Send a screen. view: blocks, fallback, optional cell_blocks + cell_markup."""
    await _screen(message, view, markup, edit=False)


async def edit_screen(message, view, markup):
    """Edit a screen in place. view: blocks, fallback, optional cell_blocks + cell_markup."""
    await _screen(message, view, markup, edit=True)


async def post_rich(message, blocks, fallback, markup=None):
    """Send a simple rich message (no in-row buttons). Falls back to HTML text on failure."""
    await _screen(message, {"blocks": blocks, "fallback": fallback}, markup, edit=False)
