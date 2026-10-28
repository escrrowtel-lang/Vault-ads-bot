import asyncio
import json
import html
import logging
import os
import time
import random
import re
import contextvars
import httpx
from functools import wraps
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from types import SimpleNamespace
from telegram import (Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, MessageEntity,
                      ReplyKeyboardRemove, Update)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ExtBot,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, errors, types, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from database import Database, MongoSessionStore
from pacing import AccountPacer
import subscriptions as subs_core          # NEW: subscriptions + Razorpay
import subscription_ui as subs_ui          # NEW: purchase flow + admin section

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "promo_bot")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
MIN_AD_INTERVAL_MINUTES = 10
DEFAULT_INTERVAL_MINUTES = max(MIN_AD_INTERVAL_MINUTES, int(os.getenv("DEFAULT_INTERVAL_MINUTES", "60")))
GROUP_DELAY_SECONDS = max(15, int(os.getenv("GROUP_DELAY_SECONDS", "30")))
DM_BROADCAST_DELAY_SECONDS = max(30, int(os.getenv("DM_BROADCAST_DELAY_SECONDS", "60")))
DM_REPLY_SEND_GAP_SECONDS = max(2, int(os.getenv("DM_REPLY_SEND_GAP_SECONDS", "5")))
RESTRICTION_PAUSE_HOURS = max(24, int(os.getenv("RESTRICTION_PAUSE_HOURS", "24")))
JOIN_QUEUE_INTERVAL_SECONDS = max(300, int(os.getenv("JOIN_QUEUE_INTERVAL_SECONDS", "300")))
FERNET_KEY = os.environ["FERNET_KEY"].encode()
fernet = Fernet(FERNET_KEY)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


class SecretRedactingFormatter(logging.Formatter):
    """Redact secrets even when they appear inside exception tracebacks or URLs."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self._secrets = [x for x in secrets if x]

    def format(self, record):
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def configure_logging():
    # HTTPX logs Telegram Bot API URLs; those URLs contain the bot token.
    # Keep request-level libraries quiet and redact secrets from every root log.
    for name in ("httpx", "httpcore"):
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.CRITICAL)
        noisy.propagate = False
        for h in list(noisy.handlers):
            noisy.removeHandler(h)


    # Telethon logs automatic flood-wait sleeps at INFO. Keep warnings/errors,
    # while the cache below prevents repeated GetDialogsRequest calls.
    logging.getLogger("telethon.client.users").setLevel(logging.WARNING)
    # "Got difference for channel ... updates" fires on every incoming update; it is not an error.
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)

    formatter = SecretRedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        [BOT_TOKEN, API_HASH, MONGO_URI, FERNET_KEY.decode("utf-8", errors="ignore")],
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(formatter)


configure_logging()
log = logging.getLogger("promo-bot")

pacer = AccountPacer()
# Tenant-aware proxies are initialized below, after RuntimeStats is declared.
# Operator data is tenant-isolated in MongoDB; downloaded media remains on disk.
# Legacy owner sessions remain accessible through the Mongo session store.


# ---- Inline menu system (no bottom Reply Keyboards) ----
import hashlib
SUBMENU_LABELS: dict[str, str] = {}
NAV_LABELS = {"📣 Promotion", "📝 Posts", "👥 Accounts", "🤖 Auto Reply", "✏️ Edit Bio", "📊 Monitoring",
              "🔗 Join Groups", "🤖 Bot Accounts", "➕ Add Session", "🔀 Sending Mode", "⬅️ Back"}


BUTTON_STYLES = os.getenv("BUTTON_STYLES", "true").lower() in {"true", "1", "yes", "on"}
_DANGER = ("Stop", "Remove", "Delete", "Clear")
_SUCCESS = ("Start", "Add", "Save", "Toggle", "Use Saved", "Rotate", "Random", "Individual", "Set All")
_PRIMARY = ("Promotion", "Posts", "Accounts", "Auto Reply", "Edit Bio", "Monitoring", "Admin Panel",
            "Sessions", "Join", "Bot Accounts", "Stats", "Logs", "Schedule", "Status", "Users")


def styled_button(label: str, callback_data: str) -> InlineKeyboardButton:
    """Inline button with Telegram's supported colour style (danger=red, success=green, primary=blue).
    Back/neutral buttons keep the default look. Sent via api_kwargs so older clients just ignore it."""
    style = None
    if BUTTON_STYLES and "Back" not in label:
        if any(w in label for w in _DANGER):
            style = "danger"
        elif any(w in label for w in _SUCCESS):
            style = "success"
        elif any(w in label for w in _PRIMARY):
            style = "primary"
    if style:
        return InlineKeyboardButton(label, callback_data=callback_data, api_kwargs={"style": style})
    return InlineKeyboardButton(label, callback_data=callback_data)


def _inline_from_rows(rows, **_ignored):
    """Build an InlineKeyboardMarkup; each button routes back to handle_menu by its label."""
    out = []
    for row in rows:
        btns = []
        for label in row:
            cb = "sm:" + hashlib.md5(label.encode()).hexdigest()[:12]
            SUBMENU_LABELS[cb] = label
            btns.append(styled_button(label, cb))
        out.append(btns)
    return InlineKeyboardMarkup(out)

# Normal replies clear any sub-menu reply keyboard; the main menu is inline (MAIN_INLINE).
MENU = ReplyKeyboardRemove()
MAIN_TEXT = (
    "📢 <b>Promotion Bot</b>\n\nChoose a category below. Promotion, accounts, replies, "
    "bio tools and monitoring are grouped to keep the main screen clean."
)
MAIN_INLINE = InlineKeyboardMarkup(
    [
        [styled_button("📣 Promotion", "main:promotion")],
        [styled_button("📝 Posts", "main:posts"), styled_button("👥 Accounts", "main:accounts")],
        [styled_button("🤖 Auto Reply", "main:autoreply"), styled_button("✏️ Edit Bio", "main:bio")],
        [styled_button("📊 Monitoring", "main:monitoring")],
    ]
)
# Each inline callback is routed through handle_menu using the same label as before.
MAIN_CALLBACK_LABELS = {
    "main:promotion": "📣 Promotion",
    "main:posts": "📝 Posts",
    "main:accounts": "👥 Accounts",
    "main:autoreply": "🤖 Auto Reply",
    "main:bio": "✏️ Edit Bio",
    "main:monitoring": "📊 Monitoring",
}

PROMOTION_MENU = _inline_from_rows(
    [
        ["▶️ Start Promo", "⏹ Stop Promo"],
        ["⏱ Interval", "⏳ Group Delay"],
        ["🔄 Rescan Targets", "📨 DM Broadcast"],
        ["📅 Schedule"],
        ["🌙 Quiet Hours"],
        ["🎯 Promo Accounts"],
        ["⬅️ Back"],
    ],
)

ACCOUNTS_MENU = _inline_from_rows(
    [
        ["➕ Add Session", "👤 Sessions"],
        ["🔗 Join Groups", "🤖 Bot Accounts"],
        ["🗑 Remove Session"],
        ["⬅️ Back"],
    ],
)

JOIN_GROUPS_MENU = _inline_from_rows(
    [
        ["➕ Add Group Links", "📋 Join Queue"],
        ["🗑 Clear Pending Joins"],
        ["⬅️ Back"],
    ],
)

MONITORING_MENU = _inline_from_rows(
    [
        ["📊 Live Stats", "📊 Account Stats"],
        ["📜 Account Logs"],
        ["⬅️ Back"],
    ],
)

BIO_MENU = _inline_from_rows(
    [
        ["🤖 Set Bot Bio", "👤 Set User Bio"],
        ["⬅️ Back"],
    ],
)

BIO_SCOPE_MENU = _inline_from_rows(
    [
        ["👤 Individual", "🌐 Set All"],
        ["⬅️ Back"],
    ],
)

POST_MENU = _inline_from_rows(
    [
        ["📝 Save Post", "📋 List Posts"],
        ["👁 Preview Post", "🔘 Toggle Post"],
        ["🗑 Delete Post", "🔀 Sending Mode"],
        ["⬅️ Back"],
    ],
)

POST_MODE_MENU = _inline_from_rows(
    [["🔄 Rotate", "🎲 Random"], ["⬅️ Back"]],
)

AUTO_REPLY_MENU = _inline_from_rows(
    [
        ["✏️ Account Reply", "✏️ Control Bot Reply"],
        ["👁 Reply Preview"],
        ["🟢 Toggle Account Reply", "🟢 Toggle Bot Reply"],
        ["🤖 AI Mode", "🔑 Groq API Key"],
        ["📚 AI Profile"],
        ["⏱ Reply Cooldown", "📡 Reply Status"],
        ["⬅️ Back"],
    ],
)
AI_POLICY_MENU = _inline_from_rows([["✅ I Accept These Rules"], ["⬅️ Back"]])

ADD_SESSION_MENU = _inline_from_rows(
    [
        ["🔑 Session String", "📱 Login with Phone"],
        ["⬅️ Back"],
    ],
)

BOT_ACCOUNT_MENU = _inline_from_rows(
    [
        ["➕ Add Bot", "👤 Bot Accounts"],
        ["📊 Bot Stats", "📜 Bot Logs"],
        ["🟢 Toggle Bot Auto Reply", "⏱ Bot Reply Cooldown"],
        ["🗑 Remove Bot"],
        ["⬅️ Back"],
    ],
)

BOT_GROUP_REPLY_DEFAULT = os.getenv(
    "BOT_GROUP_REPLY_TEXT",
    "👋 Thanks for your message."
).replace("\\n", "\n")
BOT_GROUP_REPLY_COOLDOWN_SECONDS = max(60, int(os.getenv("BOT_GROUP_REPLY_COOLDOWN_SECONDS", "600")))

login_flows: dict[int, dict] = {}
# In-memory target cache: session Mongo ID -> {groups, users, scanned_at}.
# Cached InputPeer objects avoid a fresh GetDialogsRequest on every promotion round.
target_cache: dict[str, dict] = {}
TARGET_CACHE_TTL = max(300, int(os.getenv("TARGET_CACHE_TTL_SECONDS", "21600")))
# A join event updates the target list immediately; a slow background sync repairs
# missed events after Telegram reconnects. No operator-initiated rescan is needed.
AUTO_GROUP_SYNC_SECONDS = max(300, int(os.getenv("AUTO_GROUP_SYNC_SECONDS", "600")))

# Direct replies only to people who initiate a private conversation.
DM_AUTO_REPLY_ENABLED = os.getenv("DM_AUTO_REPLY_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
ACCOUNT_DM_AUTO_REPLY_ENABLED = os.getenv("ACCOUNT_DM_AUTO_REPLY_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
DM_REPLY_COOLDOWN_SECONDS = max(60, int(os.getenv("DM_REPLY_COOLDOWN_SECONDS", "300")))
DM_AUTO_REPLY_TEXT = os.getenv(
    "DM_AUTO_REPLY_TEXT",
    "👋 Hi! Thanks for reaching out. Your message has been received. We will reply when available."
).replace("\\n", "\n")
ACCOUNT_DM_AUTO_REPLY_TEXT = os.getenv(
    "ACCOUNT_DM_AUTO_REPLY_TEXT",
    "👋 Hi! Thanks for messaging. We will get back to you soon."
).replace("\\n", "\n")
DM_AUTO_REPLY_USE_SAVED_POST = os.getenv("DM_AUTO_REPLY_USE_SAVED_POST", "false").lower() in {"true", "1", "yes", "on"}
async def reply_setting(key: str, env_default: str) -> str:
    return await db.get_setting(key, env_default)


async def reply_enabled(key: str, default: bool) -> bool:
    return (await reply_setting(key, "true" if default else "false")).lower() in {"true", "1", "yes", "on"}


async def reply_cooldown() -> int:
    try:
        return max(60, int(await reply_setting("dm_reply_cooldown_seconds", str(DM_REPLY_COOLDOWN_SECONDS))))
    except (ValueError, TypeError):
        return DM_REPLY_COOLDOWN_SECONDS


def _client_key_from_row(row: dict) -> str:
    tg_id = row.get("tg_user_id")
    return str(tg_id if tg_id is not None else row["id"])


async def reply_panel_text() -> str:
    acc = await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED)
    bot = await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED)
    saved_post = await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST)
    cooldown = await reply_cooldown()
    tenant_client_keys = {_client_key_from_row(row) for row in await session_store.get_sessions()}
    connected_listeners = sum(
        client.is_connected()
        for account_key, client in _account_dm_clients.items()
        if account_key in tenant_client_keys
    )
    return (
        "💬 <b>AUTO REPLY SETTINGS</b>\n\n"
        f"👤 Connected real IDs: <b>{'ON' if acc else 'OFF'}</b>\n"
        f"🤖 Control bot: <b>{'ON' if bot else 'OFF'}</b>\n"
        f"📝 Real-ID reply mode: <b>{'Saved promo post' if saved_post else 'Custom text'}</b>\n"
        f"⏱ Cooldown: <b>{cooldown // 60} min</b>\n\n"
        f"📡 Real-ID listeners connected: <b>{connected_listeners}</b>\n"
        f"✅ Account replies sent since restart: <b>{_reply_sent_counts['accounts']}</b>\n\n"
        "Choose a button to edit. Changes apply without restart."
    )


# A single persistent Telethon connection per account is shared by group promotion,
# manual DM broadcasts and incoming-DM handlers. Separate simultaneous clients using
# the same StringSession can compete for updates, making auto-replies unreliable.
# One live Telethon connection per actual Telegram account ID.
_account_dm_clients: dict[str, TelegramClient] = {}
_account_client_locks: dict[str, asyncio.Lock] = {}
_session_client_keys: dict[str, str] = {}
_account_reply_timestamps: dict[tuple[str, int], float] = {}
_account_reply_pending: set[tuple[str, int]] = set()
_TENANT_REPLY_COUNTS: dict[int, dict[str, int]] = {}
class _ReplyCountProxy:
    def _data(self):
        return _TENANT_REPLY_COUNTS.setdefault(tenant_id(),
                                                {"accounts": 0, "control_bot": 0, "failed": 0})
    def __getitem__(self, key):
        return self._data()[key]
    def __setitem__(self, key, value):
        self._data()[key] = value
_reply_sent_counts = _ReplyCountProxy()
_bot_reply_timestamps: dict[int, float] = {}

scan_locks: dict[str, asyncio.Lock] = {}
# New groups seen before an account's first scan are merged into that scan.
pending_groups: dict[str, dict] = {}
# Avoid back-to-back repeats per target in Random mode (while the process is running).
last_random_post_by_target: dict[tuple[str, str], int] = {}

@dataclass
class RuntimeStats:
    running: bool = False
    round_no: int = 0
    started_at: float | None = None
    current_session: str = "-"
    current_group: str = "-"
    current_post: str = "-"
    post_mode: str = "rotate"
    active_posts: int = 0
    total_groups: int = 0
    total_users: int = 0
    group_attempted: int = 0
    group_sent: int = 0
    group_failed: int = 0
    group_skipped: int = 0
    user_attempted: int = 0
    user_sent: int = 0
    user_failed: int = 0
    user_skipped: int = 0
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    flood_waits: int = 0
    paused_sessions: int = 0
    group_delay_seconds: int = GROUP_DELAY_SECONDS
    last_error: str = "-"
    next_round_at: float | None = None
    cached_group_count: dict = field(default_factory=dict)
    cached_user_count: dict = field(default_factory=dict)

# The current operator is bound to each bot update and inherited by background
# tasks. Persistent tenant settings/posts/sessions are isolated by owner_id in MongoDB.
CURRENT_OPERATOR = contextvars.ContextVar("operator", default=ADMIN_ID)
APPROVED_USERS: set[int] = {ADMIN_ID}
TENANT_DB: dict[int, Database] = {}
TENANT_SESSIONS: dict[int, MongoSessionStore] = {}
TENANT_STATS: dict[int, RuntimeStats] = {}
PROMO_TASKS: dict[int, asyncio.Task] = {}
BOT_KEY_BLOCKED: dict[tuple[int, int], float] = {}   # bot listeners waiting out an undecryptable token
KEY_MISMATCH_LOGGED: set = set()     # accounts already reported for an undecryptable session
LISTENER_TASKS: dict[int, asyncio.Task] = {}
JOIN_QUEUE_TASKS: dict[int, asyncio.Task] = {}
BOT_ACCOUNT_TASKS: dict[tuple[int, int], asyncio.Task] = {}
BOT_ACCOUNT_STATUS: dict[tuple[int, int], str] = {}
OPERATORS_DIR = Path(os.getenv("OPERATORS_DIR", "operators"))

# Owner-only operator access list; a separate collection from encrypted sessions.
from pymongo import MongoClient as _AccessMongoClient
_access_mongo = _AccessMongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
operator_access = _access_mongo[MONGO_DB]["approved_operators"]

# Account-specific, tenant-isolated statistics and durable log files.
# Separate promotion and manual-DM counters, so two active accounts cannot
# overwrite each other's current target or status.
ACCOUNT_METRICS: dict[tuple[int, str, str], dict] = {}
ACCOUNT_EVENTS: dict[tuple[int, str, str], deque] = {}
ACCOUNT_PROGRESS_MESSAGES: dict[tuple[int, str, str], object] = {}


def metric_key(session_id, kind):
    return (tenant_id(), str(session_id), kind)


def begin_metric(row, kind, total=0, groups=0, users=0):
    key = metric_key(row["id"], kind)
    metric = {
        "label": str(row["label"]), "kind": kind, "total": total,
        "groups": groups, "users": users, "attempted": 0,
        "sent": 0, "failed": 0, "skipped": 0, "flood_waits": 0,
        "group_sent": 0, "user_sent": 0,
        "status": "Starting", "target": "-", "last_error": "-",
        "started_at": time.time(), "last_edit": 0.0,
    }
    ACCOUNT_METRICS[key] = metric
    ACCOUNT_EVENTS[key] = deque(maxlen=30)
    return metric


def account_stats_text(metric):
    pct = 100 * metric["attempted"] / metric["total"] if metric["total"] else 0
    return (
        f"📊 <b>{'GROUP PROMOTION' if metric['kind'] == 'promo' else 'DM BROADCAST'} — ACCOUNT</b>\n"
        f"👤 <b>{esc(metric['label'])}</b>\n"
        f"State: <b>{esc(metric['status'])}</b>\n"
        f"Progress: <b>{metric['attempted']}/{metric['total']}</b> ({pct:.0f}%)\n"
        f"✅ Sent: <b>{metric['sent']}</b> | ❌ Failed: <b>{metric['failed']}</b> "
        f"| ⏭ Skipped: <b>{metric['skipped']}</b>\n"
        f"👥 Group sends: <b>{metric['group_sent']}</b> | 👤 DM sends: <b>{metric['user_sent']}</b>\n"
        f"⏳ Flood waits: <b>{metric['flood_waits']}</b>\n"
        f"🎯 Current: {esc(metric['target'])[:100]}\n"
        f"⏱ Elapsed: <b>{fmt_duration(time.time()-metric['started_at'])}</b>\n"
        f"⚠️ Last error: {esc(metric['last_error'])[:160]}"
    )


def account_log_path(key):
    owner, sid, kind = key
    safe_sid = ''.join(c for c in sid if c.isalnum() or c in '-_')[:80]
    base = Path('logs') if owner == ADMIN_ID else OPERATORS_DIR / str(owner) / 'logs'
    base.mkdir(parents=True, exist_ok=True)
    return base / f'{safe_sid}-{kind}.log'


def _append_account_log(path, line):
    with path.open('a', encoding='utf-8') as fp:
        fp.write(line + '\n')


async def record_account_event(row, metric, target_type, target_name, outcome, error=''):
    key = metric_key(row['id'], metric['kind'])
    metric['attempted'] += 1
    metric['target'] = f"{target_type}: {target_name}"
    if outcome == 'sent':
        metric['sent'] += 1
        metric['group_sent' if target_type == 'group' else 'user_sent'] += 1
    elif outcome == 'skipped':
        metric['skipped'] += 1
    else:
        metric['failed'] += 1
    if error:
        metric['last_error'] = error
    line = (f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | "
            f"{outcome.upper()} | {target_type} | {target_name}"
            + (f" | {error}" if error else ''))
    ACCOUNT_EVENTS.setdefault(key, deque(maxlen=30)).append(line)
    log.info('account=%s tenant=%s mode=%s %s', row['label'], tenant_id(), metric['kind'], line)
    try:
        await asyncio.to_thread(_append_account_log, account_log_path(key), line)
    except OSError:
        log.warning('Account log write failed for %s', row['label'])


DASH: dict[int, dict] = {}          # tenant -> {"msg","lock","last","text","task"}
DASH_EDIT_SECONDS = 10
DASH_MAX_CHARS = 3800


def _dash_account_block(metric):
    kind = "Promo" if metric["kind"] == "promo" else "DM"
    return (f"👤 <b>{esc(metric['label'])}</b> · {kind}\n"
            f"State: <b>{esc(metric['status'])}</b> · {metric['attempted']}/{metric['total']}\n"
            f"✅ {metric['sent']} · ❌ {metric['failed']} · ⏭ {metric['skipped']} · ⏳ FW {metric['flood_waits']}\n"
            f"🎯 {esc(metric['target'])[:80]}\n"
            f"⚠️ {esc(metric['last_error'])[:100]}")


def dash_text() -> str:
    """One message: live stats plus a compact block per account, kept under Telegram's limit."""
    head = stats_text()
    tid = tenant_id()
    blocks = [_dash_account_block(m) for key, m in ACCOUNT_METRICS.items() if key[0] == tid]
    if not blocks:
        return head
    kept, used = [], len(head) + 40
    for block in blocks:
        if used + len(block) + 2 > DASH_MAX_CHARS:
            break
        kept.append(block)
        used += len(block) + 2
    more = len(blocks) - len(kept)
    tail = f"\n…and {more} more account(s)" if more else ""
    return head + "\n\n👥 <b>ACCOUNTS</b>\n\n" + "\n\n".join(kept) + tail


async def _dash_flusher():
    while True:
        await asyncio.sleep(DASH_EDIT_SECONDS)
        try:
            await dash_refresh(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dashboard flusher error")


async def dash_ensure(bot):
    """Create (and pin) the dashboard once per operator; later runs reuse and edit it."""
    tid = tenant_id()
    st = DASH.setdefault(tid, {"msg": None, "lock": asyncio.Lock(), "last": 0.0, "text": None, "task": None})
    if st["msg"] is None:
        try:
            msg = await bot.send_message(tid, dash_text(), parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("could not create promotion dashboard")
            return st
        st["msg"], st["text"] = msg, dash_text()
        try:
            old = await db.get_setting("dash_message_id", None)
            if old and str(old) != str(msg.message_id):
                await bot.unpin_chat_message(chat_id=tid, message_id=int(old))
        except Exception:
            pass
        try:
            await bot.pin_chat_message(chat_id=tid, message_id=msg.message_id, disable_notification=True)
            await db.set_setting("dash_message_id", str(msg.message_id))
        except Exception:
            log.warning("could not pin promotion dashboard")
    if st["task"] is None or st["task"].done():
        st["task"] = asyncio.create_task(_dash_flusher())
    return st


async def dash_refresh(force=False):
    st = DASH.get(tenant_id())
    if not st or st["msg"] is None:
        return
    if not force and time.monotonic() - st["last"] < DASH_EDIT_SECONDS:
        return
    async with st["lock"]:
        text = dash_text()
        if text == st["text"]:
            return
        st["last"] = time.monotonic()
        try:
            await st["msg"].edit_text(text, parse_mode=ParseMode.HTML)
            st["text"] = text
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("dashboard edit failed: %s", exc)
        except Exception as exc:
            log.warning("dashboard edit failed: %s", type(exc).__name__)


async def dash_finish():
    st = DASH.get(tenant_id())
    if not st:
        return
    if st["task"] and not st["task"].done():
        st["task"].cancel()
    st["task"] = None
    await dash_refresh(force=True)


async def edit_account_progress(row, metric, *, force=False):
    await dash_refresh()          # throttled; the flusher sends the latest state


async def send_account_telemetry(update, kind):
    rows = await session_store.get_sessions(enabled_only=False)
    if not rows:
        return await update.message.reply_text('No accounts saved.', reply_markup=MENU)
    for row in rows:
        sid = str(row['id'])
        if kind == 'stats':
            blocks = []
            for mode in ('promo', 'dm'):
                metric = ACCOUNT_METRICS.get(metric_key(sid, mode))
                if metric:
                    blocks.append(account_stats_text(metric))
            if not blocks:
                blocks.append(f"👤 <b>{esc(row['label'])}</b>\nNo sending activity since restart.")
            # Separate message for each account, with distinct promo and DM sections.
            for block in blocks:
                await update.message.reply_text(block, parse_mode=ParseMode.HTML, reply_markup=MENU)
        else:
            for mode in ('promo', 'dm'):
                key = metric_key(sid, mode)
                recent = ACCOUNT_EVENTS.get(key)
                if not recent:
                    try:
                        path = account_log_path(key)
                        if path.exists():
                            with path.open(encoding='utf-8', errors='replace') as f:
                                recent = deque(f, maxlen=10)
                    except OSError:
                        recent = None
                if recent:
                    lines = list(recent)[-10:]
                    heading = 'Group promotion' if mode == 'promo' else 'Manual DM broadcast'
                    contents = '\n'.join(esc(x.strip())[:230] for x in lines)
                    await update.message.reply_text(
                        f"📜 <b>{esc(row['label'])} — {heading}</b>\n\n<code>{contents[:3100]}</code>",
                        parse_mode=ParseMode.HTML, reply_markup=MENU)
    return None


def tenant_id() -> int:
    return CURRENT_OPERATOR.get()

def tenant_db(uid: int) -> Database:
    if uid not in TENANT_DB:
        TENANT_DB[uid] = Database(MONGO_URI, MONGO_DB, owner_id=uid)
    return TENANT_DB[uid]

def tenant_sessions(uid: int) -> MongoSessionStore:
    if uid not in TENANT_SESSIONS:
        TENANT_SESSIONS[uid] = MongoSessionStore(MONGO_URI, MONGO_DB, owner_id=uid,
                                                primary_owner=(uid == ADMIN_ID))
    return TENANT_SESSIONS[uid]

def tenant_stats(uid: int) -> RuntimeStats:
    return TENANT_STATS.setdefault(uid, RuntimeStats())

class _TenantProxy:
    def __init__(self, get_instance):
        object.__setattr__(self, "get_instance", get_instance)
    def __getattr__(self, name):
        return getattr(self.get_instance(tenant_id()), name)
    def __setattr__(self, name, value):
        setattr(self.get_instance(tenant_id()), name, value)

db = _TenantProxy(tenant_db)
session_store = _TenantProxy(tenant_sessions)
stats = _TenantProxy(tenant_stats)
state: dict[int, str] = {}

def tenant_media_dir() -> Path:
    result = MEDIA_DIR if tenant_id() == ADMIN_ID else OPERATORS_DIR / str(tenant_id()) / "media"
    result.mkdir(parents=True, exist_ok=True)
    return result

def broadcast_running(app):
    return app.bot_data.get(f"dm_broadcast_running:{tenant_id()}", False)

def set_broadcast_running(app, value):
    app.bot_data[f"dm_broadcast_running:{tenant_id()}"] = value

def get_broadcast_task(app):
    return app.bot_data.get(f"dm_broadcast_task:{tenant_id()}")

def set_broadcast_task(app, task):
    app.bot_data[f"dm_broadcast_task:{tenant_id()}"] = task

async def load_approved_users():
    docs = await asyncio.to_thread(lambda: list(operator_access.find({}, {"_id": 1})))
    APPROVED_USERS.clear()
    APPROVED_USERS.add(ADMIN_ID)
    APPROVED_USERS.update(int(doc["_id"]) for doc in docs)

async def initialize_tenant(uid: int):
    token = CURRENT_OPERATOR.set(uid)
    try:
        await tenant_db(uid).init()
        await tenant_sessions(uid).init()
        if await db.get_setting("interval_minutes") is None:
            await db.set_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))
        if await db.get_setting("group_delay_seconds") is None:
            await db.set_setting("group_delay_seconds", str(GROUP_DELAY_SECONDS))
        stats.group_delay_seconds = await get_group_delay()
    finally:
        CURRENT_OPERATOR.reset(token)

def retry_after_seconds(value) -> float:
    """Normalize PTB RetryAfter value (number or timedelta) to seconds."""
    if hasattr(value, "total_seconds"):
        return max(0.0, float(value.total_seconds()))
    return max(0.0, float(value))


async def bot_group_reply_enabled_for(owner_uid: int) -> bool:
    token = CURRENT_OPERATOR.set(owner_uid)
    try:
        return (await db.get_setting("bot_group_reply_enabled", "true")).lower() in {"true", "1", "yes", "on"}
    finally:
        CURRENT_OPERATOR.reset(token)


async def bot_group_reply_cooldown_for(owner_uid: int) -> int:
    token = CURRENT_OPERATOR.set(owner_uid)
    try:
        try:
            return max(60, int(await db.get_setting(
                "bot_group_reply_cooldown_seconds", str(BOT_GROUP_REPLY_COOLDOWN_SECONDS)
            )))
        except (TypeError, ValueError):
            return BOT_GROUP_REPLY_COOLDOWN_SECONDS
    finally:
        CURRENT_OPERATOR.reset(token)


async def bot_group_reply_post_for(owner_uid: int):
    """Return the next enabled saved promotion post for bot group replies."""
    token = CURRENT_OPERATOR.set(owner_uid)
    try:
        mode = await db.get_setting("post_mode", "rotate")
        if mode not in {"rotate", "random"}:
            mode = "rotate"
        return await db.next_reply_post(mode)
    finally:
        CURRENT_OPERATOR.reset(token)


async def bot_dm_auto_reply_text_for(owner_uid: int) -> str:
    """Use the configured Auto Reply text for private DMs to added bot accounts."""
    token = CURRENT_OPERATOR.set(owner_uid)
    try:
        return await db.get_setting("control_bot_dm_text", DM_AUTO_REPLY_TEXT)
    finally:
        CURRENT_OPERATOR.reset(token)


async def send_bot_saved_post_reply(api_bot: Bot, chat_id: int, reply_to_message_id: int, post: dict):
    """Send one saved promotion post as an actual reply via the connected bot account."""
    caption = post.get("html_text") or None
    media_path = post.get("media_path")
    common = dict(
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        allow_sending_without_reply=False,
    )
    if media_path:
        if not Path(media_path).exists():
            raise FileNotFoundError(f"Saved media missing: {media_path}")
        with open(media_path, "rb") as fh:
            kind = post.get("kind")
            if kind == "photo":
                return await api_bot.send_photo(photo=fh, caption=caption, parse_mode=ParseMode.HTML, **common)
            if kind == "video":
                return await api_bot.send_video(video=fh, caption=caption, parse_mode=ParseMode.HTML, **common)
            if kind == "animation":
                return await api_bot.send_animation(animation=fh, caption=caption, parse_mode=ParseMode.HTML, **common)
            return await api_bot.send_document(document=fh, caption=caption, parse_mode=ParseMode.HTML, **common)
    if not caption:
        raise ValueError("Saved promotion post is empty")
    return await api_bot.send_message(
        text=caption,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        **common,
    )


def decrypt_bot_token(value: str) -> str:
    try:
        return fernet.decrypt(value.encode()).decode()
    except (InvalidToken, AttributeError) as exc:
        raise RuntimeError("Stored bot token cannot be decrypted with current FERNET_KEY") from exc


async def _bot_account_listener(app: Application, owner_uid: int, bot_id: int):
    """Long-poll one connected Bot API account and auto-reply in groups and DMs.

    Groups use enabled saved promotion posts. Private DMs use the configured
    Auto Reply text. Cooldowns persist in MongoDB across restarts.
    """
    key = (int(owner_uid), int(bot_id))
    token_ctx = CURRENT_OPERATOR.set(owner_uid)
    api_bot = None
    try:
        row = await db.get_bot_account(bot_id)
        if not row or not row.get("enabled", True):
            return
        token_raw = decrypt_bot_token(row["token_enc"])
        api_bot = Bot(token=token_raw)
        await api_bot.initialize()
        # Bot API getUpdates cannot work while a webhook is configured.
        # Remove stale webhooks automatically so the added bot can become active
        # on this server without manual cleanup. Pending updates are preserved.
        try:
            await api_bot.delete_webhook(drop_pending_updates=False)
        except TelegramError as exc:
            log.warning("Could not clear webhook owner=%s bot_id=%s: %s", owner_uid, bot_id, type(exc).__name__)
        me = await api_bot.get_me()
        can_read_all = bool(getattr(me, "can_read_all_group_messages", False))
        BOT_ACCOUNT_STATUS[key] = (
            "🟢 LISTENING" if can_read_all
            else "🟠 LISTENING • PRIVACY ON"
        )
        offset = None
        retry_delay = 2
        log.info("Bot account listener connected owner=%s bot_id=%s username=%s", owner_uid, bot_id, me.username)

        while True:
            row = await db.get_bot_account(bot_id)
            if not row or not row.get("enabled", True):
                BOT_ACCOUNT_STATUS[key] = "⚪ DISABLED"
                return
            try:
                updates = await api_bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message", "my_chat_member"],
                    read_timeout=40,
                    connect_timeout=20,
                    pool_timeout=20,
                )
                retry_delay = 2
                for upd in updates:
                    offset = upd.update_id + 1

                    member_upd = getattr(upd, "my_chat_member", None)
                    if member_upd and member_upd.chat and member_upd.chat.type in {"group", "supergroup"}:
                        new_status = str(getattr(member_upd.new_chat_member, "status", ""))
                        active = new_status not in {"left", "kicked"}
                        await db.upsert_bot_group(
                            bot_id, member_upd.chat.id, member_upd.chat.title or str(member_upd.chat.id), active
                        )
                        await db.record_bot_event(
                            bot_id, "joined" if active else "left", "group", member_upd.chat.id,
                            getattr(member_upd.from_user, "id", None), member_upd.chat.title or ""
                        )
                        continue

                    msg = upd.effective_message
                    usr = upd.effective_user
                    chat = upd.effective_chat
                    if not msg or not usr or not chat:
                        continue
                    if chat.type in {"group", "supergroup"}:
                        await db.upsert_bot_group(bot_id, chat.id, chat.title or str(chat.id), True)
                    if chat.type not in {"private", "group", "supergroup"}:
                        continue
                    # Ignore join/leave/pin/topic and other service updates; reply only to user content.
                    if not msg.text and msg.effective_attachment is None:
                        continue
                    if usr.is_bot or int(usr.id) == int(bot_id):
                        continue
                    if not await bot_group_reply_enabled_for(owner_uid):
                        continue

                    cooldown = await bot_group_reply_cooldown_for(owner_uid)
                    allowed = await db.bot_reply_allowed(bot_id, chat.id, usr.id, cooldown)
                    if not allowed:
                        continue

                    scope = "dm" if chat.type == "private" else "group"
                    try:
                        if chat.type == "private":
                            dm_text = (await bot_dm_auto_reply_text_for(owner_uid) or "").strip()
                            if not dm_text:
                                await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                                await db.record_bot_event(bot_id, "skipped", scope, chat.id, usr.id, "Auto Reply text is empty")
                                continue
                            await api_bot.send_message(
                                chat_id=chat.id,
                                text=dm_text,
                                reply_to_message_id=msg.message_id,
                                allow_sending_without_reply=False,
                                disable_web_page_preview=True,
                            )
                            await db.record_bot_event(bot_id, "sent", scope, chat.id, usr.id, "Auto Reply text")
                        else:
                            post = await bot_group_reply_post_for(owner_uid)
                            if not post:
                                await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                                await db.record_bot_event(bot_id, "skipped", scope, chat.id, usr.id, "No enabled promotion post")
                                continue
                            await send_bot_saved_post_reply(api_bot, chat.id, msg.message_id, post)
                            await db.record_bot_event(bot_id, "sent", scope, chat.id, usr.id, f"post#{post.get('id', '?')}")
                    except RetryAfter as exc:
                        await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                        await db.record_bot_event(bot_id, "skipped", scope, chat.id, usr.id, f"RetryAfter {int(retry_after_seconds(exc.retry_after))}s")
                        await asyncio.sleep(retry_after_seconds(exc.retry_after) + 1.0)
                    except (Forbidden, BadRequest) as exc:
                        # No send permission / deleted message / blocked bot.
                        await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                        await db.record_bot_event(bot_id, "skipped", scope, chat.id, usr.id, type(exc).__name__)
                        log.info(
                            "Bot %s reply skipped owner=%s bot_id=%s chat=%s: %s",
                            scope, owner_uid, bot_id, chat.id, type(exc).__name__,
                        )
                    except (NetworkError, TimedOut) as exc:
                        await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                        await db.record_bot_event(bot_id, "failed", scope, chat.id, usr.id, type(exc).__name__)
                    except (TypeError, ValueError, FileNotFoundError) as exc:
                        # Saved-post formatting/media problems must not kill the whole bot listener.
                        await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                        detail = f"{type(exc).__name__}: {exc}"[:500]
                        await db.record_bot_event(bot_id, "failed", scope, chat.id, usr.id, detail)
                        log.warning(
                            "Bot %s reply data error owner=%s bot_id=%s chat=%s: %s",
                            scope, owner_uid, bot_id, chat.id, detail,
                        )
                    except TelegramError as exc:
                        await db.release_bot_reply_slot(bot_id, chat.id, usr.id)
                        await db.record_bot_event(bot_id, "failed", scope, chat.id, usr.id, type(exc).__name__)
                        log.warning(
                            "Bot %s reply failed owner=%s bot_id=%s chat=%s: %s",
                            scope, owner_uid, bot_id, chat.id, type(exc).__name__,
                        )
            except Conflict:
                BOT_ACCOUNT_STATUS[key] = "⚠️ POLLING CONFLICT • TOKEN USED ELSEWHERE"
                # Another getUpdates consumer is using the same token. A webhook is
                # already cleared above, so this normally means another running bot process.
                await db.record_bot_event(bot_id, "failed", "listener", 0, None, "Polling conflict: token used elsewhere")
                await asyncio.sleep(30)
            except RetryAfter as exc:
                BOT_ACCOUNT_STATUS[key] = f"⏳ FLOODWAIT {int(retry_after_seconds(exc.retry_after))}s"
                await asyncio.sleep(retry_after_seconds(exc.retry_after) + 1.0)
            except (NetworkError, TimedOut):
                BOT_ACCOUNT_STATUS[key] = "🟡 RECONNECTING"
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                BOT_ACCOUNT_STATUS[key] = f"🔴 ERROR {type(exc).__name__}"
                log.exception("Bot account listener error owner=%s bot_id=%s: %s: %s", owner_uid, bot_id, type(exc).__name__, exc)
                await asyncio.sleep(min(30, retry_delay))
                retry_delay = min(30, retry_delay * 2)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        BOT_ACCOUNT_STATUS[key] = f"🔴 ERROR {type(exc).__name__}"
        if "FERNET_KEY" in str(exc):
            # Token saved with an old key: wait an hour before trying again instead of every sync.
            BOT_KEY_BLOCKED[key] = time.monotonic() + 3600
            if key not in KEY_MISMATCH_LOGGED:
                KEY_MISMATCH_LOGGED.add(key)
                log.error("Bot account %s: token cannot be decrypted with the current FERNET_KEY. "
                          "Restore the old key, or remove and add this bot again.", bot_id)
        else:
            log.warning("Bot account failed owner=%s bot_id=%s: %s: %s", owner_uid, bot_id, type(exc).__name__, exc)
    finally:
        if api_bot is not None:
            try:
                await api_bot.shutdown()
            except Exception:
                pass
        BOT_ACCOUNT_TASKS.pop(key, None)
        CURRENT_OPERATOR.reset(token_ctx)


async def start_bot_account_listener(app: Application, owner_uid: int, bot_id: int):
    key = (int(owner_uid), int(bot_id))
    old = BOT_ACCOUNT_TASKS.get(key)
    if old and not old.done():
        return
    if time.monotonic() < BOT_KEY_BLOCKED.get(key, 0):
        return
    BOT_ACCOUNT_TASKS[key] = asyncio.create_task(
        _bot_account_listener(app, owner_uid, bot_id),
        name=f"bot-account-{owner_uid}-{bot_id}",
    )


async def stop_bot_account_listener(owner_uid: int, bot_id: int):
    key = (int(owner_uid), int(bot_id))
    task = BOT_ACCOUNT_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    BOT_ACCOUNT_STATUS[key] = "⚪ DISABLED"


async def sync_bot_account_listeners(app: Application, owner_uid: int):
    token_ctx = CURRENT_OPERATOR.set(owner_uid)
    try:
        rows = await db.list_bot_accounts()
        wanted = {int(r["bot_id"]) for r in rows if r.get("enabled", True)}
    finally:
        CURRENT_OPERATOR.reset(token_ctx)
    for bid in wanted:
        await start_bot_account_listener(app, owner_uid, bid)
    for (uid, bid), task in list(BOT_ACCOUNT_TASKS.items()):
        if uid == owner_uid and bid not in wanted:
            await stop_bot_account_listener(uid, bid)


async def start_tenant_listener(app, uid: int):
    task = LISTENER_TASKS.get(uid)
    if task is not None and not task.done():
        return
    token = CURRENT_OPERATOR.set(uid)
    try:
        LISTENER_TASKS[uid] = asyncio.create_task(account_auto_reply_service(app),
                                                  name=f"account_dm_auto_reply_{uid}")
    finally:
        CURRENT_OPERATOR.reset(token)


def normalize_join_link(raw: str):
    """Return (original, normalized, kind, value) or None for unsupported links."""
    value = (raw or "").strip().strip("<>")
    if not value:
        return None
    if value.startswith("@"):  # public username
        username = value[1:].strip()
        if username and all(ch.isalnum() or ch == "_" for ch in username):
            return value, f"public:{username.lower()}", "public", username
        return None
    low = value.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            value = value[len(prefix):]
            low = value.lower()
            break
    if low.startswith("telegram.me/"):
        value = value[len("telegram.me/"):]
    elif low.startswith("t.me/"):
        value = value[len("t.me/"):]
    else:
        return None
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not value:
        return None
    if value.startswith("+"):
        invite = value[1:]
        return raw.strip(), f"invite:{invite}", "invite", invite if invite else None
    if value.lower().startswith("joinchat/"):
        invite = value.split("/", 1)[1]
        return raw.strip(), f"invite:{invite}", "invite", invite if invite else None
    # Message links (/c/...) and share links are not join targets.
    if value.lower().startswith(("c/", "share/", "addlist/")):
        return None
    username = value.split("/", 1)[0]
    if username and all(ch.isalnum() or ch == "_" for ch in username):
        return raw.strip(), f"public:{username.lower()}", "public", username
    return None


async def _attempt_session_join(row: dict, link: str):
    parsed = normalize_join_link(link)
    if not parsed:
        return "failed", "Unsupported or invalid Telegram group link"
    _, _, kind, target = parsed
    if not target:
        return "failed", "Invalid invite link"
    client = await get_account_client(row)
    try:
        if kind == "invite":
            await client(ImportChatInviteRequest(target))
        else:
            await client(JoinChannelRequest(target))
        return "joined", "Joined"
    except errors.UserAlreadyParticipantError:
        return "already", "Already joined"
    except getattr(errors, "InviteRequestSentError", type(None)):
        return "requested", "Join request sent"
    except errors.FloodWaitError as exc:
        await pause_account(str(row["id"]), max(1, int(exc.seconds)), f"Join queue FloodWait ({exc.seconds}s)")
        return "retry", f"FloodWait {int(exc.seconds)}s"
    except (getattr(errors, "PeerFloodError", type(None)), getattr(errors, "UserRestrictedError", type(None))) as exc:
        await pause_account(str(row["id"]), RESTRICTION_PAUSE_HOURS * 3600, f"Join queue restriction ({type(exc).__name__})")
        return "failed", type(exc).__name__
    except (getattr(errors, "ChannelsTooMuchError", type(None)), getattr(errors, "InviteHashExpiredError", type(None)), getattr(errors, "InviteHashInvalidError", type(None)), getattr(errors, "ChannelPrivateError", type(None))) as exc:
        return "failed", type(exc).__name__
    except errors.RPCError as exc:
        name = type(exc).__name__
        # Some Telegram layers return an updates object for request-to-join; newer
        # Telethon exposes InviteRequestSentError. Keep unknown RPC failures visible.
        return "failed", name
    except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
        return "retry", type(exc).__name__


async def join_queue_service(app: Application, owner_uid: int):
    """Persisted, per-account join queue. Each account attempts at most one item per 5 min."""
    token_ctx = CURRENT_OPERATOR.set(owner_uid)
    try:
        while owner_uid in APPROVED_USERS:
            try:
                enabled = (await db.get_setting("join_queue_enabled", "true")).lower() in {"true", "1", "yes", "on"}
                if not enabled:
                    await asyncio.sleep(10)
                    continue
                rows = await session_store.get_sessions(enabled_only=True)
                now = time.time()
                for row in rows:
                    sid = str(row["id"])
                    try:
                        last = float(await db.get_setting(f"join_queue_last_attempt:{sid}", "0") or 0)
                    except (TypeError, ValueError):
                        last = 0.0
                    if now - last < JOIN_QUEUE_INTERVAL_SECONDS:
                        continue
                    item = await db.next_pending_join(sid)
                    if not item:
                        continue
                    # Persist the pacing timestamp before attempting so restart cannot
                    # cause a burst of back-to-back joins.
                    await db.set_setting(f"join_queue_last_attempt:{sid}", str(time.time()))
                    status, detail = await _attempt_session_join(row, item.get("link", ""))
                    if status == "retry":
                        await db.retry_join_item(item["id"], detail)
                    else:
                        await db.finish_join_item(item["id"], status, detail)
                    log.info("join-queue owner=%s account=%s status=%s link=%s detail=%s", owner_uid, row.get("label"), status, item.get("normalized_link"), detail)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Join queue service error owner=%s", owner_uid)
            await asyncio.sleep(5)
    finally:
        JOIN_QUEUE_TASKS.pop(owner_uid, None)
        CURRENT_OPERATOR.reset(token_ctx)


async def start_join_queue_service(app: Application, owner_uid: int):
    task = JOIN_QUEUE_TASKS.get(owner_uid)
    if task and not task.done():
        return
    JOIN_QUEUE_TASKS[owner_uid] = asyncio.create_task(
        join_queue_service(app, owner_uid), name=f"join_queue_{owner_uid}"
    )


def authorized_handler(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        uid = update.effective_user.id
        if update.effective_chat.type != "private":
            # Sessions and account controls must not be exposed to group chats.
            return
        if uid not in APPROVED_USERS:
            return          # /start for non-approved users is answered by the subscription gate only
        if MAINTENANCE["on"] and uid != ADMIN_ID:
            if update.callback_query:
                await update.callback_query.answer("🛠 Bot is under maintenance.", show_alert=True)
            elif update.message:
                await update.message.reply_text("🛠 Bot is under maintenance. Please try later.")
            return
        token = CURRENT_OPERATOR.set(uid)
        try:
            return await handler(update, context)
        finally:
            CURRENT_OPERATOR.reset(token)
    return wrapped



def admin_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in APPROVED_USERS and update.effective_user.id == tenant_id())


def esc(s):
    return html.escape(str(s or ""))


async def posts_summary():
    posts = await db.list_posts()
    mode = await db.get_setting("post_mode", "rotate")
    if not posts:
        return "📚 <b>POST LIBRARY</b>\n\nNo posts saved. Select 📝 Save Post to add one."
    lines = [f"📚 <b>POST LIBRARY</b> ({len(posts)} total)",
             f"Sending mode: <b>{esc(mode.title())}</b>", ""]
    for post in posts:
        preview = (post.get("html_text") or "(media only)")
        preview = html.unescape(preview)
        preview = preview.replace("\n", " ")
        if len(preview) > 65:
            preview = preview[:62] + "..."
        status = "✅ ON" if post.get("enabled") else "⏸ OFF"
        lines.append(f"<b>#{post['id']}</b> {status} · {esc(post['kind'])} · {esc(preview)}")
    return "\n".join(lines)


async def preview_saved_post(message, post):
    label = f"👁 Preview #<b>{post['id']}</b> ({'enabled' if post.get('enabled') else 'disabled'})"
    await message.reply_text(label, parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
    media_path = post.get("media_path")
    caption = post.get("html_text") or None
    if media_path:
        if not Path(media_path).exists():
            await message.reply_text("⚠️ Saved media file is missing from this server.", reply_markup=POST_MENU)
            return
        with open(media_path, "rb") as fh:
            if post["kind"] == "photo":
                await message.reply_photo(fh, caption=caption, parse_mode=ParseMode.HTML)
            elif post["kind"] == "video":
                await message.reply_video(fh, caption=caption, parse_mode=ParseMode.HTML)
            elif post["kind"] == "animation":
                await message.reply_animation(fh, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await message.reply_document(fh, caption=caption, parse_mode=ParseMode.HTML)
    elif caption:
        await message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=POST_MENU)


def fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def schedule_text() -> str:
    """Ad schedule overview built from existing settings and live round state."""
    interval = await get_ad_interval_minutes()
    delay = await get_group_delay()
    mode = await db.get_setting("post_mode", "rotate")
    active_posts = len(await db.list_posts(enabled_only=True))
    accounts = len(await session_store.get_sessions(enabled_only=True))
    if stats.running:
        status = "🟢 Running"
        next_run = fmt_duration(stats.next_round_at - time.time()) if stats.next_round_at else "-"
        current = stats.current_post if stats.current_post not in ("", "-") else "-"
    else:
        status = "🔴 Stopped"
        next_run = "-"
        current = "-"
    return (
        "📅 <b>AD SCHEDULE</b>\n\n"
        f"Status: <b>{status}</b>\n"
        f"⏱ Interval: <b>{interval} min</b> (minimum {MIN_AD_INTERVAL_MINUTES})\n"
        f"⏳ Gap between groups: <b>{delay}s</b>\n"
        f"📝 Ads: <b>{active_posts}</b> enabled | Mode: <b>{esc(mode.title())}</b>\n"
        f"📮 Current ad: <b>{esc(current)}</b>\n"
        f"👤 Active accounts: <b>{accounts}</b>\n"
        f"👥 Groups in last round: <b>{stats.total_groups}</b> (each account posts to the groups it has joined)\n"
        f"🔁 Next round in: <b>{next_run}</b>\n\n"
        "Start/Stop: ▶️ Start Promo / ⏹ Stop Promo\n"
        "Change interval: ⏱ Interval"
    )


def stats_text() -> str:
    total_targets = stats.total_groups + stats.total_users
    pct = (stats.attempted / total_targets * 100) if total_targets else 0
    elapsed = fmt_duration(time.time() - stats.started_at) if stats.started_at else "-"
    next_run = "-"
    if stats.next_round_at:
        next_run = fmt_duration(stats.next_round_at - time.time())
    return (
        "📊 <b>LIVE PROMOTION STATS</b>\n\n"
        f"Status: {'🟢 Running' if stats.running else '🔴 Stopped'}\n"
        f"Round: <b>{stats.round_no}</b>\n"
        f"📝 Active posts: <b>{stats.active_posts}</b> | Mode: <b>{esc(stats.post_mode.title())}</b>\n"
        f"📮 Current post: <b>{esc(stats.current_post)}</b>\n"
        f"Overall: <b>{stats.attempted}/{total_targets}</b> ({pct:.1f}%)\n\n"
        "👥 <b>GROUPS</b>\n"
        f"Total: <b>{stats.total_groups}</b> | Tried: <b>{stats.group_attempted}</b>\n"
        f"✅ {stats.group_sent}  ❌ {stats.group_failed}  ⏭ {stats.group_skipped}\n\n"
        "👤 <b>OPT-IN USERS</b>\n"
        f"Total: <b>{stats.total_users}</b> | Tried: <b>{stats.user_attempted}</b>\n"
        f"✅ {stats.user_sent}  ❌ {stats.user_failed}  ⏭ {stats.user_skipped}\n\n"
        f"⏳ Flood waits: <b>{stats.flood_waits}</b> | Paused IDs: <b>{stats.paused_sessions}</b>\n"
        f"⏳ Minimum group gap: <b>{stats.group_delay_seconds}s</b>\n"
        "👥 Per-account status: tap 📊 Account Stats or 📜 Account Logs\n"
        f"⏱ Elapsed: {elapsed}\n"
        f"🔁 Next round in: {next_run}\n"
        f"⚠️ Last error: <code>{esc(stats.last_error)[:500]}</code>"
    )


def clean_session_string(raw) -> str:
    """Normalize pasted/stored Telethon StringSession text; raise ValueError with a clear message."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    value = "".join(str(raw or "").split())            # drop spaces/newlines from copy-paste
    value = value.strip("`'\"“”‘’")                      # drop quotes/backticks
    if value.lower().startswith("session="):
        value = value.split("=", 1)[1].strip("`'\"")
    if not value or value[0] != "1":
        raise ValueError("Invalid Telethon StringSession (it must start with '1'). Generate one with generate_session.py.")
    try:
        StringSession(value)                              # raises ValueError('Not a valid string')
    except ValueError:
        raise ValueError("Invalid or truncated Telethon StringSession. Copy the full string and send it again.") from None
    return value


async def make_client(session_string: str) -> TelegramClient:
    session_string = clean_session_string(session_string)
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=0)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session is not authorized")
    return client


async def _adopt_shared_client(row: dict, client: TelegramClient) -> TelegramClient:
    """Attach all runtime handlers to an already-connected authorized client.

    This is used after session validation/login so the validated connection itself
    becomes the one shared client. No second client is created for the same auth key.
    """
    sid = str(row["id"])
    tg_id = row.get("tg_user_id")
    account_key = str(tg_id if tg_id is not None else sid)
    _session_client_keys[sid] = account_key

    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session is not authorized")

    owner = tenant_id()
    handler = account_reply_handler(sid)

    async def scoped_dm(event):
        token = CURRENT_OPERATOR.set(owner)
        try:
            return await handler(event)
        finally:
            CURRENT_OPERATOR.reset(token)

    me = await client.get_me()
    my_id = me.id

    async def scoped_group_join(event):
        token = CURRENT_OPERATOR.set(owner)
        try:
            await handle_group_join_event(event, sid, my_id)
        finally:
            CURRENT_OPERATOR.reset(token)

    async def scoped_group_message(event):
        if not getattr(event, "is_group", False):
            return
        token = CURRENT_OPERATOR.set(owner)
        try:
            await remember_group_from_event(event, sid)
        finally:
            CURRENT_OPERATOR.reset(token)

    client.add_event_handler(scoped_dm, events.NewMessage(incoming=True))
    client.add_event_handler(scoped_group_join, events.ChatAction())
    client.add_event_handler(scoped_group_message, events.NewMessage(incoming=True))
    _account_dm_clients[account_key] = client
    log.info("Shared Telegram client ready for %s (%s)", row.get("label", sid), row.get("tg_user_id", account_key))
    return client


async def _register_validated_session(client: TelegramClient, me, session_string: str):
    """Atomically save and adopt a freshly validated/login client.

    The account lock is acquired *before* MongoDB marks the session active. This
    prevents the background listener from seeing the fresh row and creating a
    second Telethon client while the validation/login client is still connected.
    """
    account_key = str(me.id)
    lock = _account_client_locks.setdefault(account_key, asyncio.Lock())
    async with lock:
        # Remove any older pooled connection for this Telegram account without
        # calling close_account_client() (which would try to acquire this lock again).
        old = _account_dm_clients.pop(account_key, None)
        if old is not None and old is not client:
            try:
                await old.disconnect()
            except Exception:
                pass

        label = f"{me.first_name or 'Account'} ({me.id})"
        enc = fernet.encrypt(session_string.encode()).decode()
        stored_sid = await session_store.add_session(
            label, enc, me.id, me.username, me.first_name
        )
        rows = await session_store.get_sessions(enabled_only=False)
        stored_row = next((r for r in rows if str(r.get("id")) == str(stored_sid)), None)
        if stored_row is None:
            raise RuntimeError("Session was saved but could not be loaded from MongoDB")
        await subs_core.log_event(f"➕ Account added · operator <code>{tenant_id()}</code> · "
                                  f"{esc(label)} · tg <code>{me.id}</code>")

        # Still under the same account lock: background get_account_client() calls
        # for this account cannot create another client until adoption is complete.
        shared = await _adopt_shared_client(stored_row, client)
        return label, stored_sid, stored_row, shared


async def get_account_client(row: dict) -> TelegramClient:
    """Return exactly one long-lived Telethon client for this Telegram account."""
    sid = str(row["id"])
    tg_id = row.get("tg_user_id")
    account_key = str(tg_id if tg_id is not None else sid)
    _session_client_keys[sid] = account_key
    lock = _account_client_locks.setdefault(account_key, asyncio.Lock())
    async with lock:
        existing = _account_dm_clients.get(account_key)
        if existing and existing.is_connected():
            return existing
        if existing:
            _account_dm_clients.pop(account_key, None)
            try:
                await existing.disconnect()
            except Exception:
                pass
        try:
            client = await make_client(decrypt_session(row["session_enc"]))
            return await _adopt_shared_client(row, client)
        except errors.AuthKeyDuplicatedError:
            await session_store.mark_needs_relogin(
                sid,
                "AuthKeyDuplicatedError: create a fresh Telegram session for this account",
            )
            log.error(
                "Session requires re-login for %s (%s): AuthKeyDuplicatedError",
                row.get("label", sid),
                row.get("tg_user_id", "?"),
            )
            raise


async def close_account_client(sid_or_account_key: str):
    raw = str(sid_or_account_key)
    account_key = _session_client_keys.pop(raw, raw)
    lock = _account_client_locks.setdefault(account_key, asyncio.Lock())
    async with lock:
        client = _account_dm_clients.pop(account_key, None)
        if client:
            try:
                await client.disconnect()
            finally:
                for sid, key in list(_session_client_keys.items()):
                    if key == account_key:
                        _session_client_keys.pop(sid, None)


def decrypt_session(value: str) -> str:
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("FERNET_KEY does not match stored sessions") from e


async def _input_peer(entity):
    """Build a reusable InputPeer so later rounds do not need entity resolution."""
    if isinstance(entity, types.Chat):
        return types.InputPeerChat(entity.id)
    if isinstance(entity, types.Channel):
        return types.InputPeerChannel(entity.id, entity.access_hash or 0)
    if isinstance(entity, types.User):
        return types.InputPeerUser(entity.id, entity.access_hash or 0)
    return entity


def _group_peer_key(peer):
    """Stable identity for cached InputPeerChat and InputPeerChannel."""
    if isinstance(peer, types.InputPeerChat):
        return ("chat", int(peer.chat_id))
    if isinstance(peer, types.InputPeerChannel):
        return ("channel", int(peer.channel_id))
    return (type(peer).__name__, str(peer))


async def cache_joined_group(sid: str, entity, name: str) -> bool:
    """Upsert a group instantly; never add broadcast-only channels or DMs."""
    if not isinstance(entity, types.Chat) and not (
        isinstance(entity, types.Channel) and getattr(entity, "megagroup", False)
    ):
        return False
    if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
        return False
    peer = await _input_peer(entity)
    key = _group_peer_key(peer)
    lock = scan_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        cached = target_cache.get(sid)
        if cached is None:
            # A complete initial scan is still required to find older groups.
            # The new group will also be present in that scan or in the pending
            # event buffer if it arrives while initial scanning is ongoing.
            pending_groups.setdefault(sid, {})[key] = (peer, name)
            return True
        groups = cached["groups"]
        for i, (existing, _old_name) in enumerate(groups):
            if _group_peer_key(existing) == key:
                groups[i] = (peer, name)
                return False
        groups.append((peer, name))
        stats.cached_group_count[sid] = len(groups)
        log.info("Auto-detected group on session %s: %s", sid, name)
        return True


async def forget_left_group(sid: str, entity):
    """Remove the account's departed group so it is not sent to next round."""
    peer = await _input_peer(entity)
    key = _group_peer_key(peer)
    lock = scan_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        pending_groups.get(sid, {}).pop(key, None)
        cached = target_cache.get(sid)
        if cached is not None:
            groups = cached["groups"]
            groups[:] = [(p, n) for p, n in groups if _group_peer_key(p) != key]
            stats.cached_group_count[sid] = len(groups)


async def handle_group_join_event(event, sid: str, my_id: int):
    """A self join/add/removal event only changes its own session cache."""
    if not (getattr(event, "is_group", False) or getattr(event, "is_channel", False)):
        return
    user_ids = getattr(event, "user_ids", None) or []
    if getattr(event, "user_id", None) is not None:
        user_ids = list(user_ids) + [event.user_id]
    if my_id not in user_ids:
        return  # Someone else joined; do not add unrelated targets.
    try:
        entity = await event.get_chat()
        if getattr(event, "user_joined", False) or getattr(event, "user_added", False):
            await cache_joined_group(sid, entity, getattr(entity, "title", None) or str(event.chat_id))
        elif getattr(event, "user_left", False) or getattr(event, "user_kicked", False):
            await forget_left_group(sid, entity)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("Could not update joined group cache for %s: %s", sid, type(exc).__name__)


async def remember_group_from_event(event, sid: str):
    """Discover new groups from incoming group activity without dialog scans."""
    try:
        entity = await event.get_chat()
        if entity is not None:
            await cache_joined_group(sid, entity, getattr(entity, "title", None) or str(event.chat_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("Group message cache update failed on %s: %s", sid, type(exc).__name__)


async def scan_targets(client: TelegramClient):
    """Scan dialogs once and cache joined groups only for recurring promotion.

    Private dialogs are intentionally not part of the recurring scheduler. A separate
    one-time DM broadcast action can scan existing private dialogs when explicitly used.
    """
    groups = []
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        name = dialog.name or str(dialog.id)
        if isinstance(entity, types.Chat):
            groups.append((await _input_peer(entity), name))
            continue
        if isinstance(entity, types.Channel) and getattr(entity, "megagroup", False):
            groups.append((await _input_peer(entity), name))
    return groups, []


async def _has_prior_outgoing_dm(client: TelegramClient, entity, me) -> bool:
    """True only when this connected account has previously messaged the user.

    This deliberately excludes contact-only entries and incoming-only conversations,
    so the manual DM broadcast never opens a brand-new private conversation.
    """
    try:
        async for message in client.iter_messages(entity, from_user=me, limit=1):
            if message is not None and getattr(message, "out", True):
                return True
        return False
    except errors.FloodWaitError:
        raise
    except Exception as exc:
        log.debug("Prior-DM history check failed for %s: %s", getattr(entity, "id", "?"), type(exc).__name__)
        return False


async def scan_existing_dm_users(client: TelegramClient):
    """Return private users that this account has already messaged before.

    Contacts/non-contacts are treated the same. A user is eligible only if a private
    dialog exists and Telegram history contains at least one outgoing message from
    this connected account. Duplicate user IDs are removed.
    """
    users = []
    seen_ids = set()
    me = await client.get_me()
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        if not isinstance(entity, types.User):
            continue
        if entity.id == me.id or getattr(entity, "bot", False) or getattr(entity, "deleted", False):
            continue
        if entity.id in seen_ids:
            continue
        if not await _has_prior_outgoing_dm(client, entity, me):
            continue
        seen_ids.add(entity.id)
        display = " ".join(x for x in [getattr(entity, "first_name", None), getattr(entity, "last_name", None)] if x)
        users.append((await _input_peer(entity), display or getattr(entity, "username", None) or str(entity.id)))
    return users


async def get_cached_targets(row: dict, client: TelegramClient, force: bool = False):
    """Return cached targets, refreshing at most once per TTL unless force=True."""
    sid = str(row["id"])
    lock = scan_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        cached = target_cache.get(sid)
        now = time.time()
        if cached and not force and now - cached["scanned_at"] < TARGET_CACHE_TTL:
            return cached["groups"], cached["users"], False
        groups, users = await scan_targets(client)
        # Keep join events that arrived during the dialogs API call; prefer
        # live entities where they offer a fresher access_hash or title.
        merged = {_group_peer_key(peer): (peer, name) for peer, name in groups}
        merged.update(pending_groups.pop(sid, {}))
        # A full scan is authoritative for existing groups; live events are
        # serialized by scan_locks and pending new joins are merged above.
        groups = list(merged.values())
        target_cache[sid] = {"groups": groups, "users": users, "scanned_at": now}
        stats.cached_group_count[row["id"]] = len(groups)
        stats.cached_user_count[row["id"]] = len(users)
        return groups, users, True


async def get_ad_interval_minutes() -> int:
    """Effective ad interval: stored value, never below MIN_AD_INTERVAL_MINUTES."""
    try:
        stored = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES)))
    except (ValueError, TypeError):
        stored = DEFAULT_INTERVAL_MINUTES
    return max(MIN_AD_INTERVAL_MINUTES, stored)


async def get_group_delay() -> int:
    """Per-session minimum gap between group sends, editable from the admin menu."""
    try:
        return min(600, max(15, int(await db.get_setting("group_delay_seconds", str(GROUP_DELAY_SECONDS)))))
    except (ValueError, TypeError):
        return GROUP_DELAY_SECONDS


async def get_pause_remaining(session_id: str) -> int:
    """Persisted in settings so restarting the bot never clears a FloodWait."""
    try:
        paused_until = float(await db.get_setting(f"account_pause:{session_id}", "0"))
    except (ValueError, TypeError):
        return 0
    return max(0, int(paused_until - time.time() + 0.999))


async def pause_account(session_id: str, seconds: float, reason: str):
    seconds = max(1, int(seconds))
    key = f"account_pause:{session_id}"
    try:
        old_until = float(await db.get_setting(key, "0"))
    except (ValueError, TypeError):
        old_until = 0
    until = max(old_until, time.time() + seconds)
    await db.set_setting(key, str(until))
    await db.set_setting(f"account_pause_reason:{session_id}", reason)
    log.warning("Session %s paused for %ss: %s", session_id, max(1, int(until-time.time())), reason)


async def log_account_state(row, metric, state, detail=""):
    """Write PAUSED/RESUMED events without treating them as delivery attempts."""
    key = metric_key(row["id"], metric["kind"])
    line = (f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | "
            f"{state} | {row['label']}" + (f" | {detail}" if detail else ""))
    ACCOUNT_EVENTS.setdefault(key, deque(maxlen=30)).append(line)
    log.info("account=%s tenant=%s mode=%s %s", row["label"], tenant_id(), metric["kind"], line)
    try:
        await asyncio.to_thread(_append_account_log, account_log_path(key), line)
    except OSError:
        log.warning("Account log write failed for %s", row["label"])


async def wait_for_flood_release(row, metric):
    """Wait for Telegram's *persisted* FloodWait, without holding the pacing lock.

    Another session's promotion worker keeps running normally.  Read the stored
    expiry on each check: another workload may extend the same account's pause.
    Never auto-retry a target whose send got a FloodWait.
    """
    sid = str(row["id"])
    metric["status"] = "Paused (FloodWait)"
    await log_account_state(row, metric, "PAUSED", "Waiting for Telegram cooldown")
    while stats.running:
        remaining = await get_pause_remaining(sid)
        if remaining <= 0:
            metric["status"] = "Running"
            metric["target"] = "Resumed after Telegram cooldown"
            await db.set_setting(f"account_pause_reason:{sid}", "")
            await log_account_state(row, metric, "RESUMED", "Telegram cooldown expired")
            await edit_account_progress(row, metric, force=True)
            return True
        # Keep other accounts' future promotion rounds independent of a very
        # long pause. The scheduler will re-check the persisted deadline.
        if remaining > 600:
            metric["status"] = "Paused (scheduled recheck)"
            await log_account_state(row, metric, "PAUSED", "Long cooldown; scheduled automatic recheck")
            await edit_account_progress(row, metric, force=True)
            return False
        metric["target"] = f"FloodWait: {fmt_duration(remaining)} remaining"
        await edit_account_progress(row, metric)
        await asyncio.sleep(min(10, remaining))
    return False


class AccountPausedError(Exception):
    def __init__(self, remaining: int):
        self.remaining = int(remaining)
        super().__init__(f"Account cooling down ({self.remaining}s left)")


async def guarded_account_send(session_id: str, operation):
    """Re-check account restrictions at execution time, after any queue wait."""
    remaining = await get_pause_remaining(session_id)
    if remaining:
        raise AccountPausedError(remaining)
    return await operation()



NETWORK_RETRY_DELAYS = (2, 5, 10, 20)


async def run_with_connection_retry(row: dict, operation_factory, purpose: str):
    """Retry only temporary transport failures for one Telegram account.

    operation_factory receives the currently shared client and must return an awaitable.
    Auth/Flood/PeerFlood errors intentionally escape immediately.
    """
    sid = str(row["id"])
    last_exc = None
    for attempt in range(len(NETWORK_RETRY_DELAYS) + 1):
        client = await get_account_client(row)
        try:
            return await operation_factory(client)
        except (errors.AuthKeyDuplicatedError, errors.AuthKeyUnregisteredError,
                errors.FloodWaitError, errors.PeerFloodError):
            raise
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt >= len(NETWORK_RETRY_DELAYS):
                break
            delay = NETWORK_RETRY_DELAYS[attempt]
            log.warning(
                "Temporary connection error for %s (%s) during %s: %s; retrying in %ss",
                row.get("label", sid), row.get("tg_user_id", "?"), purpose,
                type(exc).__name__, delay,
            )
            await close_account_client(sid)
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise ConnectionError(f"{purpose} failed after connection retries")


TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.S)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def saved_html_to_telegram(html_text: str):
    """Saved post HTML -> (plain text, entities) for Telethon.

    Premium emoji (<tg-emoji>) become custom-emoji entities with their original IDs, so they
    go out exactly as the admin saved them. Other formatting uses Telethon's HTML parser.
    """
    from telethon.extensions import html as tl_html

    parts, entities, offset, pos = [], [], 0, 0

    def add_segment(segment: str):
        nonlocal offset
        if not segment:
            return
        seg_text, seg_entities = tl_html.parse(segment)
        for ent in seg_entities or []:
            ent.offset += offset
        entities.extend(seg_entities or [])
        parts.append(seg_text)
        offset += _utf16_len(seg_text)

    for match in TG_EMOJI_RE.finditer(html_text or ""):
        add_segment(html_text[pos:match.start()])
        fallback = html.unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        if fallback:
            entities.append(types.MessageEntityCustomEmoji(
                offset=offset, length=_utf16_len(fallback), document_id=int(match.group(1))))
            parts.append(fallback)
            offset += _utf16_len(fallback)
        pos = match.end()
    add_segment((html_text or "")[pos:])
    return "".join(parts), entities


async def send_saved_post(client: TelegramClient, entity, post: dict):
    media_path = post.get("media_path")
    text, entities = saved_html_to_telegram(post.get("html_text") or "")
    if media_path:
        if not Path(media_path).exists():
            raise FileNotFoundError(f"Saved media missing: {media_path}")
        await client.send_file(
            entity,
            media_path,
            caption=text or None,
            formatting_entities=entities or None,
        )
    else:
        await client.send_message(entity, text, formatting_entities=entities or None, link_preview=False)


def pick_post(posts: list[dict], mode: str, index: int, round_number: int = 1, previous_id=None):
    if not posts:
        raise RuntimeError("No active promotion posts")
    if mode == "random":
        choices = [post for post in posts if post["id"] != previous_id] if len(posts) > 1 else posts
        return random.choice(choices or posts)
    return posts[(index + round_number - 1) % len(posts)]


async def _run_session_round(row, client, groups, users, posts, mode, progress_msg, edit_lock, metric):
    """Independent account worker; sequential, paced group sends and no immediate flood retries."""
    targets = [("group", e, n) for e, n in groups] + [("user", e, n) for e, n in users]
    sid = str(row["id"])
    delay = stats.group_delay_seconds
    metric["status"] = "Running"
    try:
        for target_index, (target_type, entity, name) in enumerate(targets):
            if not stats.running:
                return
            if quiet_remaining_seconds(await quiet_window()):
                return                      # quiet hours started; the scheduler resumes later
            stats.current_session = row["label"]
            stats.current_group = f"{'👥' if target_type == 'group' else '👤'} {name}"
            target_key = (sid, str(entity))
            post = pick_post(posts, mode, target_index, stats.round_no,
                             last_random_post_by_target.get(target_key))
            if mode == "random":
                last_random_post_by_target[target_key] = post["id"]
            stats.current_post = f"#{post['id']}"
            stop_this_account = False
            resume_after_flood = False
            outcome, error = "sent", ""
            metric["target"] = f"{target_type}: {name}"
            try:
                # A single pacing lock is shared with this account's incoming DM auto-reply.
                await pacer.run(
                    sid, delay if target_type == "group" else DM_BROADCAST_DELAY_SECONDS,
                    lambda: guarded_account_send(
                        sid,
                        lambda: run_with_connection_retry(
                            row,
                            lambda active_client: send_saved_post(active_client, entity, post),
                            f"promotion to {name}",
                        ),
                    ),
                    # A dead/unwritable/invalid target should not burn the normal
                    # group pacing delay. FloodWait/PeerFlood are handled below
                    # with account-level cooldowns before the next target.
                    gap_on_error=(target_type != "group"),
                )
                stats.sent += 1
                if target_type == "group":
                    stats.group_sent += 1
                else:
                    stats.user_sent += 1
            except AccountPausedError as e:
                outcome, error = "skipped", f"Account paused {e.remaining}s"
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.paused_sessions += 1
                stats.last_error = f"{row['label']}: account cooling down ({e.remaining}s left)"
                stop_this_account = True
            except errors.SlowModeWaitError as e:
                outcome, error = "skipped", f"Chat slow mode {e.seconds}s"
                # Slow mode is a chat-level restriction; skip this chat for this round.
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.last_error = f"{row['label']}: {name} has slow mode ({e.seconds}s)"
            except errors.FloodWaitError as e:
                outcome, error = "skipped", f"FloodWait {e.seconds}s"
                metric["flood_waits"] += 1
                # Do not immediately retry the same target after FloodWait. Respect
                # Telegram's stated cooldown, persist it, and stop this account's round.
                stats.flood_waits += 1
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                wait = int(e.seconds) + delay
                await pause_account(sid, wait, f"Telegram FloodWait ({e.seconds}s)")
                stats.last_error = f"{row['label']}: FloodWait {e.seconds}s; account paused"
                stats.paused_sessions += 1
                # The current target is skipped; resume the NEXT target once
                # Telegram's stated FloodWait expires, even in this same round.
                resume_after_flood = True
            except errors.PeerFloodError:
                outcome, error = "skipped", "Account restricted (PeerFlood)"
                # PeerFlood indicates an account-level anti-spam restriction.
                # Delays cannot lift that restriction; pause this account.
                await pause_account(sid, RESTRICTION_PAUSE_HOURS * 3600,
                                    "Telegram account restriction (PeerFlood); review account status")
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.last_error = f"{row['label']}: account restricted; sending paused"
                stats.paused_sessions += 1
                stop_this_account = True
            except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError,
                    errors.ChatAdminRequiredError):
                outcome, error = "skipped", "Writing not permitted"
                stats.skipped += 1
                if target_type == "group":
                    stats.group_skipped += 1
                else:
                    stats.user_skipped += 1
                stats.last_error = f"{row['label']}: write not allowed: {name}"
            except Exception as e:
                outcome, error = "failed", type(e).__name__
                stats.failed += 1
                if target_type == "group":
                    stats.group_failed += 1
                else:
                    stats.user_failed += 1
                stats.last_error = f"{row['label']}: {type(e).__name__}: {e}"
            finally:
                await record_account_event(row, metric, target_type, name, outcome, error)
                stats.attempted += 1
                if target_type == "group":
                    stats.group_attempted += 1
                else:
                    stats.user_attempted += 1

            await edit_account_progress(row, metric, force=stop_this_account or resume_after_flood)
            if stop_this_account:
                break
            if resume_after_flood:
                if not await wait_for_flood_release(row, metric):
                    return
            # No explicit sleep here: AccountPacer enforces the minimum gap
            # between completed sends, including interleaved DM auto-replies.
    finally:
        metric["status"] = "Stopped" if not stats.running else "Finished"
        metric["target"] = "-"
        try:
            await edit_account_progress(row, metric, force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Final account progress update failed for %s: %s", row.get("label", sid), type(exc).__name__)


async def do_round(app: Application):
    all_sessions = await session_store.get_sessions(enabled_only=True)
    chosen = set(await promo_selected_ids([str(r["id"]) for r in all_sessions]))
    sessions = [r for r in all_sessions if str(r["id"]) in chosen]
    posts = await db.list_posts(enabled_only=True)
    mode = await db.get_setting("post_mode", "rotate")
    if mode not in {"rotate", "random"}:
        mode = "rotate"
    if not all_sessions:
        raise RuntimeError("No active sessions")
    if not sessions:
        raise RuntimeError("No promotion accounts selected (📣 Promotion → 🎯 Promo Accounts)")
    if not posts:
        raise RuntimeError("No enabled promotion posts")

    stats.post_mode = mode
    stats.active_posts = len(posts)
    stats.round_no += 1
    stats.started_at = time.time()
    stats.group_attempted = stats.group_sent = stats.group_failed = stats.group_skipped = 0
    stats.user_attempted = stats.user_sent = stats.user_failed = stats.user_skipped = 0
    stats.attempted = stats.sent = stats.failed = stats.skipped = stats.flood_waits = 0
    stats.total_groups = 0
    stats.total_users = 0
    stats.last_error = "-"
    stats.paused_sessions = 0
    stats.group_delay_seconds = await get_group_delay()

    prepared = []
    initially_paused = []
    # Connect sessions concurrently. A target scan occurs only when cache is missing/stale.
    async def prepare(row):
        client = None
        try:
            remaining = await get_pause_remaining(str(row["id"]))
            if remaining:
                return row, None, [], [], ("paused", remaining)
            client = await get_account_client(row)
            groups, users, _ = await get_cached_targets(row, client, force=False)
            return row, client, groups, users, None
        except errors.FloodWaitError as e:
            await pause_account(str(row["id"]), e.seconds + stats.group_delay_seconds,
                                f"Telegram FloodWait while scanning/connecting ({e.seconds}s)")
            return row, None, [], [], ("paused", int(e.seconds))
        except Exception as e:
            return row, None, [], [], e

    results = await asyncio.gather(*(prepare(row) for row in sessions))
    for row, client, groups, users, err in results:
        if isinstance(err, tuple) and err[0] == "paused":
            stats.paused_sessions += 1
            stats.last_error = f"{row['label']}: account cooling down ({fmt_duration(err[1])} left)"
            # This account gets its own delayed worker; the other sessions
            # can send immediately instead of waiting for this cooldown.
            reason = await db.get_setting(f"account_pause_reason:{row['id']}", "")
            if err[1] <= 600 and "PeerFlood" not in reason:
                initially_paused.append(row)
            continue
        if err:
            stats.failed += 1
            stats.last_error = f"{row['label']}: {type(err).__name__}: {err}"
            continue
        prepared.append((row, client, groups, users))
        stats.total_groups += len(groups)
        stats.total_users += len(users)

    await dash_ensure(app.bot)

    edit_lock = asyncio.Lock()
    metrics = {}
    for row, client, groups, users in prepared:
        metric = begin_metric(row, 'promo', len(groups)+len(users), len(groups), len(users))
        metrics[str(row['id'])] = metric
        pass
    async def resume_prepaused(row):
        """Reconnect only this account when a pre-existing FloodWait expires."""
        sid = str(row["id"])
        metric = begin_metric(row, 'promo')
        metrics[sid] = metric
        pass
        if not await wait_for_flood_release(row, metric):
            return
        try:
            # An independent account may encounter a new Telegram cooldown
            # while reconnecting. Persist it and let the scheduler recheck.
            client = await get_account_client(row)
            groups, users, _ = await get_cached_targets(row, client, force=False)
        except errors.FloodWaitError as e:
            await pause_account(sid, e.seconds + stats.group_delay_seconds,
                                f"Telegram FloodWait while resuming ({e.seconds}s)")
            metric["status"] = "Paused (new FloodWait)"
            await log_account_state(row, metric, "PAUSED", f"New FloodWait: {e.seconds}s")
            await edit_account_progress(row, metric, force=True)
            return
        except Exception as e:
            metric["status"] = "Connection failed"
            metric["last_error"] = f"{type(e).__name__}: {e}"
            await edit_account_progress(row, metric, force=True)
            return
        metric["total"] = len(groups) + len(users)
        metric["groups"] = len(groups)
        metric["users"] = len(users)
        stats.total_groups += len(groups)
        stats.total_users += len(users)
        await _run_session_round(row, client, groups, users, posts, mode, None, edit_lock, metric)

    # Normal and cooling-down accounts have independent concurrent workers.
    # One account finishing or crashing must never stop the other accounts.
    async def isolated_worker(row, client, groups, users):
        sid = str(row["id"])
        metric = metrics[sid]
        try:
            await _run_session_round(
                row, client, groups, users, posts, mode, None, edit_lock, metric
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metric["status"] = "Worker error"
            metric["last_error"] = f"{type(exc).__name__}: {exc}"
            stats.failed += 1
            stats.last_error = f"{row['label']}: worker {type(exc).__name__}: {exc}"
            await log_account_state(row, metric, "WORKER_ERROR", metric["last_error"])
            try:
                await edit_account_progress(row, metric, force=True)
            except Exception:
                pass

    async def isolated_resume(row):
        try:
            await resume_prepaused(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sid = str(row["id"])
            metric = metrics.get(sid) or begin_metric(row, 'promo')
            metrics[sid] = metric
            metric["status"] = "Resume error"
            metric["last_error"] = f"{type(exc).__name__}: {exc}"
            stats.failed += 1
            stats.last_error = f"{row['label']}: resume {type(exc).__name__}: {exc}"
            await log_account_state(row, metric, "RESUME_ERROR", metric["last_error"])
            try:
                await edit_account_progress(row, metric, force=True)
            except Exception:
                pass

    worker_results = await asyncio.gather(
        *(isolated_worker(row, client, groups, users)
          for row, client, groups, users in prepared),
        *(isolated_resume(row) for row in initially_paused),
        return_exceptions=True,
    )
    # CancelledError should still stop the scheduler; ordinary account errors are
    # already contained and logged by the wrappers above.
    for result in worker_results:
        if isinstance(result, asyncio.CancelledError):
            raise result

    await dash_finish()


async def run_one_time_dm_broadcast(app: Application):
    """Manual one-time DM send only to users this account has messaged before."""
    sessions = await session_store.get_sessions(enabled_only=True)
    posts = await db.list_posts(enabled_only=True)
    mode = await db.get_setting("post_mode", "rotate")
    if mode not in {"rotate", "random"}:
        mode = "rotate"
    if not sessions:
        raise RuntimeError("No active sessions")
    if not posts:
        raise RuntimeError("No enabled promotion posts")
    stats.post_mode = mode
    stats.active_posts = len(posts)

    stats.started_at = time.time()
    stats.current_group = "-"
    stats.group_attempted = stats.group_sent = stats.group_failed = stats.group_skipped = 0
    stats.user_attempted = stats.user_sent = stats.user_failed = stats.user_skipped = 0
    stats.attempted = stats.sent = stats.failed = stats.skipped = stats.flood_waits = 0
    stats.total_groups = 0
    stats.total_users = 0
    stats.last_error = "-"
    stats.paused_sessions = 0
    stats.group_delay_seconds = await get_group_delay()

    prepared = []
    async def prepare_dm(row):
        client = None
        try:
            remaining = await get_pause_remaining(str(row["id"]))
            if remaining:
                return row, None, [], ("paused", remaining)
            client = await get_account_client(row)
            users = await scan_existing_dm_users(client)
            return row, client, users, None
        except errors.FloodWaitError as e:
            await pause_account(str(row["id"]), e.seconds + DM_BROADCAST_DELAY_SECONDS,
                                f"Telegram FloodWait while scanning DMs ({e.seconds}s)")
            return row, None, [], ("paused", int(e.seconds))
        except Exception as e:
            return row, None, [], e

    results = await asyncio.gather(*(prepare_dm(row) for row in sessions))
    for row, client, users, err in results:
        if isinstance(err, tuple) and err[0] == "paused":
            stats.paused_sessions += 1
            stats.last_error = f"{row['label']}: account cooling down ({fmt_duration(err[1])} left)"
            continue
        if err:
            stats.failed += 1
            stats.last_error = f"{row['label']}: {type(err).__name__}: {err}"
            continue
        prepared.append((row, client, users))
        stats.total_users += len(users)

    await dash_ensure(app.bot)

    edit_lock = asyncio.Lock()
    metrics = {}
    for row, client, users in prepared:
        metric = begin_metric(row, 'dm', len(users), users=len(users))
        metrics[str(row['id'])] = metric
        pass

    async def worker(row, client, users):
        sid = str(row["id"])
        metric = metrics[sid]
        metric["status"] = "Running"
        try:
            remaining = await get_pause_remaining(sid)
            if remaining:
                stats.paused_sessions += 1
                stats.last_error = f"{row['label']}: paused for {fmt_duration(remaining)}"
                return
            for target_index, (entity, name) in enumerate(users):
                stats.current_session = row["label"]
                stats.current_group = f"👤 {name}"
                post = pick_post(posts, mode, target_index, stats.round_no)
                stats.current_post = f"#{post['id']}"
                stop_this_account = False
                outcome, error = "sent", ""
                metric["target"] = f"DM: {name}"
                try:
                    await pacer.run(sid, DM_BROADCAST_DELAY_SECONDS,
                                    lambda: guarded_account_send(
                                        sid,
                                        lambda: run_with_connection_retry(
                                            row,
                                            lambda active_client: send_saved_post(active_client, entity, post),
                                            f"manual DM to {name}",
                                        ),
                                    ))
                    stats.sent += 1
                    stats.user_sent += 1
                except AccountPausedError as e:
                    outcome, error = "skipped", f"Account paused {e.remaining}s"
                    stats.skipped += 1
                    stats.user_skipped += 1
                    stats.paused_sessions += 1
                    stats.last_error = f"{row['label']}: account cooling down ({e.remaining}s left)"
                    stop_this_account = True
                except errors.FloodWaitError as e:
                    outcome, error = "skipped", f"FloodWait {e.seconds}s"
                    metric["flood_waits"] += 1
                    stats.flood_waits += 1
                    stats.skipped += 1
                    stats.user_skipped += 1
                    await pause_account(sid, e.seconds + DM_BROADCAST_DELAY_SECONDS,
                                        f"Telegram FloodWait during manual DM ({e.seconds}s)")
                    stats.last_error = f"{row['label']}: FloodWait {e.seconds}s; account paused"
                    stats.paused_sessions += 1
                    stop_this_account = True
                except errors.PeerFloodError:
                    outcome, error = "skipped", "Account restricted (PeerFlood)"
                    await pause_account(sid, RESTRICTION_PAUSE_HOURS * 3600,
                                        "Telegram account restriction (PeerFlood)")
                    stats.skipped += 1
                    stats.user_skipped += 1
                    stats.paused_sessions += 1
                    stats.last_error = f"{row['label']}: account restricted; manual DM stopped"
                    stop_this_account = True
                except Exception as e:
                    outcome, error = "failed", type(e).__name__
                    stats.failed += 1
                    stats.user_failed += 1
                    stats.last_error = f"{row['label']}: {type(e).__name__}: {e}"
                finally:
                    await record_account_event(row, metric, "user", name, outcome, error)
                    stats.attempted += 1
                    stats.user_attempted += 1
                await edit_account_progress(row, metric, force=stop_this_account)
                if stop_this_account:
                    break
        finally:
            metric["status"] = "Finished"
            metric["target"] = "-"
            await edit_account_progress(row, metric, force=True)

    await asyncio.gather(*(worker(row, client, users) for row, client, users in prepared))
    stats.current_group = "-"
    stats.current_session = "-"
    await dash_finish()



async def dm_broadcast_runner(app: Application):
    try:
        await run_one_time_dm_broadcast(app)
    except Exception as exc:
        log.warning("Manual DM broadcast failed: %s", type(exc).__name__)
        try:
            await app.bot.send_message(tenant_id(), "⚠️ Manual DM broadcast stopped because of an error.")
        except Exception:
            pass
    finally:
        set_broadcast_running(app, False)


# ---- Quiet hours: a daily window (per operator) when promotions pause. Set from the Promotion menu.
QUIET_CACHE: dict[int, tuple[float, tuple | None]] = {}
DISPLAY_TZ_OFFSET = int(os.getenv("DISPLAY_TZ_OFFSET_MINUTES", "330"))


def parse_quiet_window(raw: str) -> tuple[int, int]:
    """'HH:MM-HH:MM' -> (start_minute, end_minute). Windows may cross midnight."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", raw or "")
    if not m:
        raise ValueError("format")
    a = int(m[1]) * 60 + int(m[2])
    b = int(m[3]) * 60 + int(m[4])
    if a >= 1440 or b >= 1440 or a == b or int(m[2]) > 59 or int(m[4]) > 59:
        raise ValueError("range")
    return a, b


async def quiet_window():
    uid = tenant_id()
    hit = QUIET_CACHE.get(uid)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    raw = (await db.get_setting("quiet_hours", "")) or ""
    try:
        win = parse_quiet_window(raw) if raw else None
    except ValueError:
        win = None
    QUIET_CACHE[uid] = (time.monotonic(), win)
    return win


def quiet_remaining_seconds(win) -> float:
    """Seconds until the quiet window ends, or 0 when promotions may run now."""
    if not win:
        return 0.0
    now = datetime.now(timezone(timedelta(minutes=DISPLAY_TZ_OFFSET)))
    mins = now.hour * 60 + now.minute
    a, b = win
    inside = (a <= mins < b) if a < b else (mins >= a or mins < b)
    if not inside:
        return 0.0
    return ((b - mins) % 1440) * 60 - now.second


async def scheduler(app: Application):
    stats.running = True
    try:
        while stats.running:
            wait = quiet_remaining_seconds(await quiet_window())
            if wait:
                stats.current_group = "🌙 Quiet hours"
                stats.next_round_at = time.time() + wait
                while stats.running and time.time() < stats.next_round_at:
                    await asyncio.sleep(min(30, max(0.2, stats.next_round_at - time.time())))
                continue
            try:
                await do_round(app)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                stats.last_error = f"{type(e).__name__}: {e}"
                try:
                    await app.bot.send_message(tenant_id(), f"⚠️ Promotion round error:\n<code>{esc(stats.last_error)}</code>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

            if not stats.running:
                break
            interval = await get_ad_interval_minutes()
            stats.next_round_at = time.time() + interval * 60
            # A session that was already paused when the round started should
            # not wait an extra full promotion interval after its FloodWait ends.
            for session in await session_store.get_sessions(enabled_only=True):
                sid = str(session["id"])
                remaining = await get_pause_remaining(sid)
                if remaining and "PeerFlood" not in (await db.get_setting(
                        f"account_pause_reason:{sid}", "")):
                    stats.next_round_at = min(stats.next_round_at, time.time() + remaining)
            while stats.running and time.time() < stats.next_round_at:
                await asyncio.sleep(min(5, max(0.2, stats.next_round_at - time.time())))
    finally:
        stats.running = False
        stats.current_group = "-"
        stats.current_session = "-"
        stats.next_round_at = None


async def bot_dm_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages sent directly to the *admin control bot*."""
    if not await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED) or not update.message or not update.effective_user:
        return
    if update.effective_user.id in APPROVED_USERS or update.effective_user.is_bot:
        return
    if update.effective_chat.type != "private":
        return
    now = time.monotonic()
    uid = update.effective_user.id
    if now - _bot_reply_timestamps.get(uid, -1e20) < await reply_cooldown():
        return
    # Prevent duplicate replies to consecutive incoming media/text messages.
    _bot_reply_timestamps[uid] = now
    try:
        message = await reply_setting("control_bot_dm_text", DM_AUTO_REPLY_TEXT)
        await update.message.reply_text(message, disable_web_page_preview=True)
        _reply_sent_counts["control_bot"] += 1
    except Exception as e:
        _reply_sent_counts["failed"] += 1
        _bot_reply_timestamps.pop(uid, None)
        log.warning("Control bot auto-reply failed: %s", type(e).__name__)


def account_reply_handler(session_key: str):
    """Incoming private DM handler, active even during long broadcast rounds."""
    async def on_dm(event):
        if not event.is_private or event.out:
            return
        if not await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED):
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return
        key = (session_key, int(event.sender_id))
        now = time.monotonic()
        if key in _account_reply_pending:
            return
        if now - _account_reply_timestamps.get(key, -1e20) < await reply_cooldown():
            return
        if await get_pause_remaining(session_key):
            # A Telegram FloodWait/PeerFlood applies to replies as well.
            return
        _account_reply_pending.add(key)
        try:
            if await reply_enabled("ai_reply_enabled", False):
                ai_text = await ai_reply_for(session_key, event)
                if ai_text:
                    await pacer.run(
                        session_key, DM_REPLY_SEND_GAP_SECONDS,
                        lambda: guarded_account_send(session_key, lambda: event.reply(ai_text, link_preview=False)),
                        priority=True,
                    )
                    _account_reply_timestamps[key] = time.monotonic()
                    _reply_sent_counts["accounts"] += 1
                    return
            if await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST):
                mode = await db.get_setting("post_mode", "rotate")
                post = await db.next_reply_post(mode)
                if post:
                    await pacer.run(
                        session_key, DM_REPLY_SEND_GAP_SECONDS,
                        lambda: guarded_account_send(session_key, lambda: send_saved_post(event.client, event.input_sender or sender, post)),
                        priority=True,
                    )
                    _account_reply_timestamps[key] = time.monotonic()
                    _reply_sent_counts["accounts"] += 1
                    return
            message = await reply_setting("account_dm_text", ACCOUNT_DM_AUTO_REPLY_TEXT)
            await pacer.run(
                session_key, DM_REPLY_SEND_GAP_SECONDS,
                lambda: guarded_account_send(session_key, lambda: event.reply(message, link_preview=False)),
                priority=True,
            )
            # A failed/delayed reply must not start a 5-minute cooldown.
            _account_reply_timestamps[key] = time.monotonic()
            _reply_sent_counts["accounts"] += 1
        except AccountPausedError:
            # Pause may begin while this reply waits behind a group message.
            return
        except errors.PeerFloodError:
            await pause_account(session_key, RESTRICTION_PAUSE_HOURS * 3600,
                                "Telegram account restriction (PeerFlood) during DM auto-reply")
        except errors.FloodWaitError as e:
            await pause_account(session_key, e.seconds + GROUP_DELAY_SECONDS,
                                f"Telegram FloodWait during DM auto-reply ({e.seconds}s)")
            log.warning("DM auto-reply paused for session %s: FloodWait %ss", session_key, e.seconds)
        except Exception as e:
            _reply_sent_counts["failed"] += 1
            log.warning("Account DM auto-reply failed session=%s: %s", session_key, type(e).__name__)
        finally:
            _account_reply_pending.discard(key)
    return on_dm


async def account_auto_reply_service(app: Application):
    """Keep one Telegram connection per enabled account, shared by all workloads."""
    my_session_ids = set()
    last_sync: dict[str, float] = {}
    listener_retry_after: dict[str, float] = {}
    listener_retry_step: dict[str, int] = {}
    try:
        while True:
            try:
                rows = await session_store.get_sessions(enabled_only=True)
                enabled = {str(row["id"]): row for row in rows}
                my_session_ids.update(enabled)
                for sid in list(my_session_ids):
                    if sid not in enabled:
                        # Do not interrupt an active broadcast; revoke afterward.
                        if not stats.running and not broadcast_running(app):
                            await close_account_client(sid)
                            my_session_ids.discard(sid)
                            last_sync.pop(sid, None)
                for sid, row in enabled.items():
                    try:
                        now_loop = time.monotonic()
                        if now_loop < listener_retry_after.get(sid, 0):
                            continue
                        client = await get_account_client(row)
                        listener_retry_step[sid] = 0
                        listener_retry_after.pop(sid, None)
                        # Self-join events usually arrive immediately. This
                        # periodic catch-up handles missed updates/reconnects
                        # without requiring the Rescan Targets menu button.
                        now = time.monotonic()
                        if now - last_sync.get(sid, 0) >= AUTO_GROUP_SYNC_SECONDS:
                            first_sync = sid not in last_sync
                            last_sync[sid] = now
                            await get_cached_targets(row, client, force=not first_sync)
                    except asyncio.CancelledError:
                        raise
                    except errors.AuthKeyDuplicatedError:
                        my_session_ids.discard(sid)
                        last_sync.pop(sid, None)
                        log.error(
                            "Account disabled until fresh login: %s (%s)",
                            row.get("label", sid),
                            row.get("tg_user_id", "?"),
                        )
                    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                        step = min(listener_retry_step.get(sid, 0), len(NETWORK_RETRY_DELAYS) - 1)
                        delay = NETWORK_RETRY_DELAYS[step]
                        listener_retry_step[sid] = min(step + 1, len(NETWORK_RETRY_DELAYS) - 1)
                        listener_retry_after[sid] = time.monotonic() + delay
                        await close_account_client(sid)
                        log.warning(
                            "Account listener temporary connection error for %s: %s; retry in %ss",
                            row.get("label", sid), type(e).__name__, delay,
                        )
                    except Exception as e:
                        if "FERNET_KEY" in str(e):
                            # Stored session was encrypted with a different key: retrying cannot fix it.
                            if sid not in KEY_MISMATCH_LOGGED:
                                KEY_MISMATCH_LOGGED.add(sid)
                                log.error("Account %s: stored session cannot be decrypted with the current "
                                          "FERNET_KEY. Restore the old key, or remove and add this account again.",
                                          row.get("label", sid))
                            listener_retry_after[sid] = time.monotonic() + 3600
                        else:
                            log.warning("Account listener unavailable for %s: %s",
                                        row.get("label", sid), type(e).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Account listener manager error: %s", type(e).__name__)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise
    finally:
        # This manager is the sole owner of persistent account connections.
        await asyncio.gather(*(close_account_client(sid) for sid in list(my_session_ids)),
                             return_exceptions=True)



async def add_operator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID or update.effective_chat.type != "private":
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        return await update.message.reply_text("Usage: /add <Telegram user ID>")
    uid = int(context.args[0])
    if uid == ADMIN_ID:
        return await update.message.reply_text("You already have owner access.")
    if uid in APPROVED_USERS:
        return await update.message.reply_text(f"✅ {uid} already has access.")
    try:
        await initialize_tenant(uid)
        await asyncio.to_thread(lambda: operator_access.update_one(
            {"_id": uid}, {"$set": {"approved_by": ADMIN_ID, "approved_at": time.time()}}, upsert=True))
        APPROVED_USERS.add(uid)
        await start_tenant_listener(context.application, uid)
        await start_join_queue_service(context.application, uid)
        await sync_bot_account_listeners(context.application, uid)
        await subs_core.log_event(f"👤 Operator added · <code>{uid}</code> · by owner")
        await update.message.reply_text(f"✅ Added {uid}. Their sessions, bot accounts, posts, media and jobs are separate.\n\nThey can now open /start.")
        try:
            await context.bot.send_message(uid, "✅ The owner has granted you access. Open /start to begin.")
        except Exception:
            pass  # Operator may not have started the bot yet.
    except Exception:
        log.exception("Failed to approve operator %s", uid)
        await update.message.reply_text("❌ Unable to add user. Check the server logs.")

async def remove_operator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID or update.effective_chat.type != "private":
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /rem <Telegram user ID>")
    uid = int(context.args[0])
    if uid == ADMIN_ID:
        return await update.message.reply_text("The owner cannot be removed.")
    if uid not in APPROVED_USERS:
        return await update.message.reply_text("That user is not approved.")
    await subs_core.log_event(f"🚫 Operator removed · <code>{uid}</code> · by owner")
    APPROVED_USERS.discard(uid)  # Reject new actions before stopping existing jobs.
    if CO_ADMINS.pop(uid, None) is not None:
        await save_co_admins()
    await asyncio.to_thread(operator_access.delete_one, {"_id": uid})
    st = tenant_stats(uid)
    st.running = False
    tasks = [PROMO_TASKS.pop(uid, None), LISTENER_TASKS.pop(uid, None), JOIN_QUEUE_TASKS.pop(uid, None)]
    tasks.extend([task for (owner_uid, _), task in list(BOT_ACCOUNT_TASKS.items()) if owner_uid == uid])
    for key in [key for key in list(BOT_ACCOUNT_TASKS) if key[0] == uid]:
        BOT_ACCOUNT_TASKS.pop(key, None)
    tasks.append(context.application.bot_data.pop(f"dm_broadcast_task:{uid}", None))
    context.application.bot_data[f"dm_broadcast_running:{uid}"] = False
    for task in tasks:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
    token = CURRENT_OPERATOR.set(uid)
    try:
        # Disable stored accounts until explicit reapproval, preserving encrypted
        # sessions for the operator in case the owner approves them again.
        rows = await tenant_sessions(uid).get_sessions()
        await asyncio.gather(*(close_account_client(str(row["id"])) for row in rows),
                             return_exceptions=True)
    finally:
        CURRENT_OPERATOR.reset(token)
    await update.message.reply_text(f"🚫 Removed {uid}. Their sessions/data remain isolated and cannot be used without access.")


# ---------------- NEW: subscription hooks (same steps as /add and /rem; the commands above are unchanged) ----------------
async def sub_grant_access(app: Application, uid: int) -> bool:
    """Approve `uid` exactly like /add. Returns True if the user was already approved."""
    if uid in APPROVED_USERS:
        return True
    await initialize_tenant(uid)
    await asyncio.to_thread(lambda: operator_access.update_one(
        {"_id": uid}, {"$set": {"approved_by": ADMIN_ID, "approved_at": time.time(), "source": "subscription"}}, upsert=True))
    APPROVED_USERS.add(uid)
    await start_tenant_listener(app, uid)
    await start_join_queue_service(app, uid)
    await sync_bot_account_listeners(app, uid)
    return False


async def sub_revoke_access(app: Application, uid: int):
    """Remove `uid` exactly like /rem. The owner can never be revoked."""
    if uid == ADMIN_ID or uid not in APPROVED_USERS:
        return
    APPROVED_USERS.discard(uid)  # Reject new actions before stopping existing jobs.
    if CO_ADMINS.pop(uid, None) is not None:
        await save_co_admins()
    await asyncio.to_thread(operator_access.delete_one, {"_id": uid})
    st = tenant_stats(uid)
    st.running = False
    tasks = [PROMO_TASKS.pop(uid, None), LISTENER_TASKS.pop(uid, None), JOIN_QUEUE_TASKS.pop(uid, None)]
    tasks.extend([task for (owner_uid, _), task in list(BOT_ACCOUNT_TASKS.items()) if owner_uid == uid])
    for key in [key for key in list(BOT_ACCOUNT_TASKS) if key[0] == uid]:
        BOT_ACCOUNT_TASKS.pop(key, None)
    tasks.append(app.bot_data.pop(f"dm_broadcast_task:{uid}", None))
    app.bot_data[f"dm_broadcast_running:{uid}"] = False
    for task in tasks:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
    token = CURRENT_OPERATOR.set(uid)
    try:
        rows = await tenant_sessions(uid).get_sessions()
        await asyncio.gather(*(close_account_client(str(row["id"])) for row in rows),
                             return_exceptions=True)
    finally:
        CURRENT_OPERATOR.reset(token)


async def sub_start(app: Application):
    """Start the subscription service. Failures here never stop the rest of the bot."""
    try:
        async def _notify(uid, text):
            await app.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        await subs_core.init(MONGO_URI, MONGO_DB, ADMIN_ID, asyncio.get_running_loop(), {
            "is_approved": lambda uid: uid in APPROVED_USERS,
            "grant": lambda uid: sub_grant_access(app, uid),
            "revoke": lambda uid: sub_revoke_access(app, uid),
            "notify": _notify,
        })
        subs_core.set_log_bot(app.bot)
        await subs_core.expiry_sweep()      # revoke anything that expired while the bot was offline
        await subs_core.start_background()
        log.info("Subscription service started (payments configured: %s, webhook secret: %s)",
                 subs_core.payments_configured(), subs_core.webhook_configured())
    except Exception:
        log.exception("Subscription service failed to start; the rest of the bot continues normally")

async def list_operators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID or update.effective_chat.type != "private":
        return
    operators = sorted(APPROVED_USERS - {ADMIN_ID})
    if not operators:
        return await update.message.reply_text("👥 No other users approved. Add one with /add <uid>.")
    lines = ["👥 Approved users:"] + [f"• <code>{uid}</code>" for uid in operators]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

LAST_MENU: dict[int, int] = {}   # chat_id -> message_id of the live main menu
MAINTENANCE = {"on": False}


def main_markup(uid: int):
    rows = []
    for row in MENU_LAYOUT:
        btns = [styled_button(_menu_label(k), k) for k in row if not _menu_hidden(k)]
        if btns:
            rows.append(btns)
    if can_open_panel(uid):
        rows.append([styled_button(_menu_label("adm:home"), "adm:home")])
    if not _menu_hidden("sub:home"):
        rows.append([InlineKeyboardButton(_menu_label("sub:home"), callback_data="sub:home",
                                          api_kwargs={"style": "danger"} if BUTTON_STYLES else None)])
    return InlineKeyboardMarkup(rows)


_LEGACY_KB_DONE: set[int] = set()   # users whose legacy keyboard is known to be cleared


async def _clear_legacy_keyboard(message, uid: int):
    """Remove the old bottom Reply Keyboard once per user (persisted), never again."""
    key = f"legacy_kb_cleared:{uid}"
    if uid in _LEGACY_KB_DONE:
        return
    try:
        adb = tenant_db(ADMIN_ID)
        if await adb.get_setting(key) == "1":
            _LEGACY_KB_DONE.add(uid)
            return
        tmp = await message.reply_text("🔄", reply_markup=ReplyKeyboardRemove())
        await tmp.delete()
        await adb.set_setting(key, "1")
        _LEGACY_KB_DONE.add(uid)
    except Exception:
        pass


async def show_main_menu(message, uid: int | None = None):
    uid = uid or (message.chat.id if getattr(message, "chat", None) else tenant_id())
    if getattr(message, "edit_in_place", False):
        await message.reply_text(await main_text(), parse_mode=ParseMode.HTML, reply_markup=main_markup(uid))
        return
    chat_id = message.chat.id if getattr(message, "chat", None) else uid
    prev = LAST_MENU.pop(chat_id, None)
    if prev:
        try:
            await message.get_bot().delete_message(chat_id, prev)
        except Exception:
            pass
    await _clear_legacy_keyboard(message, uid)
    sent = await message.reply_text(await main_text(), parse_mode=ParseMode.HTML, reply_markup=main_markup(uid))
    LAST_MENU[chat_id] = sent.message_id


# ---- Help guide: /help command and the ❓ How to Use button. Each section is an expandable quote.
HELP_SECTIONS = [
    ("🚀 Getting Started",
     "1. Tap Start or the Menu button to open the main menu.\n"
     "2. New users can start a free trial or buy a subscription.\n"
     "3. Subscriptions are paid by QR code only. Access turns on after payment is confirmed.\n"
     "4. Check your plan any time from Purchase Subscription."),
    ("📣 Promotion",
     "▶️ Start Promo / ⏹ Stop Promo: run or stop your ad rounds.\n"
     "⏱ Interval: wait time between rounds.\n"
     "⏳ Group Delay: pause between groups.\n"
     "🔄 Rescan Targets: refresh the group list.\n"
     "📨 DM Broadcast: message your contacts.\n"
     "📅 Schedule: set timed runs.\n"
     "🌙 Quiet Hours: pause promotions in a daily window, e.g. 23:00-07:00.\n"
     "🎯 Promo Accounts: choose which accounts send ads."),
    ("📝 Posts",
     "📝 Save Post: store a post to send.\n"
     "📋 List Posts and 👁 Preview Post: review saved posts.\n"
     "🔘 Toggle Post: turn a post on or off.\n"
     "🗑 Delete Post: remove a post.\n"
     "🔀 Sending Mode: Rotate goes in order, Random picks any post."),
    ("👥 Accounts",
     "➕ Add Session: add an account by session string or phone login.\n"
     "👤 Sessions: see every connected account.\n"
     "🔗 Join Groups: add group links and track the join queue.\n"
     "🤖 Bot Accounts: connect your bot tokens.\n"
     "🗑 Remove Session: remove an account."),
    ("🤖 Auto Reply",
     "✏️ Account Reply / Control Bot Reply: set the reply text.\n"
     "🟢 Toggle buttons: switch replies on or off.\n"
     "🤖 AI Mode: replies with AI using your Groq API key. Accept the rules first.\n"
     "📚 AI Profile: describe your business so the AI answers well.\n"
     "⏱ Reply Cooldown: limit how often one chat gets a reply.\n"
     "📡 Reply Status: see what is running."),
    ("✏️ Edit Bio",
     "🤖 Set Bot Bio / 👤 Set User Bio: change the bio text.\n"
     "👤 Individual: change one account.\n"
     "🌐 Set All: change every account."),
    ("📊 Monitoring",
     "📊 Live Stats: what is running right now.\n"
     "📊 Account Stats: numbers per account.\n"
     "📜 Account Logs: recent events."),
    ("💡 Tips",
     "• Never share your session strings or bot tokens.\n"
     "• Keep interval and group delay high to stay safe.\n"
     "• Flood waits pause automatically. Let them finish.\n"
     "• Need help? Use Support from the Purchase Subscription menu."),
]
HELP_ADMIN_SECTION = (
    "🛡 Admin Panel",
    "🛡 Admin Panel: manage approved users, subscriptions, broadcasts, support and account status.\n"
    "👮 Co-Admins: the owner grants you specific sections.\n"
    "🎨 Look & Emoji, 🔧 Maintenance and the log channel: owner only.",
)


HELP_TITLE = ("❓ <b>How to use this bot</b>\n"
              "<blockquote>Step-by-step guide. Tap a section to open it.</blockquote>")
HELP_BANNER = Path(__file__).resolve().parent / "assets" / "help_banner.png"
HELP_STEPS = Path(__file__).resolve().parent / "assets" / "getting_started.png"


def help_messages(uid=None, title=True) -> list:
    sections = list(HELP_SECTIONS)
    if uid is not None and can_open_panel(uid):
        sections.insert(-1, HELP_ADMIN_SECTION)
    parts = [f"<b>{t}</b>\n<blockquote expandable>{html.escape(b)}</blockquote>" for t, b in sections]
    out, cur = [], (HELP_TITLE if title else "")
    for p in parts:
        if len(cur) + len(p) + 2 > 3800:
            out.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    out.append(cur)
    return out


def _asset_bytes(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or update.effective_chat.type != "private":
        return
    chunks = help_messages(update.effective_user.id, title=False)
    banner, steps = _asset_bytes(HELP_BANNER), _asset_bytes(HELP_STEPS)
    if banner:
        await update.message.reply_photo(photo=banner, caption=HELP_TITLE, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(HELP_TITLE, parse_mode=ParseMode.HTML)
    if steps:
        await update.message.reply_photo(photo=steps, caption="Getting started in 4 steps")
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    chunks = help_messages(q.from_user.id, title=False)
    banner, steps = _asset_bytes(HELP_BANNER), _asset_bytes(HELP_STEPS)
    if banner:
        await context.bot.send_photo(chat_id=chat_id, photo=banner, caption=HELP_TITLE, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=HELP_TITLE, parse_mode=ParseMode.HTML)
    if steps:
        await context.bot.send_photo(chat_id=chat_id, photo=steps, caption="Getting started in 4 steps")
    for chunk in chunks:
        await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await bot_dm_auto_reply(update, context)
    state.pop(update.effective_user.id, None)
    # Repeated /start: drop the user's /start message and the previous menu, send exactly one menu.
    await safe_delete_message(update.message)
    await show_main_menu(update.message, update.effective_user.id)


async def cleanup_login_flow(uid: int):
    flow = login_flows.pop(uid, None)
    if flow and flow.get("client"):
        try:
            await flow["client"].disconnect()
        except Exception:
            pass


async def save_logged_in_client(client: TelegramClient):
    me = await client.get_me()
    session_string = client.session.save()
    if not session_string:
        raise RuntimeError("Could not export StringSession")
    label, stored_sid, _stored_row, _shared = await _register_validated_session(
        client, me, session_string
    )
    # Caller must not disconnect this client after successful adoption.
    return label, None, None, stored_sid


async def safe_delete_message(message):
    try:
        await message.delete()
    except Exception:
        pass


# ---- Promotion category: which accounts are used (and therefore logged in) for ads.
PROMO_ACCOUNTS_KEY = "promo_account_ids"


async def promo_selected_ids(all_ids):
    """Stored selection; if the admin never chose, every enabled account is used."""
    raw = await db.get_setting(PROMO_ACCOUNTS_KEY, None)
    if raw is None:
        return list(all_ids)
    try:
        wanted = set(json.loads(raw))
    except (TypeError, ValueError):
        return list(all_ids)
    return [i for i in all_ids if i in wanted]


def _promo_accounts_view(rows, selected):
    lines = ["🎯 <b>PROMO ACCOUNTS</b>", "",
             "Only the accounts ticked below log in and send promotions in this category.",
             f"Selected: <b>{len(selected)}</b> of {len(rows)}", ""]
    buttons = []
    for r in rows:
        sid = str(r["id"])
        name = r.get("first_name") or r.get("label") or str(r.get("tg_user_id") or sid)
        mark = "✅" if sid in selected else "⬜"
        buttons.append([InlineKeyboardButton(f"{mark} {str(name)[:30]}", callback_data=f"pacc:{sid}")])
    if not rows:
        lines.append("No active accounts yet. Add one from 👥 Accounts.")
    buttons.append([InlineKeyboardButton("☑️ Select all", callback_data="pacc:all"),
                    InlineKeyboardButton("🚫 Clear", callback_data="pacc:none")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pacc:done")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def show_promo_accounts(message):
    rows = await session_store.get_sessions(enabled_only=True)
    selected = set(await promo_selected_ids([str(r["id"]) for r in rows]))
    text, markup = _promo_accounts_view(rows, selected)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def promo_accounts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "done":
        return await query.edit_message_text("📣 <b>PROMOTION</b>", parse_mode=ParseMode.HTML, reply_markup=PROMOTION_MENU)
    rows = await session_store.get_sessions(enabled_only=True)
    ids = [str(r["id"]) for r in rows]
    selected = set(await promo_selected_ids(ids))
    if action == "all":
        selected = set(ids)
    elif action == "none":
        selected = set()
    elif action in ids:
        selected ^= {action}
    else:
        return
    await db.set_setting(PROMO_ACCOUNTS_KEY, json.dumps(sorted(selected)))
    text, markup = _promo_accounts_view(rows, selected)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---- Front menu editor + premium emoji (admin: 🎨 Look & Emoji)
MENU_ITEMS = [
    ("main:promotion", "Promotion"), ("main:posts", "Posts"), ("main:accounts", "Accounts"),
    ("main:autoreply", "Auto Reply"), ("main:bio", "Edit Bio"), ("main:monitoring", "Monitoring"),
    ("help:main", "How to Use"), ("adm:home", "Admin Panel"), ("sub:home", "Purchase Subscription"),
]
MENU_DEFAULTS = dict(MENU_ITEMS)
# Normal emoji that a premium emoji replaces for each front-menu button (not shown on the button itself).
NORMAL_EMOJI_FOR = {
    "main:promotion": "📣", "main:posts": "📝", "main:accounts": "👥", "main:autoreply": "🤖",
    "main:bio": "✏️", "main:monitoring": "📊", "help:main": "❓", "adm:home": "🛡", "sub:home": "💎",
}
MENU_LAYOUT = [["main:promotion"], ["main:posts", "main:accounts"], ["main:autoreply", "main:bio"], ["main:monitoring", "help:main"]]
# menu: callback -> {"label": str (optional), "hidden": bool (optional)}; premium_map: normal emoji -> custom emoji id
LOOK = {"menu": {}, "premium_on": False, "premium_map": {}}


async def load_look_settings():
    adb = tenant_db(ADMIN_ID)
    try:
        raw = await adb.get_setting("menu_overrides", None)
        LOOK["menu"] = json.loads(raw) if raw else {}
        raw = await adb.get_setting("premium_emoji", None)
        data = json.loads(raw) if raw else {}
        LOOK["premium_on"] = bool(data.get("on"))
        LOOK["premium_map"] = dict(data.get("map") or {})
    except (TypeError, ValueError):
        log.warning("could not parse look settings; using defaults")


async def save_look_settings():
    adb = tenant_db(ADMIN_ID)
    await adb.set_setting("menu_overrides", json.dumps(LOOK["menu"], ensure_ascii=False))
    await adb.set_setting("premium_emoji", json.dumps({"on": LOOK["premium_on"], "map": LOOK["premium_map"]}, ensure_ascii=False))


def _menu_label(key):
    return (LOOK["menu"].get(key) or {}).get("label") or MENU_DEFAULTS[key]


def _menu_hidden(key):
    return key != "adm:home" and bool((LOOK["menu"].get(key) or {}).get("hidden"))


def _premium_pattern():
    keys = sorted(LOOK["premium_map"], key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys)) if keys else None


def _premium_text(text, mode):
    """Returns (new_text, entities). HTML: <tg-emoji> tags. Plain: custom_emoji entities."""
    pat = _premium_pattern()
    if pat is None or not text:
        return text, None
    m = LOOK["premium_map"]
    if mode == "HTML":
        out = []
        for part in re.split(r"(<[^>]+>)", text):
            if part.startswith("<") and part.endswith(">"):
                out.append(part)
            else:
                out.append(pat.sub(lambda x: f'<tg-emoji emoji-id="{m[x.group(0)]}">{x.group(0)}</tg-emoji>', part))
        return "".join(out), None
    if mode in (None, ""):
        ents = []
        for x in pat.finditer(text):
            off = len(text[:x.start()].encode("utf-16-le")) // 2
            ln = len(x.group(0).encode("utf-16-le")) // 2
            ents.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=off, length=ln,
                                      custom_emoji_id=m[x.group(0)]))
        return text, (ents or None)
    return text, None


def _all_button_labels():
    """Every button label used in the bot's menus (current text), in first-seen order."""
    markups = [v for v in list(globals().values()) if isinstance(v, InlineKeyboardMarkup)]
    markups.append(main_markup(ADMIN_ID))
    markups.append(admin_markup())
    labels = []
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                if b.text and b.text not in labels:
                    labels.append(b.text)
    return labels


def _leading_emoji(text):
    if not text:
        return None
    if len(text) >= 2 and text[1] == "\ufe0f":
        return text[:2]
    return text[0] if ord(text[0]) >= 0x2190 else None


def _icon_for(callback_data, text):
    if not LOOK["premium_map"]:
        return None
    em = NORMAL_EMOJI_FOR.get(callback_data) or _leading_emoji(text)
    return LOOK["premium_map"].get(em) if em else None


def _premium_markup(markup):
    """Adds the premium emoji (mapped to the button's normal emoji) as the button icon. Label text is never changed."""
    if not isinstance(markup, InlineKeyboardMarkup) or not LOOK["premium_map"]:
        return markup
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            eid = _icon_for(btn.callback_data, btn.text or "")
            if eid:
                d = btn.to_dict()
                em = NORMAL_EMOJI_FOR.get(btn.callback_data) or _leading_emoji(btn.text or "")
                if em and (btn.text or "").startswith(em):
                    d["text"] = (btn.text[len(em):].lstrip()) or btn.text
                d["icon_custom_emoji_id"] = eid
                new_row.append(InlineKeyboardButton.de_json(d, None))
            else:
                new_row.append(btn)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)


# ---- Plan limits: free trial vs admin-set plans
BOT_BUTTON_TEXTS = {"🤖 Bot Accounts", "👤 Bot Accounts", "➕ Add Bot", "📊 Bot Stats", "📜 Bot Logs",
                    "🟢 Toggle Bot Auto Reply", "🟢 Toggle Group Reply", "⏱ Bot Reply Cooldown", "🗑 Remove Bot"}
AI_BUTTON_TEXTS = {"🤖 AI Mode", "🔑 Groq API Key", "📚 AI Profile", "✅ I Accept These Rules"}
LOCKED_TEXT = "🔒 This feature is not included in your current plan. Ask the admin to upgrade."
_caps_cache: dict[int, tuple[float, dict]] = {}
FULL_CAPS = {"max_accounts": None, "bot": True, "ai": True}


async def plan_caps(uid: int) -> dict:
    if uid == ADMIN_ID or subs_core.STORE is None:
        return dict(FULL_CAPS)
    hit = _caps_cache.get(uid)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        sub = await subs_core.run(subs_core.STORE.get_sub, uid)
        if sub is None:                       # admin-approved without a subscription
            caps = dict(FULL_CAPS)
        elif sub.get("status") != "active" or subs_core._aware(sub["expires_at"]) <= subs_core.now_utc():
            caps = dict(subs_core.TRIAL_CAPS)
        elif sub.get("plan_id") == "trial":
            caps = dict(subs_core.TRIAL_CAPS)
        else:
            plan = await subs_core.run(subs_core.STORE.get_plan, sub.get("plan_id"))
            caps = subs_core.caps_for_plan(plan) if plan else dict(subs_core.TRIAL_CAPS)
    except Exception:
        log.exception("plan caps lookup failed for %s", uid)
        return dict(subs_core.TRIAL_CAPS)
    _caps_cache[uid] = (time.time(), caps)
    return caps


def _hidden_for(caps) -> set:
    hidden = set()
    if not caps["bot"]:
        hidden |= BOT_BUTTON_TEXTS
    if not caps["ai"]:
        hidden |= AI_BUTTON_TEXTS
    return hidden


async def _gate_markup(uid, markup):
    """Removes buttons the user's plan does not include. Admin and non-chat sends are untouched."""
    if not isinstance(uid, int) or uid <= 0 or uid == ADMIN_ID or not isinstance(markup, InlineKeyboardMarkup):
        return markup
    gated = BOT_BUTTON_TEXTS | AI_BUTTON_TEXTS
    if not any(b.text in gated for row in markup.inline_keyboard for b in row):
        return markup                       # no DB read for ordinary messages
    hidden = _hidden_for(await plan_caps(uid))
    if not hidden:
        return markup
    rows = [kept for kept in ([b for b in row if b.text not in hidden] for row in markup.inline_keyboard) if kept]
    return InlineKeyboardMarkup(rows)


async def _with_network_retry(call, attempts: int = 4):
    """Retries a Telegram call on network errors (not on BadRequest). Waits 1s, 2s, 4s between tries."""
    delay = 1.0
    for attempt in range(attempts):
        try:
            return await call()
        except NetworkError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2


class PremiumBot(ExtBot):
    """Applies admin-chosen premium emoji to outgoing text/captions and button icons.
    Any Telegram rejection falls back to the original, plain send so messages are never lost."""

    async def send_message(self, chat_id, text, *args, **kwargs):
        if args:
            return await super().send_message(chat_id, text, *args, **kwargs)
        return await self._premium_send(super().send_message, dict(kwargs, chat_id=chat_id, text=text))

    async def answer_callback_query(self, *args, **kwargs):
        """Button acknowledgements are short-lived, so retry briefly and never crash the handler."""
        call = super().answer_callback_query
        try:
            return await _with_network_retry(lambda: call(*args, **kwargs), attempts=2)
        except NetworkError:
            log.warning("callback answer dropped (network); the button still works")
            return False

    async def edit_message_text(self, text, *args, **kwargs):
        if args:
            return await super().edit_message_text(text, *args, **kwargs)
        return await self._premium_send(super().edit_message_text, dict(kwargs, text=text))

    async def send_photo(self, photo, *args, **kwargs):
        if args:
            return await super().send_photo(photo, *args, **kwargs)
        return await self._premium_send(super().send_photo, dict(kwargs, photo=photo))

    async def _premium_send(self, method, kw):
        if kw.get("reply_markup") is not None and "chat_id" in kw:
            kw["reply_markup"] = await _gate_markup(kw["chat_id"], kw["reply_markup"])
        if not LOOK["premium_on"]:
            return await _with_network_retry(lambda: method(**kw))
        new = dict(kw)
        key = "text" if "text" in kw else ("caption" if "caption" in kw else None)
        if key and LOOK["premium_map"] and isinstance(kw[key], str) and kw.get("entities") is None and kw.get("caption_entities") is None:
            new[key], ents = _premium_text(kw[key], kw.get("parse_mode"))
            if ents:
                new["entities" if key == "text" else "caption_entities"] = ents
        if kw.get("reply_markup") is not None:
            new["reply_markup"] = _premium_markup(kw["reply_markup"])
        try:
            return await _with_network_retry(lambda: method(**new))
        except BadRequest as e:
            log.warning("premium emoji send rejected (%s); sending plain", e)
            return await _with_network_retry(lambda: method(**kw))


def _utf16_slice(text, offset, length):
    return text.encode("utf-16-le")[offset * 2:(offset + length) * 2].decode("utf-16-le")


def look_home_view():
    return ("🎨 <b>LOOK &amp; EMOJI</b>\n\nEdit the front menu and use premium emoji.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🧭 Front Menu", callback_data="adm:menu")],
                [InlineKeyboardButton("💠 Premium Emoji", callback_data="adm:emoji")],
                [InlineKeyboardButton("📝 Front Message", callback_data="adm:front")],
                [InlineKeyboardButton("🏷 Free-Plan Bio", callback_data="adm:fbio")],
                [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
            ]))


def menu_editor_view(note=""):
    lines = ["🧭 <b>FRONT MENU</b>", "", "Tap an item to rename or hide it. Actions stay the same.", ""]
    rows = []
    for i, (k, _default) in enumerate(MENU_ITEMS):
        lbl = _menu_label(k)
        lines.append(f"{i + 1}. {esc(lbl)}" + (" <i>(hidden)</i>" if _menu_hidden(k) else ""))
        rows.append([InlineKeyboardButton(f"✏️ {lbl[:30]}", callback_data=f"mnu:e:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:look")])
    return (note + "\n\n" if note else "") + "\n".join(lines), InlineKeyboardMarkup(rows)


def menu_item_view(idx, note=""):
    k, default = MENU_ITEMS[idx]
    status = "🙈 hidden" if _menu_hidden(k) else "👁 visible"
    text = (f"🧭 <b>{esc(_menu_label(k))}</b>\n\nDefault: {esc(default)}\nStatus: {status}")
    if note:
        text = note + "\n\n" + text
    rows = [[InlineKeyboardButton("✏️ Rename", callback_data=f"mnu:r:{idx}")]]
    if k != "adm:home":
        rows.append([InlineKeyboardButton("👁 Show" if _menu_hidden(k) else "🙈 Hide", callback_data=f"mnu:h:{idx}")])
    rows.append([InlineKeyboardButton("↩️ Reset to default", callback_data=f"mnu:x:{idx}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:menu")])
    return text, InlineKeyboardMarkup(rows)


def _btn_emoji_list():
    chars = []
    for _cb, em in NORMAL_EMOJI_FOR.items():
        if em not in chars:
            chars.append(em)
    for label in _all_button_labels():
        em = _leading_emoji(label)
        if em and em not in chars:
            chars.append(em)
    return chars[:60]


def btn_emoji_view(note=""):
    chars = _btn_emoji_list()
    m = LOOK["premium_map"]
    lines = ["🎛 <b>BUTTON EMOJI</b>", "",
             "Tap an emoji, then send a message that has the premium emoji.",
             "That premium emoji replaces the normal one in buttons and messages. Button names stay the same.", "",
             f"Set: <b>{sum(1 for c in chars if c in m)}</b> of {len(chars)}"]
    rows, row = [], []
    for i, c in enumerate(chars):
        row.append(InlineKeyboardButton(("✅ " if c in m else "") + c, callback_data=f"bem:p:{i}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:emoji")])
    return (note + "\n\n" if note else "") + "\n".join(lines), InlineKeyboardMarkup(rows)


def emoji_view(note=""):
    on, m = LOOK["premium_on"], LOOK["premium_map"]
    lines = ["💠 <b>PREMIUM EMOJI</b>", "",
             "Normal emoji in bot messages and button icons are replaced by the premium emoji mapped here.",
             "Users with Telegram Premium see the premium version; others see the normal emoji.", "",
             f"Status: <b>{'ON 🟢' if on else 'OFF 🔴'}</b>", f"Mapped: <b>{len(m)}</b>", ""]
    rows = [[InlineKeyboardButton("🔁 Turn " + ("OFF" if on else "ON"), callback_data="emj:toggle")],
            [InlineKeyboardButton("➕ Add premium emoji", callback_data="emj:add")],
            [InlineKeyboardButton("🎛 Button Emoji", callback_data="adm:btnemoji")]]
    if not m:
        lines.append("No mappings yet. Tap ➕ Add, then send one message that contains a premium emoji.")
    for n, (base, eid) in enumerate(list(m.items())[:30]):
        lines.append(f"{esc(base)} → <code>{esc(eid)}</code>")
        rows.append([InlineKeyboardButton(f"🗑 Remove {base}", callback_data=f"emj:d:{n}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:look")])
    return (note + "\n\n" if note else "") + "\n".join(lines), InlineKeyboardMarkup(rows)


async def look_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q.from_user or q.from_user.id != ADMIN_ID:
        return await q.answer("⛔ Admin only", show_alert=True)
    await q.answer()
    uid, data = q.from_user.id, q.data
    text = markup = None
    if data == "adm:look":
        text, markup = look_home_view()
    elif data == "adm:menu":
        text, markup = menu_editor_view()
    elif data == "adm:emoji":
        text, markup = emoji_view()
    elif data == "adm:btnemoji":
        text, markup = btn_emoji_view()
    elif data == "adm:btnemoji":
        text, markup = btn_emoji_view()
    elif data.startswith("bem:p:"):
        chars = _btn_emoji_list()
        i = int(data.split(":")[2])
        if 0 <= i < len(chars):
            ch = chars[i]
            state[uid] = f"await_btn_emoji:{ch}"
            rows = []
            if ch in LOOK["premium_map"]:
                rows.append([InlineKeyboardButton("🗑 Remove premium", callback_data=f"bem:x:{i}")])
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data="adm:btnemoji")])
            text = f"🎛 Send a message with the premium emoji to replace {ch}."
            markup = InlineKeyboardMarkup(rows)
    elif data.startswith("bem:x:"):
        chars = _btn_emoji_list()
        i = int(data.split(":")[2])
        if 0 <= i < len(chars):
            LOOK["premium_map"].pop(chars[i], None)
            await save_look_settings()
            text, markup = btn_emoji_view("🗑 Removed.")
    elif data.startswith("mnu:"):
        _, act, sidx = data.split(":")
        idx = int(sidx)
        if not 0 <= idx < len(MENU_ITEMS):
            return
        k, _d = MENU_ITEMS[idx]
        if act == "e":
            text, markup = menu_item_view(idx)
        elif act == "r":
            state[uid] = f"await_menu_label:{idx}"
            text = f"✏️ Send the new label for <b>{esc(_menu_label(k))}</b> (1–40 characters, one line)."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"mnu:e:{idx}")]])
        elif act == "h" and k != "adm:home":
            entry = LOOK["menu"].setdefault(k, {})
            entry["hidden"] = not entry.get("hidden")
            await save_look_settings()
            text, markup = menu_item_view(idx, "✅ Saved.")
        elif act == "x":
            LOOK["menu"].pop(k, None)
            await save_look_settings()
            text, markup = menu_item_view(idx, "↩️ Reset to default.")
    elif data.startswith("emj:"):
        parts = data.split(":")
        if parts[1] == "toggle":
            LOOK["premium_on"] = not LOOK["premium_on"]
            await save_look_settings()
            text, markup = emoji_view("✅ Premium emoji " + ("ON" if LOOK["premium_on"] else "OFF") + ".")
        elif parts[1] == "add":
            state[uid] = "await_premium_emoji"
            text = ("💠 Send one message that contains the premium emoji you want to use. "
                    "Its normal emoji will be replaced by it everywhere.")
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:emoji")]])
        elif parts[1] == "d":
            keys = list(LOOK["premium_map"])
            n = int(parts[2])
            if 0 <= n < len(keys):
                LOOK["premium_map"].pop(keys[n], None)
                await save_look_settings()
                text, markup = emoji_view("🗑 Removed.")
    if data == "adm:front":
        text, markup = await front_admin_view()
    elif data == "frt:e":
        state[uid] = "await_front_text"
        text = "✏️ Send the new front message now. Telegram formatting (bold, italic, links) is kept. Max 3500 characters."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:front")]])
    elif data == "frt:r":
        await tenant_db(ADMIN_ID).set_setting("front_text", "")
        invalidate_front_text()
        text, markup = await front_admin_view("↩️ Reset to the default message.")
    elif data == "adm:fbio":
        text, markup = await fbio_view()
    elif data == "fbio:t":
        state[uid] = "await_free_tag"
        text = "🏷 Send the tag to put in the bio (for example @YourBotName), max 60 characters, one line."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:fbio")]])
    elif data == "fbio:tg":
        adb = tenant_db(ADMIN_ID)
        on = (await adb.get_setting("free_bio_enabled", "0")) == "1"
        await adb.set_setting("free_bio_enabled", "0" if on else "1")
        text, markup = await fbio_view("✅ Turned " + ("OFF" if on else "ON") + ".")
    elif data == "fbio:run":
        tag = ((await tenant_db(ADMIN_ID).get_setting("free_bio_tag", "")) or "").strip()
        if not tag:
            text, markup = await fbio_view("❌ Set a tag first.")
        else:
            c = await enforce_free_bio_tags(tag)
            text, markup = await fbio_view(f"🔁 Checked {c['checked']} · fixed {c['fixed']} · failed {c['failed']}.")
    if text is None:
        return
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def look_text_input(update: Update, uid: int, current: str, text: str):
    msg = update.message
    state.pop(uid, None)
    if text in {"⬅️ Back", "❌ Cancel"}:
        t, m = look_home_view()
        return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
    if current.startswith("await_menu_label:"):
        idx = int(current.split(":", 1)[1])
        label = text.strip()
        if not 1 <= len(label) <= 40 or "\n" in label:
            state[uid] = current
            return await msg.reply_text("❌ Label must be 1–40 characters on one line. Send again or tap ❌ Cancel.")
        k, default = MENU_ITEMS[idx]
        entry = LOOK["menu"].setdefault(k, {})
        if label == default:
            entry.pop("label", None)
        else:
            entry["label"] = label
        if not entry:
            LOOK["menu"].pop(k, None)
        await save_look_settings()
        t, m = menu_editor_view("✅ Label updated.")
        return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
    if current == "await_front_text":
        body = msg.text_html or ""
        if not body or len(body) > 3500:
            state[uid] = current
            return await msg.reply_text("❌ Send a message of 1–3500 characters, or tap ❌ Cancel.")
        try:
            await msg.get_bot().send_message(chat_id=uid, text=body, parse_mode=ParseMode.HTML,
                                             disable_web_page_preview=True)
        except BadRequest as e:
            state[uid] = current
            return await msg.reply_text(f"❌ Telegram rejected the formatting ({e.message}). Edit and send again.")
        await tenant_db(ADMIN_ID).set_setting("front_text", body)
        invalidate_front_text()
        t, m = await front_admin_view("✅ Front message saved.")
        return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
    if current == "await_free_tag":
        tag = text.strip()
        if not 1 <= len(tag) <= 60 or "\n" in tag:
            state[uid] = current
            return await msg.reply_text("❌ Tag must be 1–60 characters on one line. Send again or tap ❌ Cancel.")
        adb = tenant_db(ADMIN_ID)
        await adb.set_setting("free_bio_tag", tag)
        await adb.set_setting("free_bio_enabled", "1")
        t, m = await fbio_view("✅ Tag saved and turned ON.")
        return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
    if current.startswith("await_btn_emoji:"):
        ch = current.split(":", 1)[1]
        eid = next((e.custom_emoji_id for e in (msg.entities or [])
                    if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None)), None)
        if not eid:
            state[uid] = current
            return await msg.reply_text("❌ No premium emoji in that message. Send one that has a premium emoji, or tap ❌ Cancel.")
        LOOK["premium_map"][ch] = eid
        await save_look_settings()
        t, m = btn_emoji_view(f"✅ Premium emoji set for {ch}.")
        return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
    # await_premium_emoji
    found = {}
    for e in (msg.entities or []):
        if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None):
            base = _utf16_slice(text, e.offset, e.length)
            if base:
                found[base] = e.custom_emoji_id
    if not found:
        state[uid] = current
        return await msg.reply_text("❌ No premium emoji in that message. Send one that contains a premium emoji, or tap ❌ Cancel.")
    LOOK["premium_map"].update(found)
    await save_look_settings()
    t, m = emoji_view(f"✅ Added {len(found)} mapping(s).")
    return await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)


# ---- AI reply mode (Groq). Each owner brings their own Groq key; it is stored encrypted.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
AI_POLICY_TEXT = (
    "⚠️ <b>AI MODE — RULES</b>\n\n"
    "AI jawab aapke account se bhejega. Aage badhne se pehle ye rules maanna zaroori hai:\n\n"
    "🚫 WhatsApp ya Telegram accounts/numbers ko bechna, kharidna ya transfer karna sakht mana hai.\n"
    "🚫 OTP, password, login code ya verification code maangna ya share karna mana hai.\n"
    "🚫 Scam, fraud, impersonation ya spam ke liye use karna mana hai.\n"
    "🚫 Paise ka lena-dena AI nahi karega.\n\n"
    "Niyam todne par AI mode band ho jayega aur access hata diya jayega. "
    "Groq key aur jawabon ki zimmedari aapki hogi."
)
AI_BLOCKED_REPLY = ("Is baat ke liye owner se direct baat karein. "
                    "Main AI assistant hoon aur account, OTP ya payment se related kaam nahi karta.")
AI_MEDIA_REPLY = "Abhi main sirf text samajh sakta hoon. Apna sawal likh kar bhejein."
AI_RISKY = re.compile(
    r"otp|password|2fa|login code|verification code|sell.{0,20}account|buy.{0,20}account|"
    r"account.{0,20}(sale|sell|buy|transfer)|number.{0,20}(sale|sell|buy)|send money|upi|bitcoin|crypto|"
    r"(buy|sell|kharid|bech).{0,20}(account|number)|bech.{0,15}(account|number)|account.{0,15}bech", re.I)
_ai_history: dict = {}


def ai_is_risky(text: str) -> bool:
    return bool(AI_RISKY.search(text or ""))


def ai_system_prompt(profile: str) -> str:
    return (
        "You are an AI assistant replying on behalf of the account owner in a private chat. "
        "Say you are an AI assistant in your first reply. Reply in 1-2 short sentences (max 200 characters), "
        "in the same language the user uses (Hindi, English or Hinglish). "
        "Use only the business information below. If you do not know, say the owner will reply soon. "
        "Never ask for or share OTP, passwords, login or verification codes, or payment details. "
        "Never help buy, sell or transfer WhatsApp or Telegram accounts or numbers. "
        "Never help with scams, fraud, impersonation or spam.\n\nBUSINESS INFORMATION:\n" + profile
    )


async def groq_chat(api_key: str, system: str, messages: list) -> str:
    payload = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}] + messages,
               "max_tokens": 120, "temperature": 0.4}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(GROQ_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def groq_key_valid(api_key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def ai_reply_for(session_key: str, event):
    """Returns the AI reply text, or None to fall back to the normal auto-reply."""
    if not (await plan_caps(tenant_id()))["ai"]:
        return None
    enc = await db.get_setting("ai_groq_key", None)
    profile = ((await db.get_setting("ai_profile", "")) or "").strip()
    if not enc or not profile or not await db.get_setting("ai_policy_accepted_at", None):
        return None
    incoming = (getattr(event, "raw_text", "") or "").strip()
    if not incoming:
        return AI_MEDIA_REPLY
    if ai_is_risky(incoming):
        return AI_BLOCKED_REPLY
    try:
        api_key = fernet.decrypt(enc.encode()).decode()
    except InvalidToken:
        log.warning("Stored Groq key cannot be decrypted; AI reply skipped")
        return None
    hist = _ai_history.setdefault((session_key, int(event.sender_id)), deque(maxlen=6))
    hist.append({"role": "user", "content": incoming[:800]})
    try:
        reply = (await groq_chat(api_key, ai_system_prompt(profile), list(hist))).strip()[:500]
    except Exception as e:
        hist.pop()
        log.warning("Groq reply failed: %s", type(e).__name__)
        return None
    if not reply:
        hist.pop()
        return None
    hist.append({"role": "assistant", "content": reply})
    return reply


async def ai_status_text() -> str:
    accepted = bool(await db.get_setting("ai_policy_accepted_at", None))
    has_key = bool(await db.get_setting("ai_groq_key", None))
    has_profile = bool(((await db.get_setting("ai_profile", "")) or "").strip())
    on = await reply_enabled("ai_reply_enabled", False)
    yes = lambda b: "✅" if b else "❌"
    return ("🤖 <b>AI MODE</b>\n\n"
            f"Status: <b>{'ON 🟢' if on else 'OFF 🔴'}</b>\n"
            f"Rules accepted {yes(accepted)} · Groq key {yes(has_key)} · AI profile {yes(has_profile)}")


async def ai_menu_action(update: Update, uid: int, text: str):
    msg = update.message
    if text == "✅ I Accept These Rules":
        await db.set_setting("ai_policy_accepted_at", datetime.now(timezone.utc).isoformat())
        return await msg.reply_text("✅ Rules accepted.\n\n" + await ai_status_text(),
                                    parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
    if text == "🤖 AI Mode":
        if not await db.get_setting("ai_policy_accepted_at", None):
            return await msg.reply_text(AI_POLICY_TEXT, parse_mode=ParseMode.HTML, reply_markup=AI_POLICY_MENU)
        if not await db.get_setting("ai_groq_key", None):
            return await msg.reply_text("❌ Pehle 🔑 Groq API Key set karein.", reply_markup=AUTO_REPLY_MENU)
        if not ((await db.get_setting("ai_profile", "")) or "").strip():
            return await msg.reply_text("❌ Pehle 📚 AI Profile set karein.", reply_markup=AUTO_REPLY_MENU)
        now_on = await reply_enabled("ai_reply_enabled", False)
        await db.set_setting("ai_reply_enabled", "false" if now_on else "true")
        return await msg.reply_text(await ai_status_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
    if text == "🔑 Groq API Key":
        state[uid] = "await_groq_key"
        return await msg.reply_text("🔑 Apni Groq API key bhejein (gsk_...). Message turant delete ho jayega. "
                                    "Cancel ke liye Back dabayein.", reply_markup=AUTO_REPLY_MENU)
    if text == "📚 AI Profile":
        state[uid] = "await_ai_profile"
        return await msg.reply_text(
            "📚 Apna business likhein (20–2000 characters): aap kya bechte/sikhate hain, prices, timings, "
            "FAQ. AI sirf isi ke andar jawab dega.", reply_markup=AUTO_REPLY_MENU)


async def ai_text_input(update: Update, uid: int, current: str, text: str):
    msg = update.message
    state.pop(uid, None)
    if text in {"⬅️ Back", "❌ Cancel"}:
        return await msg.reply_text("Cancelled.", reply_markup=AUTO_REPLY_MENU)
    if current == "await_groq_key":
        key = text.strip()
        await safe_delete_message(msg)
        if not key.startswith("gsk_") or len(key) < 20:
            state[uid] = current
            return await msg.reply_text("❌ Ye Groq key jaisi nahi lagti. Dobara bhejein ya Back dabayein.")
        if not await groq_key_valid(key):
            state[uid] = current
            return await msg.reply_text("❌ Groq ne ye key accept nahi ki. Dobara bhejein ya Back dabayein.")
        await db.set_setting("ai_groq_key", fernet.encrypt(key.encode()).decode())
        return await msg.reply_text("✅ Groq key saved (encrypted).\n\n" + await ai_status_text(),
                                    parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
    profile = text.strip()
    if not 20 <= len(profile) <= 2000:
        state[uid] = current
        return await msg.reply_text("❌ 20 se 2000 characters ke beech bhejein.")
    await db.set_setting("ai_profile", profile)
    return await msg.reply_text("✅ AI profile saved.\n\n" + await ai_status_text(),
                                parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)


_FRONT_CACHE = {"value": None, "at": 0.0}


def invalidate_front_text():
    _FRONT_CACHE["at"] = 0.0


async def main_text() -> str:
    """Front message, cached briefly so every /start does not wait on the database."""
    if _FRONT_CACHE["value"] is not None and time.monotonic() - _FRONT_CACHE["at"] < 120:
        return _FRONT_CACHE["value"]
    try:
        value = (await tenant_db(ADMIN_ID).get_setting("front_text", "")) or MAIN_TEXT
    except Exception:
        return MAIN_TEXT
    _FRONT_CACHE.update(value=value, at=time.monotonic())
    return value


# ---- Free-plan bio tag: accounts of free-plan users must keep the admin's tag in their bio.
FREE_BIO_INTERVAL_SECONDS = 600
BIO_LIMIT = 70


def _fit_bio(bio: str, tag: str) -> str:
    base = (bio or "").strip()
    if tag.lower() in base.lower():
        return base
    if len(base) + 1 + len(tag) <= BIO_LIMIT:
        return f"{base} {tag}".strip()
    room = BIO_LIMIT - len(tag) - 1
    return f"{base[:max(room, 0)].rstrip()} {tag}".strip()


async def _is_free_plan(uid: int) -> bool:
    if not subs_core.STORE:
        return False
    sub = await subs_core.run(subs_core.STORE.get_sub, uid)
    return bool(sub and sub.get("status") == "active" and sub.get("plan_id") == "trial"
                and subs_core._aware(sub["expires_at"]) > subs_core.now_utc())


async def enforce_free_bio_tags(tag: str) -> dict:
    counts = {"checked": 0, "fixed": 0, "failed": 0}
    for uid in sorted(APPROVED_USERS - {ADMIN_ID}):
        if not await _is_free_plan(uid):
            continue
        token = CURRENT_OPERATOR.set(uid)
        try:
            rows = await session_store.get_sessions(enabled_only=True)
            for row in rows:
                counts["checked"] += 1
                try:
                    client = await get_account_client(row)
                    if not client.is_connected():
                        raise RuntimeError("not connected")
                    full = await client(GetFullUserRequest("me"))
                    bio = full.full_user.about or ""
                    new_bio = _fit_bio(bio, tag)
                    if new_bio != bio.strip():
                        await client(UpdateProfileRequest(about=new_bio))
                        counts["fixed"] += 1
                        await asyncio.sleep(2)
                except errors.FloodWaitError as e:
                    counts["failed"] += 1
                    await asyncio.sleep(min(e.seconds, 60))
                except Exception as e:
                    counts["failed"] += 1
                    log.warning("free bio check failed for %s: %s", row.get("id"), type(e).__name__)
        finally:
            CURRENT_OPERATOR.reset(token)
    return counts


async def free_bio_guard_loop():
    while True:
        try:
            adb = tenant_db(ADMIN_ID)
            tag = ((await adb.get_setting("free_bio_tag", "")) or "").strip()
            if tag and (await adb.get_setting("free_bio_enabled", "0")) == "1":
                await enforce_free_bio_tags(tag)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("free bio guard error")
        await asyncio.sleep(FREE_BIO_INTERVAL_SECONDS)


async def front_admin_view(note=""):
    cur = await main_text()
    custom = bool(await tenant_db(ADMIN_ID).get_setting("front_text", ""))
    text = (note + "\n\n" if note else "") + (
        "📝 <b>FRONT MESSAGE</b>\n\nThis text is shown above the front menu.\n"
        f"<i>{'Custom message active.' if custom else 'Using the default message.'}</i>\n\n— Preview —\n") + cur
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit", callback_data="frt:e")],
        [InlineKeyboardButton("↩️ Reset to default", callback_data="frt:r")],
        [InlineKeyboardButton("⬅️ Back", callback_data="adm:look")],
    ])


async def fbio_view(note=""):
    adb = tenant_db(ADMIN_ID)
    tag = ((await adb.get_setting("free_bio_tag", "")) or "").strip()
    on = (await adb.get_setting("free_bio_enabled", "0")) == "1"
    lines = ["🏷 <b>FREE-PLAN BIO TAG</b>", "",
             "Accounts of users on the free trial keep this tag in their Telegram bio.",
             "If they remove it, the bot adds it back automatically.", "",
             f"Tag: <b>{esc(tag) if tag else 'not set'}</b>",
             f"Status: <b>{'ON 🟢' if on else 'OFF 🔴'}</b>"]
    rows = [[InlineKeyboardButton("✏️ Set tag", callback_data="fbio:t")],
            [InlineKeyboardButton("🔁 Turn " + ("OFF" if on else "ON"), callback_data="fbio:tg")],
            [InlineKeyboardButton("▶️ Check now", callback_data="fbio:run")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:look")]]
    return (note + "\n\n" if note else "") + "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text or ""
    if context.user_data.pop("post_saved_message_id", None) == update.message.message_id:
        return

    # Auto-reply submenu: always process Back before text input.
    current = state.get(uid)
    if isinstance(current, str) and (current.startswith(("await_menu_label:", "await_btn_emoji:")) or current == "await_premium_emoji"):
        return await look_text_input(update, uid, current, text)
    if current == "await_coadmin":
        return await coadmin_text(update, uid, text)
    if current in ("await_front_text", "await_free_tag"):
        return await look_text_input(update, uid, current, text)
    if current in ("await_groq_key", "await_ai_profile"):
        return await ai_text_input(update, uid, current, text)
    if text in BOT_BUTTON_TEXTS or text in AI_BUTTON_TEXTS:
        if text in _hidden_for(await plan_caps(uid)):
            return await update.message.reply_text(LOCKED_TEXT)
    if text in {"🤖 AI Mode", "🔑 Groq API Key", "📚 AI Profile", "✅ I Accept These Rules"}:
        return await ai_menu_action(update, uid, text)
    if text == "⬅️ Back":
        state.pop(uid, None)
        context.user_data.pop("bio_kind", None)
        context.user_data.pop("bio_mode", None)
        context.user_data.pop("bio_target", None)
        await cleanup_login_flow(uid)
        await show_main_menu(update.message)
        return

    # Clean category navigation from the main page.
    if text == "📣 Promotion":
        state.pop(uid, None)
        await update.message.reply_text("📣 <b>PROMOTION</b>", parse_mode=ParseMode.HTML, reply_markup=PROMOTION_MENU)
        return
    if text == "📝 Posts":
        state.pop(uid, None)
        await update.message.reply_text(await posts_summary(), parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
        return
    if text == "👥 Accounts":
        state.pop(uid, None)
        await update.message.reply_text("👥 <b>ACCOUNTS</b>", parse_mode=ParseMode.HTML, reply_markup=ACCOUNTS_MENU)
        return
    if text == "📊 Monitoring":
        state.pop(uid, None)
        await update.message.reply_text("📊 <b>MONITORING</b>", parse_mode=ParseMode.HTML, reply_markup=MONITORING_MENU)
        return
    if text == "✏️ Edit Bio":
        state.pop(uid, None)
        context.user_data.pop("bio_kind", None)
        context.user_data.pop("bio_mode", None)
        context.user_data.pop("bio_target", None)
        await update.message.reply_text(
            "✏️ <b>EDIT / SET BIO</b>\n\nWhose bio do you want to change?",
            parse_mode=ParseMode.HTML, reply_markup=BIO_MENU,
        )
        return
    if text in {"🤖 Set Bot Bio", "👤 Set User Bio"}:
        kind = "bot" if text.startswith("🤖") else "user"
        context.user_data["bio_kind"] = kind
        context.user_data.pop("bio_mode", None)
        context.user_data.pop("bio_target", None)
        await update.message.reply_text(
            ("🤖 <b>BOT BIO</b>" if kind == "bot" else "👤 <b>USER BIO</b>") +
            "\n\nChoose whether to update one account or all active accounts.",
            parse_mode=ParseMode.HTML, reply_markup=BIO_SCOPE_MENU,
        )
        return
    if text == "👤 Individual" and context.user_data.get("bio_kind") in {"bot", "user"}:
        kind = context.user_data["bio_kind"]
        buttons = []
        if kind == "user":
            rows = await session_store.get_sessions(enabled_only=True)
            active_rows = []
            for row in rows:
                try:
                    client = await get_account_client(row)
                    if client and client.is_connected():
                        active_rows.append(row)
                except Exception:
                    continue
            for row in active_rows:
                tg_id = row.get("tg_user_id")
                if tg_id is None:
                    continue
                label = row.get("first_name") or row.get("label") or row.get("username") or str(tg_id)
                buttons.append([InlineKeyboardButton(str(label)[:40], callback_data=f"bio_pick:user:{int(tg_id)}")])
        else:
            rows = await db.list_bot_accounts(enabled_only=True)
            for row in rows:
                bid = int(row["bot_id"])
                status = BOT_ACCOUNT_STATUS.get((uid, bid), "")
                if not status.startswith("🟢"):
                    continue
                label = row.get("first_name") or ("@" + row.get("username") if row.get("username") else str(bid))
                buttons.append([InlineKeyboardButton(str(label)[:40], callback_data=f"bio_pick:bot:{bid}")])
        if not buttons:
            await update.message.reply_text(
                "❌ No active connected account available for individual bio update.",
                reply_markup=BIO_SCOPE_MENU,
            )
            return
        await update.message.reply_text(
            "Select an active connected account:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if text == "🌐 Set All" and context.user_data.get("bio_kind") in {"bot", "user"}:
        context.user_data["bio_mode"] = "all"
        context.user_data.pop("bio_target", None)
        state[uid] = "await_bio_text"
        kind_label = "bot accounts" if context.user_data["bio_kind"] == "bot" else "user accounts"
        await update.message.reply_text(
            f"Send the new bio for all active {kind_label}.\n\nSend <code>CLEAR</code> to remove the bio.",
            parse_mode=ParseMode.HTML, reply_markup=BIO_SCOPE_MENU,
        )
        return

    if current == "await_bio_text":
        kind = context.user_data.get("bio_kind")
        mode = context.user_data.get("bio_mode")
        target = context.user_data.get("bio_target")
        bio_text = "" if text.strip().upper() == "CLEAR" else text.strip()
        if kind == "bot" and len(bio_text) > 120:
            await update.message.reply_text("❌ Bot bio can be at most 120 characters. Send a shorter bio.", reply_markup=BIO_SCOPE_MENU)
            return
        if not kind or mode not in {"one", "all"}:
            state.pop(uid, None)
            await update.message.reply_text("Bio flow expired. Open ✏️ Edit Bio again.", reply_markup=MENU)
            return

        results = []
        if kind == "bot":
            rows = await db.list_bot_accounts(enabled_only=True)
            if mode == "one":
                rows = [r for r in rows if int(r.get("bot_id", 0)) == int(target or 0)]
            else:
                rows = [r for r in rows if BOT_ACCOUNT_STATUS.get((uid, int(r["bot_id"])), "").startswith("🟢")]
            for row in rows:
                bid = int(row["bot_id"])
                label = row.get("first_name") or ("@" + row.get("username") if row.get("username") else str(bid))
                try:
                    token_value = decrypt_bot_token(row["token_enc"])
                    api_bot = Bot(token=token_value)
                    await api_bot.set_my_short_description(short_description=bio_text)
                    results.append((label, True, ""))
                except Exception as exc:
                    results.append((label, False, type(exc).__name__))
        else:
            rows = await session_store.get_sessions(enabled_only=True)
            if mode == "one":
                rows = [r for r in rows if int(r.get("tg_user_id") or 0) == int(target or 0)]
            for row in rows:
                label = row.get("first_name") or row.get("label") or row.get("username") or str(row.get("tg_user_id"))
                try:
                    client = await get_account_client(row)
                    if not client.is_connected():
                        raise RuntimeError("NotConnected")
                    await client(UpdateProfileRequest(about=bio_text))
                    results.append((label, True, ""))
                except Exception as exc:
                    results.append((label, False, type(exc).__name__))

        state.pop(uid, None)
        context.user_data.pop("bio_mode", None)
        context.user_data.pop("bio_target", None)
        ok = sum(1 for _, success, _ in results if success)
        failed = [(label, err) for label, success, err in results if not success]
        lines = [f"✅ Bio updated: <b>{ok}</b>"]
        if failed:
            lines.append(f"⚠️ Failed: <b>{len(failed)}</b>")
            lines.extend(f"• {esc(label)} — <code>{esc(err)}</code>" for label, err in failed[:10])
        if not results:
            lines = ["❌ No active account matched this bio update."]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=BIO_MENU)
        return

    if current in {"await_account_reply", "await_control_reply"}:
        if text.casefold() in {"cancel", "/cancel", "❌ cancel"}:
            state.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=AUTO_REPLY_MENU)
            return
        if not text.strip() or len(text) > 4000:
            await update.message.reply_text("Send a text reply of 1–4000 characters, or Back to cancel.", reply_markup=AUTO_REPLY_MENU)
            return
        key = "account_dm_text" if current == "await_account_reply" else "control_bot_dm_text"
        await db.set_setting(key, text)
        if current == "await_account_reply":
            # Explicit text save switches off saved-post mode to avoid confusion.
            await db.set_setting("dm_use_saved_post", "false")
        state.pop(uid, None)
        await update.message.reply_text("✅ Auto-reply saved. New incoming DMs will use it.\n\n" + await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return

    if current == "await_reply_cooldown":
        try:
            minutes = int(text)
            if not 1 <= minutes <= 1440:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter whole minutes (1–1440), or Back.", reply_markup=AUTO_REPLY_MENU)
            return
        await db.set_setting("dm_reply_cooldown_seconds", str(minutes * 60))
        state.pop(uid, None)
        await update.message.reply_text("✅ Cooldown updated.\n\n" + await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return

    if text == "🤖 Auto Reply":
        state.pop(uid, None)
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text == "✏️ Account Reply":
        state[uid] = "await_account_reply"
        await update.message.reply_text("Send the auto-reply text for connected real IDs. It will be saved when received. Press Back to cancel.", reply_markup=AUTO_REPLY_MENU)
        return
    if text == "✏️ Control Bot Reply":
        state[uid] = "await_control_reply"
        await update.message.reply_text("Send the auto-reply text for people DMing this control bot. Press Back to cancel.", reply_markup=AUTO_REPLY_MENU)
        return
    if text == "👁 Reply Preview":
        a = await reply_setting("account_dm_text", ACCOUNT_DM_AUTO_REPLY_TEXT)
        b = await reply_setting("control_bot_dm_text", DM_AUTO_REPLY_TEXT)
        await update.message.reply_text(
            "👁 <b>REPLY PREVIEW</b>\n\n👤 <b>Real IDs:</b>\n<pre>" + esc(a) +
            "</pre>\n🤖 <b>Control bot:</b>\n<pre>" + esc(b) + "</pre>",
            parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU,
        )
        return
    if text == "🔄 Use Saved Post":
        old = await reply_enabled("dm_use_saved_post", DM_AUTO_REPLY_USE_SAVED_POST)
        if not old and not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=AUTO_REPLY_MENU)
            return
        await db.set_setting("dm_use_saved_post", "false" if old else "true")
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text in {"🟢 Toggle Account Reply", "🟢 Toggle Bot Reply"}:
        key, default = (("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED) if text == "🟢 Toggle Account Reply" else ("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED))
        old = await reply_enabled(key, default)
        await db.set_setting(key, "false" if old else "true")
        await update.message.reply_text(await reply_panel_text(), parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU)
        return
    if text == "📡 Reply Status":
        rows = await session_store.get_sessions(enabled_only=True)
        online = {sid for sid, client in _account_dm_clients.items() if client.is_connected()}
        account_enabled = await reply_enabled("account_dm_enabled", ACCOUNT_DM_AUTO_REPLY_ENABLED)
        control_enabled = await reply_enabled("control_bot_dm_enabled", DM_AUTO_REPLY_ENABLED)
        await update.message.reply_text(
            "📡 <b>AUTO-REPLY STATUS</b>\n\n"
            f"👤 Account replies: <b>{'ON' if account_enabled else 'OFF'}</b>\n"
            f"🤖 Control bot replies: <b>{'ON' if control_enabled else 'OFF'}</b>\n"
            f"🔌 Connected accounts: <b>{len(online)}/{len(rows)}</b>\n"
            f"⏳ DM replies queued: <b>{len(_account_reply_pending)}</b>\n"
            f"✅ Account replies sent: <b>{_reply_sent_counts['accounts']}</b>\n"
            f"✅ Control replies sent: <b>{_reply_sent_counts['control_bot']}</b>\n"
            f"⚠️ Auto-reply errors: <b>{_reply_sent_counts['failed']}</b>\n\n"
            "Queued replies follow the account's current group cooldown. "
            "If an account is FloodWait/PeerFlood paused, replies pause too.",
            parse_mode=ParseMode.HTML, reply_markup=AUTO_REPLY_MENU,
        )
        return
    if text == "⏱ Reply Cooldown":
        state[uid] = "await_reply_cooldown"
        await update.message.reply_text("Send cooldown in minutes (1–1440).", reply_markup=AUTO_REPLY_MENU)
        return

    # Post-management flow. Back was already processed at the top of this function.
    if current in {"await_preview_post_id", "await_toggle_post_id", "await_delete_post_id"}:
        if text.casefold() in {"cancel", "/cancel", "❌ cancel"}:
            state.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=POST_MENU)
            return
        try:
            post_id = int(text.strip().lstrip("#"))
        except ValueError:
            await update.message.reply_text("Send the numeric post ID from 📋 List Posts, or press Back.", reply_markup=POST_MENU)
            return
        post = await db.get_post_by_id(post_id)
        if not post:
            await update.message.reply_text("❌ Post not found. Try an ID from the list.", reply_markup=POST_MENU)
            return
        if current == "await_preview_post_id":
            state.pop(uid, None)
            await preview_saved_post(update.message, post)
            return
        if stats.running or broadcast_running(context.application):
            await update.message.reply_text("⏹ Stop the ongoing broadcast before modifying posts.", reply_markup=POST_MENU)
            state.pop(uid, None)
            return
        if current == "await_toggle_post_id":
            if post["enabled"] and len(await db.list_posts(enabled_only=True)) <= 1:
                await update.message.reply_text("❌ Keep at least one post enabled. Add another post before disabling this one.", reply_markup=POST_MENU)
                state.pop(uid, None)
                return
            enabled = await db.toggle_post(post_id)
            state.pop(uid, None)
            await update.message.reply_text(f"✅ Post #{post_id} is now {'ON' if enabled else 'OFF'}.", reply_markup=POST_MENU)
            return
        if current == "await_delete_post_id":
            deleted = await db.delete_post(post_id)
            state.pop(uid, None)
            if deleted and deleted.get("media_path"):
                try:
                    Path(deleted["media_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            await update.message.reply_text(f"🗑 Post #{post_id} deleted.", reply_markup=POST_MENU)
            return

    if text in {"📚 Manage Posts", "👁 View Post"}:
        state.pop(uid, None)
        await update.message.reply_text(await posts_summary(), parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
        return
    if text == "📋 List Posts":
        await update.message.reply_text(await posts_summary(), parse_mode=ParseMode.HTML, reply_markup=POST_MENU)
        return
    if text in {"👁 Preview Post", "🔘 Toggle Post", "🗑 Delete Post"}:
        state[uid] = {"👁 Preview Post":"await_preview_post_id", "🔘 Toggle Post":"await_toggle_post_id", "🗑 Delete Post":"await_delete_post_id"}[text]
        await update.message.reply_text("Send the post ID from 📋 List Posts. Press Back to cancel.", reply_markup=POST_MENU)
        return
    if text == "🔀 Sending Mode":
        current_mode = await db.get_setting("post_mode", "rotate")
        await update.message.reply_text(f"Current mode: {current_mode.title()}\n\nChoose Rotate or Random.", reply_markup=POST_MODE_MENU)
        return
    if text in {"🔄 Rotate", "🎲 Random"}:
        new_mode = "rotate" if text == "🔄 Rotate" else "random"
        await db.set_setting("post_mode", new_mode)
        await update.message.reply_text(f"✅ Sending mode: {new_mode.title()}. Changes apply on the next round.", reply_markup=POST_MENU)
        return

    # Other conversation states follow.

    if current == "choose_add_method":
        if text == "🔑 Session String":
            state[uid] = "await_session"
            await update.message.reply_text(
                "Send the Telethon <b>StringSession</b>.\n\nIt will be validated, encrypted, and stored in MongoDB.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        elif text == "📱 Login with Phone":
            state[uid] = "await_phone"
            await update.message.reply_text(
                "📱 Send the phone number with country code.\nExample: <code>+919876543210</code>\n\n⚠️ The OTP will be entered in this private bot chat. OTP and 2FA password are never stored in the database.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        else:
            await update.message.reply_text("Choose one of the two options below.", reply_markup=ADD_SESSION_MENU)
        return

    if current == "await_session":
        state.pop(uid, None)
        await safe_delete_message(update.message)
        try:
            session_string = clean_session_string(text)
        except ValueError as e:
            state[uid] = "await_session"   # let the user paste again
            await context.bot.send_message(
                uid, f"❌ <b>SESSION NOT READY</b>\n\n{esc(str(e))}",
                parse_mode=ParseMode.HTML, reply_markup=ADD_SESSION_MENU)
            return
        status_msg = await context.bot.send_message(uid, "⏳ Validating session...")
        client = None
        me = None
        stored_sid = None
        try:
            # Validate once, then promote THIS SAME connection into the shared
            # runtime client. This prevents validation/listener overlap for one auth key.
            client = await make_client(session_string)
            me = await client.get_me()
            label, stored_sid, _stored_row, shared = await _register_validated_session(
                client, me, session_string
            )
            client = None  # ownership moved to shared pool; do not disconnect in finally
            connected = bool(shared and shared.is_connected())
            username = f"@{me.username}" if me.username else "—"

            try:
                await status_msg.delete()
            except Exception:
                pass

            await context.bot.send_message(
                uid,
                "✅ <b>SESSION CONNECTED</b>\n\n"
                f"👤 <b>Account:</b> {esc(label)}\n"
                f"🆔 <b>Telegram ID:</b> <code>{me.id}</code>\n"
                f"🔗 <b>Username:</b> {esc(username)}\n"
                f"📡 <b>Status:</b> {'🟢 CONNECTED' if connected else '🟡 SAVED / CONNECTING'}\n\n"
                "Promotion, listeners, auto-reply and group updates will use this account's shared connection.",
                parse_mode=ParseMode.HTML,
                reply_markup=MENU,
            )
        except Exception as e:
            # If validation succeeded but live shared connection failed, keep the
            # saved record and clearly tell the operator what happened.
            detail = esc(type(e).__name__ + ': ' + str(e))
            try:
                await status_msg.edit_text(
                    "❌ <b>SESSION NOT READY</b>\n\n"
                    f"<code>{detail}</code>\n\n"
                    "If this is a temporary connection error, try again. If it is AuthKeyDuplicatedError, create a fresh session.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await context.bot.send_message(
                    uid,
                    "❌ <b>SESSION NOT READY</b>\n\n"
                    f"<code>{detail}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=MENU,
                )
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return

    if current == "await_phone":
        phone = text.strip().replace(" ", "")
        await safe_delete_message(update.message)
        if not phone.startswith("+") or not phone[1:].isdigit():
            await context.bot.send_message(uid, "❌ Invalid number. Send it with country code, e.g. <code>+919876543210</code>.", parse_mode=ParseMode.HTML, reply_markup=ADD_SESSION_MENU)
            return
        await cleanup_login_flow(uid)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            login_flows[uid] = {"client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash}
            state[uid] = "await_otp"
            await context.bot.send_message(
                uid,
                "✅ Login code sent by Telegram.\n\nSend the OTP here. You can type it normally, e.g. <code>12345</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=ADD_SESSION_MENU,
            )
        except Exception as e:
            await client.disconnect()
            state.pop(uid, None)
            await context.bot.send_message(uid, f"❌ Could not send OTP:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_otp":
        code = ''.join(ch for ch in text if ch.isdigit())
        await safe_delete_message(update.message)
        flow = login_flows.get(uid)
        if not flow:
            state.pop(uid, None)
            await context.bot.send_message(uid, "❌ Login session expired. Start Add Session again.", reply_markup=MENU)
            return
        if not code:
            await context.bot.send_message(uid, "❌ Send the numeric OTP.", reply_markup=ADD_SESSION_MENU)
            return
        client = flow["client"]
        try:
            await client.sign_in(phone=flow["phone"], code=code, phone_code_hash=flow["phone_code_hash"])
            label, group_count, user_count, stored_sid = await save_logged_in_client(client)
            state.pop(uid, None)
            flow["client"] = None  # shared pool owns it now
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n🎯 Target cache will be built on first Start Promo or manual Rescan Targets.", parse_mode=ParseMode.HTML, reply_markup=MENU)
        except errors.SessionPasswordNeededError:
            state[uid] = "await_2fa"
            await context.bot.send_message(uid, "🔐 Two-step verification is enabled. Send your Telegram 2FA password.", reply_markup=ADD_SESSION_MENU)
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
            if isinstance(e, errors.PhoneCodeExpiredError):
                state.pop(uid, None)
                await cleanup_login_flow(uid)
                await context.bot.send_message(uid, "❌ OTP expired. Start Add Session again to request a new code.", reply_markup=MENU)
            else:
                await context.bot.send_message(uid, "❌ Wrong OTP. Try again or press ⬅️ Back.", reply_markup=ADD_SESSION_MENU)
        except Exception as e:
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"❌ Login failed:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_2fa":
        password = text
        await safe_delete_message(update.message)
        flow = login_flows.get(uid)
        if not flow:
            state.pop(uid, None)
            await context.bot.send_message(uid, "❌ Login session expired. Start Add Session again.", reply_markup=MENU)
            return
        client = flow["client"]
        try:
            await client.sign_in(password=password)
            label, group_count, user_count, stored_sid = await save_logged_in_client(client)
            state.pop(uid, None)
            flow["client"] = None  # shared pool owns it now
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"✅ Login successful and session saved.\n\n👤 {esc(label)}\n🎯 Target cache will be built on first Start Promo or manual Rescan Targets.", parse_mode=ParseMode.HTML, reply_markup=MENU)
        except errors.PasswordHashInvalidError:
            await context.bot.send_message(uid, "❌ Wrong 2FA password. Try again or press ⬅️ Back.", reply_markup=ADD_SESSION_MENU)
        except Exception as e:
            state.pop(uid, None)
            await cleanup_login_flow(uid)
            await context.bot.send_message(uid, f"❌ Login failed:\n<code>{esc(type(e).__name__ + ': ' + str(e))}</code>", parse_mode=ParseMode.HTML, reply_markup=MENU)
        return

    if current == "await_join_links":
        raw_lines = [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]
        parsed = []
        invalid = []
        seen = set()
        for raw in raw_lines:
            item = normalize_join_link(raw)
            if not item:
                invalid.append(raw)
                continue
            original, normalized, _kind, _value = item
            if normalized in seen:
                continue
            seen.add(normalized)
            parsed.append((original, normalized))
        if not parsed:
            await update.message.reply_text(
                "❌ No valid Telegram group links found.\n\nSend one link per line, for example:\nhttps://t.me/publicgroup\nhttps://t.me/+PrivateInviteHash",
                reply_markup=JOIN_GROUPS_MENU,
            )
            return
        rows = await session_store.get_sessions(enabled_only=True)
        if not rows:
            await update.message.reply_text("❌ No active real-user sessions are connected.", reply_markup=ACCOUNTS_MENU)
            state.pop(uid, None)
            return
        added, existing = await db.enqueue_join_links(rows, parsed)
        state.pop(uid, None)
        await start_join_queue_service(context.application, uid)
        lines = [
            "✅ <b>JOIN QUEUE UPDATED</b>",
            f"Links accepted: <b>{len(parsed)}</b>",
            f"Active accounts: <b>{len(rows)}</b>",
            f"New account/link jobs: <b>{added}</b>",
            f"Already queued/history: <b>{existing}</b>",
            "",
            f"Each connected ID attempts at most <b>1 group every {JOIN_QUEUE_INTERVAL_SECONDS // 60} minutes</b>.",
        ]
        if invalid:
            lines.append(f"⚠️ Invalid/unsupported lines skipped: <b>{len(invalid)}</b>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=JOIN_GROUPS_MENU)
        return

    if current == "await_bot_token":
        token_raw = text.strip()
        await safe_delete_message(update.message)
        try:
            if token_raw == BOT_TOKEN:
                raise ValueError("The control bot token cannot be added as a Bot Account.")
            test_bot = Bot(token=token_raw)
            await test_bot.initialize()
            try:
                me = await test_bot.get_me()
            finally:
                await test_bot.shutdown()
            if not me.is_bot:
                raise ValueError("This token does not belong to a bot account.")
            enc = fernet.encrypt(token_raw.encode()).decode()
            await db.add_bot_account(me.id, me.username, me.first_name, enc)
            state.pop(uid, None)
            await start_bot_account_listener(context.application, uid, me.id)
            await context.bot.send_message(
                uid,
                f"✅ <b>BOT ACCOUNT ADDED</b>\n\n"
                f"🤖 {esc(me.first_name or 'Bot')}\n"
                f"👤 @{esc(me.username or 'no_username')}\n"
                f"🆔 <code>{me.id}</code>\n\n"
                "DM auto-reply is also active and uses the same saved promotion posts/cooldown.\n\n"
                "For replies to every normal group message while the bot is not admin, disable Privacy Mode in @BotFather → /setprivacy for this bot. "
                "The bot must still be a member and allowed to send messages.",
                parse_mode=ParseMode.HTML,
                reply_markup=BOT_ACCOUNT_MENU,
            )
        except Exception as exc:
            state[uid] = "await_bot_token"
            await context.bot.send_message(
                uid,
                f"❌ Bot token validation failed: <code>{esc(type(exc).__name__ + ': ' + str(exc))}</code>\n\nSend a valid bot token or press Back.",
                parse_mode=ParseMode.HTML,
                reply_markup=BOT_ACCOUNT_MENU,
            )
        return

    if current == "await_bot_group_reply_cooldown":
        try:
            minutes = int(text.strip())
            if not 1 <= minutes <= 1440:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Send cooldown in whole minutes (1–1440).", reply_markup=BOT_ACCOUNT_MENU)
            return
        await db.set_setting("bot_group_reply_cooldown_seconds", str(minutes * 60))
        state.pop(uid, None)
        await update.message.reply_text(
            f"✅ Bot auto-reply cooldown set to {minutes} minute(s) for both group replies and DMs.",
            reply_markup=BOT_ACCOUNT_MENU,
        )
        return

    if current == "await_remove_bot":
        try:
            bot_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("Send the numeric bot ID shown in Bot Accounts.", reply_markup=BOT_ACCOUNT_MENU)
            return
        row = await db.get_bot_account(bot_id)
        if not row:
            await update.message.reply_text("❌ Bot account not found.", reply_markup=BOT_ACCOUNT_MENU)
            return
        await stop_bot_account_listener(uid, bot_id)
        await db.delete_bot_account(bot_id)
        state.pop(uid, None)
        await update.message.reply_text(f"✅ Bot @{row.get('username') or bot_id} removed.", reply_markup=BOT_ACCOUNT_MENU)
        return

    if current == "await_group_delay":
        try:
            seconds = int(text.strip())
            if not 15 <= seconds <= 600:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Send a whole number of seconds (15–600), or press Back.", reply_markup=MENU)
            return
        await db.set_setting("group_delay_seconds", str(seconds))
        stats.group_delay_seconds = seconds
        state.pop(uid, None)
        await update.message.reply_text(f"✅ Minimum group delay set to {seconds} seconds. Applies next round.", reply_markup=MENU)
        return

    if current == "await_interval":
        state.pop(uid, None)
        try:
            minutes = int(text.strip())
            if minutes < MIN_AD_INTERVAL_MINUTES:
                raise ValueError
            await db.set_setting("interval_minutes", str(minutes))
            await update.message.reply_text(f"✅ Interval set to {minutes} minutes.", reply_markup=MENU)
        except ValueError:
            await update.message.reply_text(f"❌ Send a whole number of minutes (minimum {MIN_AD_INTERVAL_MINUTES}).", reply_markup=MENU)
        return

    if current == "await_quiet":
        state.pop(uid, None)
        value = text.strip()
        if value.lower() in ("off", "0", "none"):
            await db.set_setting("quiet_hours", "")
            QUIET_CACHE.pop(tenant_id(), None)
            return await update.message.reply_text("✅ Quiet hours turned off.", reply_markup=MENU)
        try:
            parse_quiet_window(value)
        except ValueError:
            return await update.message.reply_text(
                "❌ Use HH:MM-HH:MM, for example 23:00-07:00.", reply_markup=MENU)
        await db.set_setting("quiet_hours", value)
        QUIET_CACHE.pop(tenant_id(), None)
        await update.message.reply_text(
            f"✅ Quiet hours set to {value} (IST). No promotions run in this window.", reply_markup=MENU)
        return

    if current == "await_remove":
        state.pop(uid, None)
        try:
            session_ref = text.strip()
            deleted = await session_store.delete_session(session_ref)
            target_cache.clear()
            if not deleted:
                raise ValueError("Session not found")
            await subs_core.log_event(f"🗑 Account removed · operator <code>{tenant_id()}</code> · {esc(session_ref)}")
            await update.message.reply_text(f"✅ Session {session_ref} removed.", reply_markup=MENU)
        except Exception:
            await update.message.reply_text("❌ Send the Mongo session ID shown in Sessions, or its numeric Telegram user ID.", reply_markup=ACCOUNTS_MENU)
        return

    if text == "🤖 Bot Accounts":
        enabled = (await db.get_setting("bot_group_reply_enabled", "true")).lower() in {"true", "1", "yes", "on"}
        cooldown = await bot_group_reply_cooldown_for(uid)
        rows = await db.list_bot_accounts()
        await update.message.reply_text(
            "🤖 <b>BOT ACCOUNT MODE</b>\n\n"
            f"Auto reply (Groups + DMs): <b>{'ON' if enabled else 'OFF'}</b>\n"
            f"Cooldown: <b>{cooldown // 60} min</b>\n"
            f"Connected bot accounts: <b>{len(rows)}</b>\n\n"
            "Replies use the same enabled promotion posts saved in 📝 Posts and follow the current Rotate/Random post mode.\n"
            "• Group: one reply per bot + group + user per cooldown\n"
            "• DM: one reply per bot + user per cooldown\n\n"
            "A bot does not need to be group admin to send a reply if normal sending is allowed. "
            "For a non-admin bot to receive ordinary group messages, disable Privacy Mode in @BotFather → /setprivacy. "
            "If status shows POLLING CONFLICT, the same bot token is still running on another server/process.",
            parse_mode=ParseMode.HTML,
            reply_markup=BOT_ACCOUNT_MENU,
        )
        return

    if text == "➕ Add Bot":
        state[uid] = "await_bot_token"
        await update.message.reply_text(
            "Send the Telegram bot token from @BotFather.\n\n"
            "It will be validated, encrypted with the existing FERNET_KEY, and stored in this operator's MongoDB scope.",
            reply_markup=BOT_ACCOUNT_MENU,
        )
        return

    if text == "👤 Bot Accounts":
        rows = await db.list_bot_accounts()
        if not rows:
            await update.message.reply_text("No bot accounts added.", reply_markup=BOT_ACCOUNT_MENU)
            return
        lines = ["🤖 <b>BOT ACCOUNTS</b>\n"]
        for row in rows:
            bid = int(row["bot_id"])
            status = BOT_ACCOUNT_STATUS.get((uid, bid), "🟡 STARTING" if row.get("enabled", True) else "⚪ DISABLED")
            uname = f"@{row.get('username')}" if row.get("username") else "no username"
            gcount = await db.count_bot_groups(bid)
            bst = await db.bot_event_stats(bid)
            lines.append(
                f"{status} • <b>{esc(row.get('first_name') or uname)}</b>\n"
                f"{esc(uname)} • ID <code>{bid}</code>\n"
                f"👥 Known groups: <b>{gcount}</b> • ✅ Replies: <b>{bst['sent']}</b>"
            )
        await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=BOT_ACCOUNT_MENU)
        return

    if text == "📊 Bot Stats":
        rows = await db.list_bot_accounts()
        if not rows:
            await update.message.reply_text("No bot accounts added.", reply_markup=BOT_ACCOUNT_MENU)
            return
        blocks = ["📊 <b>BOT ACCOUNT STATS</b>"]
        for row in rows:
            bid = int(row["bot_id"])
            label = row.get("first_name") or ("@" + row.get("username") if row.get("username") else str(bid))
            status = BOT_ACCOUNT_STATUS.get((uid, bid), "🟡 STARTING" if row.get("enabled", True) else "⚪ DISABLED")
            gcount = await db.count_bot_groups(bid)
            st = await db.bot_event_stats(bid)
            blocks.append(
                f"{status} <b>{esc(label)}</b>\n"
                f"👥 Known groups: <b>{gcount}</b>\n"
                f"✅ Replies: <b>{st['sent']}</b> (Groups {st['group_sent']} • DMs {st['dm_sent']})\n"
                f"⏭ Skipped: <b>{st['skipped']}</b> • ❌ Failed: <b>{st['failed']}</b>"
            )
        await update.message.reply_text("\n\n".join(blocks), parse_mode=ParseMode.HTML, reply_markup=BOT_ACCOUNT_MENU)
        return

    if text == "📜 Bot Logs":
        rows = await db.list_bot_accounts()
        if not rows:
            await update.message.reply_text("No bot accounts added.", reply_markup=BOT_ACCOUNT_MENU)
            return
        names = {int(r['bot_id']): (r.get('username') or r.get('first_name') or str(r['bot_id'])) for r in rows}
        events = await db.list_bot_events(limit=25)
        if not events:
            await update.message.reply_text("📜 No bot-account events recorded yet.", reply_markup=BOT_ACCOUNT_MENU)
            return
        lines = ["📜 <b>RECENT BOT LOGS</b>"]
        for ev in reversed(events):
            ts = ev.get('created_at')
            when = ts.strftime('%H:%M:%S') if ts else '--:--:--'
            bid = int(ev.get('bot_id', 0))
            label = names.get(bid, str(bid))
            detail = ev.get('detail') or ''
            lines.append(
                f"<code>{when}</code> • <b>{esc(label)}</b> • {esc(ev.get('scope','-'))} • "
                f"<b>{esc(ev.get('event','-').upper())}</b>" + (f" • {esc(detail)[:100]}" if detail else "")
            )
        text_out = "\n".join(lines)
        if len(text_out) > 3900:
            text_out = text_out[-3900:]
            text_out = "📜 <b>RECENT BOT LOGS</b>\n" + text_out.split('\n',1)[-1]
        await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=BOT_ACCOUNT_MENU)
        return

    if text in {"🟢 Toggle Group Reply", "🟢 Toggle Bot Auto Reply"}:
        enabled = (await db.get_setting("bot_group_reply_enabled", "true")).lower() in {"true", "1", "yes", "on"}
        await db.set_setting("bot_group_reply_enabled", "false" if enabled else "true")
        await update.message.reply_text(
            f"✅ Bot auto reply (Groups + DMs) is now {'OFF' if enabled else 'ON'}.",
            reply_markup=BOT_ACCOUNT_MENU,
        )
        return

    if text in {"⏱ Group Reply Cooldown", "⏱ Bot Reply Cooldown"}:
        current_cd = await bot_group_reply_cooldown_for(uid)
        state[uid] = "await_bot_group_reply_cooldown"
        await update.message.reply_text(
            f"Current cooldown: {current_cd // 60} minute(s).\n\n"
            "Send new cooldown in minutes.\n"
            "Group: same bot + group + user gets one reply per window.\n"
            "DM: same bot + user gets one reply per window.",
            reply_markup=BOT_ACCOUNT_MENU,
        )
        return

    if text == "🗑 Remove Bot":
        rows = await db.list_bot_accounts()
        if not rows:
            await update.message.reply_text("No bot accounts to remove.", reply_markup=BOT_ACCOUNT_MENU)
            return
        state[uid] = "await_remove_bot"
        lines = ["Send the numeric bot ID to remove:"] + [
            f"{int(r['bot_id'])} — @{r.get('username') or 'no_username'}" for r in rows
        ]
        await update.message.reply_text("\n".join(lines), reply_markup=BOT_ACCOUNT_MENU)
        return

    if text == "🔗 Join Groups":
        state.pop(uid, None)
        st = await db.join_queue_stats()
        await update.message.reply_text(
            "🔗 <b>JOIN GROUPS</b>\n\n"
            f"Pending: <b>{st['pending']}</b> • Joined: <b>{st['joined']}</b>\n"
            f"Requests sent: <b>{st['requested']}</b> • Already joined: <b>{st['already']}</b>\n"
            f"Failed: <b>{st['failed']}</b>\n\n"
            f"Each connected real-user ID processes at most one queued link every {JOIN_QUEUE_INTERVAL_SECONDS // 60} minutes.",
            parse_mode=ParseMode.HTML, reply_markup=JOIN_GROUPS_MENU,
        )
        return

    if text == "➕ Add Group Links":
        rows = await session_store.get_sessions(enabled_only=True)
        if not rows:
            await update.message.reply_text("❌ Add/activate a real-user session first.", reply_markup=ACCOUNTS_MENU)
            return
        state[uid] = "await_join_links"
        await update.message.reply_text(
            "Send Telegram group/channel links now. One per line.\n\n"
            "Examples:\n"
            "<code>https://t.me/publicgroup</code>\n"
            "<code>@publicgroup</code>\n"
            "<code>https://t.me/+AbCdEf123456</code>\n\n"
            "The same list will be queued for every currently active connected real-user ID.",
            parse_mode=ParseMode.HTML, reply_markup=JOIN_GROUPS_MENU,
        )
        return

    if text == "📋 Join Queue":
        st = await db.join_queue_stats()
        recent = await db.list_recent_join_items(20)
        lines = [
            "📋 <b>JOIN QUEUE</b>",
            f"Pending <b>{st['pending']}</b> • Joined <b>{st['joined']}</b> • Requested <b>{st['requested']}</b>",
            f"Already <b>{st['already']}</b> • Failed <b>{st['failed']}</b>",
        ]
        if recent:
            lines.append("\n<b>Recent:</b>")
            icons = {"pending":"⏳", "joined":"✅", "requested":"📝", "already":"☑️", "failed":"❌"}
            for item in recent[:15]:
                label = item.get("label") or str(item.get("tg_user_id") or "Account")
                status = item.get("status", "pending")
                link = item.get("link", "")
                detail = item.get("detail", "")
                lines.append(f"{icons.get(status,'•')} {esc(label)} • {esc(link)[:70]}" + (f" • {esc(detail)[:60]}" if detail else ""))
        await update.message.reply_text("\n".join(lines)[:3900], parse_mode=ParseMode.HTML, reply_markup=JOIN_GROUPS_MENU)
        return

    if text == "🗑 Clear Pending Joins":
        result = await db.clear_pending_join_items()
        await update.message.reply_text(
            f"✅ Cleared <b>{int(result.deleted_count)}</b> pending join job(s). Completed history was kept.",
            parse_mode=ParseMode.HTML, reply_markup=JOIN_GROUPS_MENU,
        )
        return

    if text == "➕ Add Session":
        caps = await plan_caps(uid)
        if caps["max_accounts"] is not None:
            existing = await session_store.get_sessions(enabled_only=False)
            if len(existing) >= caps["max_accounts"]:
                return await update.message.reply_text(
                    f"🔒 Your plan allows {caps['max_accounts']} account(s). Remove one or ask the admin to upgrade.",
                    reply_markup=ACCOUNTS_MENU)
        state[uid] = "choose_add_method"
        await update.message.reply_text(
            "How do you want to add the Telegram account?",
            reply_markup=ADD_SESSION_MENU,
        )

    elif text == "🎯 Promo Accounts":
        await show_promo_accounts(update.message)
        return
    elif text == "👤 Sessions":
        rows = await session_store.get_sessions()
        if not rows:
            await update.message.reply_text("No sessions added.", reply_markup=MENU)
            return
        lines = ["👤 <b>SESSIONS</b>\n"]
        buttons = []
        for r in rows:
            sid = str(r["id"])
            gcount = stats.cached_group_count.get(r["id"], "?")
            ucount = stats.cached_user_count.get(r["id"], "?")
            uname = f"@{r['username']}" if r.get("username") else "no username"
            auth_state = r.get("auth_state", "active")
            enabled = bool(r.get("enabled", True))
            if not enabled:
                status = "⚪ DISABLED"
            elif auth_state == "needs_relogin":
                status = "🔐 NEEDS RELOGIN"
            else:
                status = "🟢 ACTIVE"
            display_name = r['first_name'] or r['label'] or str(r.get('tg_user_id') or sid)
            lines.append(
                f"#{sid} • {esc(display_name)} • {esc(uname)}\n"
                f"{status} • groups: {gcount} • users: {ucount}"
            )
            buttons.append([InlineKeyboardButton(
                f"🗑 Remove {str(display_name)[:24]}",
                callback_data=f"sess_rm:{sid}",
            )])
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif text == "🗑 Remove Session":
        rows = await session_store.get_sessions()
        if not rows:
            await update.message.reply_text("No sessions to remove.", reply_markup=MENU)
            return
        state[uid] = "await_remove"
        lines = ["Send the session ID to remove (Mongo ID or Telegram user ID):"] + [f"{r['id']} — {r['label']} (TG: {r.get('tg_user_id')})" for r in rows]
        await update.message.reply_text("\n".join(lines), reply_markup=MENU)

    elif text == "📝 Save Post":
        state[uid] = "await_post"
        await update.message.reply_text(
            "Send another promotion post now. Existing posts will not be replaced.\n\nSupported: text, photo, video, document, animation. Use Back to cancel.",
            reply_markup=POST_MENU,
        )

    elif text == "⏳ Group Delay":
        current_delay = await get_group_delay()
        state[uid] = "await_group_delay"
        await update.message.reply_text(
            f"Current minimum delay: {current_delay} seconds between group sends on each ID.\n\n"
            "Send a new value in seconds (15–600). Lower delay does not guarantee Telegram acceptance.",
            reply_markup=MENU,
        )

    elif text == "🌙 Quiet Hours":
        current = (await db.get_setting("quiet_hours", "")) or "off"
        state[uid] = "await_quiet"
        await update.message.reply_text(
            f"Current quiet hours: {current} (IST).\n\nSend a daily window like 23:00-07:00. "
            "Promotions pause during it. Send off to disable.",
            reply_markup=MENU,
        )
    elif text == "⏱ Interval":
        current_interval = await get_ad_interval_minutes()
        state[uid] = "await_interval"
        await update.message.reply_text(
            f"Current interval: {current_interval} min.\n\nSend new interval in minutes (minimum {MIN_AD_INTERVAL_MINUTES}).",
            reply_markup=MENU,
        )

    elif text == "🔄 Rescan Targets":
        rows = await session_store.get_sessions(enabled_only=True)
        if not rows:
            await update.message.reply_text("No sessions added.", reply_markup=MENU)
            return
        msg = await update.message.reply_text("⏳ Refreshing recurring group target cache...")
        out = []
        total_groups = 0
        total_users = 0
        for r in rows:
            client = None
            try:
                client = await get_account_client(r)
                groups, users, _ = await get_cached_targets(r, client, force=True)
                total_groups += len(groups)
                total_users += len(users)
                out.append(f"✅ {r['label']}: {len(groups)} groups")
            except Exception as e:
                out.append(f"❌ {r['label']}: {type(e).__name__}")
            finally:
                # Managed client remains connected for the DM listener.
                pass
        scan_text = (
            "🔄 <b>SCAN COMPLETE</b>\n\n"
            + "\n".join(map(esc, out))
            + f"\n\n👥 Groups: <b>{total_groups}</b>"
        )
        try:
            await msg.edit_text(scan_text, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            log.warning("Scan status message could not be edited: %s", type(e).__name__)
            await update.message.reply_text(scan_text, parse_mode=ParseMode.HTML, reply_markup=MENU)

    elif text == "▶️ Start Promo":
        if (PROMO_TASKS.get(uid) and not PROMO_TASKS[uid].done()) or broadcast_running(context.application):
            await update.message.reply_text("🟢 A broadcast is already running.", reply_markup=MENU)
            return
        if not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=MENU)
            return
        active_sessions = await session_store.get_sessions(enabled_only=True)
        if not active_sessions:
            all_sessions = await session_store.get_sessions(enabled_only=False)
            if all_sessions:
                relogin_count = sum(1 for r in all_sessions if r.get("auth_state") == "needs_relogin")
                disabled_count = sum(1 for r in all_sessions if not r.get("enabled", True))
                details = []
                if relogin_count:
                    details.append(f"🔐 Needs relogin: <b>{relogin_count}</b>")
                if disabled_count:
                    details.append(f"⚪ Disabled: <b>{disabled_count}</b>")
                detail_text = "\n".join(details) or "No session is currently usable."
                await update.message.reply_text(
                    "❌ <b>No active session available for promotion.</b>\n\n"
                    f"Stored sessions: <b>{len(all_sessions)}</b>\n"
                    f"{detail_text}\n\n"
                    "Open <b>👤 Sessions</b> to see each account status. Re-add only accounts marked <b>NEEDS RELOGIN</b>.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=MENU,
                )
            else:
                await update.message.reply_text("❌ Add at least one session first.", reply_markup=MENU)
            return
        stats.running = True
        PROMO_TASKS[uid] = asyncio.create_task(scheduler(context.application), name=f"promo_scheduler_{uid}")
        await update.message.reply_text("▶️ Promotion started. Accounts run concurrently. Each account gets its own progress message; use 📊 Account Stats and 📜 Account Logs.", reply_markup=MENU)

    elif text == "⏹ Stop Promo":
        stats.running = False
        if PROMO_TASKS.get(uid) and not PROMO_TASKS[uid].done():
            PROMO_TASKS[uid].cancel()
            try:
                await PROMO_TASKS[uid]
            except asyncio.CancelledError:
                pass
        PROMO_TASKS.pop(uid, None)
        await update.message.reply_text("⏹ Promotion stopped.", reply_markup=MENU)

    elif text == "📨 DM Broadcast":
        if broadcast_running(context.application):
            await update.message.reply_text("🟢 Manual DM broadcast is already running.", reply_markup=MENU)
            return
        if stats.running:
            await update.message.reply_text("⏹ Stop the recurring group promotion before running a one-time DM broadcast.", reply_markup=MENU)
            return
        if not await db.get_post():
            await update.message.reply_text("❌ Save a promotion post first.", reply_markup=MENU)
            return
        if not await session_store.get_sessions(enabled_only=True):
            await update.message.reply_text("❌ Add at least one session first.", reply_markup=MENU)
            return
        await update.message.reply_text("📨 Manual DM broadcast started. Each account gets its own progress message. See 📊 Account Stats or 📜 Account Logs.", reply_markup=MENU)
        set_broadcast_running(context.application, True)
        set_broadcast_task(context.application, asyncio.create_task(
            dm_broadcast_runner(context.application), name=f"one_time_dm_broadcast_{uid}"))

    elif text == "📊 Account Stats":
        await send_account_telemetry(update, 'stats')

    elif text == "📜 Account Logs":
        await send_account_telemetry(update, 'logs')

    elif text == "📅 Schedule":
        await update.message.reply_text(await schedule_text(), parse_mode=ParseMode.HTML, reply_markup=PROMOTION_MENU)

    elif text == "📊 Live Stats":
        stats.active_posts = len(await db.list_posts(enabled_only=True))
        stats.post_mode = await db.get_setting("post_mode", "rotate")
        await update.message.reply_text(stats_text(), parse_mode=ParseMode.HTML, reply_markup=MENU)

    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=MENU)


class _CallbackMessage:
    """Lets handle_menu run from an inline button press. Navigation edits the menu in place;
    other replies replace the menu message so only one menu stays on screen."""

    def __init__(self, query, label):
        self._q = query
        self._m = query.message
        self.text = label
        self.message_id = -1
        self.chat = query.message.chat
        self.edit_in_place = label in NAV_LABELS

    def __getattr__(self, name):
        return getattr(self._m, name)

    async def reply_text(self, text, **kw):
        kw.pop("reply_to_message_id", None)
        markup = kw.get("reply_markup")
        if isinstance(markup, ReplyKeyboardRemove):
            kw.pop("reply_markup")
            markup = None
        inline = isinstance(markup, InlineKeyboardMarkup)
        if self.edit_in_place and inline:
            try:
                return await self._m.edit_text(text, **kw)
            except Exception as e:
                if "not modified" in str(e).lower():
                    return self._m
        if inline:
            try:
                await self._m.delete()
            except Exception:
                pass
        return await self._m.reply_text(text, **kw)


async def _route_label(update: Update, context, label: str):
    query = update.callback_query
    msg = _CallbackMessage(query, label)
    proxy = SimpleNamespace(effective_user=query.from_user, effective_chat=query.message.chat, message=msg)
    await handle_menu(proxy, context)


async def _sub_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """NEW: 'Back' from the purchase screen -> existing main menu (approved users only)."""
    async def _go(u, c):
        await _route_label(u, c, "⬅️ Back")
    await authorized_handler(_go)(update, context)


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    label = MAIN_CALLBACK_LABELS.get(query.data)
    if label and query.message:
        await _route_label(update, context, label)


async def submenu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    label = SUBMENU_LABELS.get(query.data)
    if not label or not query.message:
        return await query.answer("Menu expired — send /start", show_alert=False)
    await query.answer()
    await _route_label(update, context, label)


# ---------------- Admin panel (ADMIN_ID only, persistent settings) ----------------
async def load_admin_settings():
    try:
        MAINTENANCE["on"] = (await tenant_db(ADMIN_ID).get_setting("maintenance_mode", "false")) == "true"
    except Exception as e:
        log.warning("Could not load admin settings: %s", e)


async def account_status_text() -> str:
    """Every operator's accounts: attached, connected, promotion running, flood state, last error."""
    totals = {"accounts": 0, "enabled": 0, "connected": 0, "running": 0, "relogin": 0}
    sections = []
    for uid in sorted(APPROVED_USERS):
        try:
            rows = await tenant_sessions(uid).get_sessions(enabled_only=False)
        except Exception:
            log.warning("account status: could not read sessions for %s", uid)
            sections.append(f"⚠️ Operator <code>{uid}</code>: could not read accounts")
            continue
        if not rows:
            continue
        st = tenant_stats(uid)
        head = (f"👤 <b>Operator</b> <code>{uid}</code> · {len(rows)} account(s) · "
                f"promo {'🟢 running' if st.running else '🔴 stopped'} · "
                f"⏳ flood {st.flood_waits} · paused {st.paused_sessions}")
        lines = [head]
        for r in rows:
            sid = str(r["id"])
            enabled = bool(r.get("enabled", True))
            relogin = r.get("auth_state", "active") == "needs_relogin"
            client = _account_dm_clients.get(_client_key_from_row(r))
            connected = bool(client and client.is_connected())
            totals["accounts"] += 1
            totals["enabled"] += enabled
            totals["connected"] += connected
            totals["relogin"] += relogin
            name = esc(r.get("first_name") or r.get("label") or sid)
            uname = f"@{esc(r['username'])}" if r.get("username") else "-"
            if not enabled:
                badge = "⚪ disabled"
            elif relogin:
                badge = "🔐 needs relogin"
            elif connected:
                badge = "🟢 connected"
            else:
                badge = "🔴 offline"
            promo = ACCOUNT_METRICS.get((uid, sid, "promo"))
            if promo and st.running:
                totals["running"] += 1
                work = f"▶️ promo {promo['attempted']}/{promo['total']} · ✅ {promo['sent']} · ❌ {promo['failed']}"
            elif promo:
                work = f"⏹ promo idle · ✅ {promo['sent']} sent total this run"
            else:
                work = "⏸ no promo this run"
            line = f"• {name} {uname} · {badge}\n   {work}"
            if promo and promo.get("last_error") not in (None, "", "-"):
                line += f"\n   ⚠️ {esc(promo['last_error'])[:80]}"
            lines.append(line)
        sections.append("\n".join(lines))
    summary = (f"📡 <b>ACCOUNT STATUS</b>\n\n"
               f"Operators: <b>{len(APPROVED_USERS)}</b> · Accounts: <b>{totals['accounts']}</b>\n"
               f"Enabled: <b>{totals['enabled']}</b> · 🟢 Connected: <b>{totals['connected']}</b> · "
               f"▶️ Promo running: <b>{totals['running']}</b> · 🔐 Relogin: <b>{totals['relogin']}</b>\n\n")
    body = "\n\n".join(sections) if sections else "No accounts attached yet."
    text = summary + body
    if len(text) > 3900:
        text = text[:3850].rsplit("\n", 1)[0] + "\n\n…more accounts not shown. Check each operator in the Sessions menu."
    return text


def _decrypts(value) -> bool:
    if not value:
        return False
    try:
        fernet.decrypt(value.encode())
        return True
    except Exception:
        return False


async def find_undecryptable() -> list[tuple[int, str, object]]:
    """Accounts and bot tokens that the current FERNET_KEY cannot open: (operator, kind, record)."""
    found = []
    for uid in sorted(APPROVED_USERS):
        try:
            for r in await tenant_sessions(uid).get_sessions(enabled_only=False):
                if "session_enc" in r and not _decrypts(r.get("session_enc")):
                    found.append((uid, "account", r))
            for b in await tenant_db(uid).list_bot_accounts():
                if "token_enc" in b and not _decrypts(b.get("token_enc")):
                    found.append((uid, "bot", b))
        except Exception:
            log.warning("cleanup scan failed for operator %s", uid)
    return found


def _cleanup_preview_text(found) -> tuple[str, InlineKeyboardMarkup]:
    if not found:
        return ("✅ No old accounts found. Every stored account opens with the current key.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:acc")]]))
    lines = []
    for uid, kind, rec in found[:30]:
        if kind == "account":
            lines.append(f"• account <code>{rec.get('id')}</code> {esc(rec.get('first_name') or rec.get('label') or '')} (operator <code>{uid}</code>)")
        else:
            lines.append(f"• bot <code>{rec.get('bot_id')}</code> @{esc(rec.get('username') or '-')} (operator <code>{uid}</code>)")
    more = f"\n…and {len(found) - 30} more" if len(found) > 30 else ""
    text = (f"🧹 <b>OLD ACCOUNTS THAT CANNOT BE OPENED</b>\n\n"
            f"Found <b>{len(found)}</b>. These were saved with an old FERNET_KEY and will never work again.\n\n"
            + "\n".join(lines) + more + "\n\n<b>This deletes them permanently.</b> Tap confirm only if you are sure.")
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete these permanently", callback_data="adm:cleanok")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="adm:acc")]])


async def delete_undecryptable() -> tuple[int, int]:
    found = await find_undecryptable()
    accounts = bots = 0
    for uid, kind, rec in found:
        try:
            if kind == "account":
                sid = str(rec["id"])
                await close_account_client(sid)
                if await tenant_sessions(uid).delete_session(sid):
                    accounts += 1
            else:
                bot_id = int(rec["bot_id"])
                task = BOT_ACCOUNT_TASKS.pop((uid, bot_id), None)
                if task:
                    task.cancel()
                if await tenant_db(uid).delete_bot_account(bot_id):
                    bots += 1
        except Exception:
            log.exception("could not delete an old %s for operator %s", kind, uid)
    return accounts, bots


# ---- Co-admins: owner grants sections from the admin panel. Look & Emoji, Maintenance,
# log channel and co-admin management stay with the owner. A co-admin must stay approved.
CO_ADMIN_SECTIONS = {"users": "Users", "subs": "Subs & Pay", "bc": "Broadcast", "acct": "Account Status"}
CO_ADMINS: dict[int, set] = {}
ADMIN_ACTION_SECTION = {"home": "any", "back": "any", "users": "users",
                        "acc": "acct", "cleanpre": "acct", "cleanok": "acct"}


def can(uid: int, section: str) -> bool:
    if uid == ADMIN_ID:
        return True
    return uid in APPROVED_USERS and section in CO_ADMINS.get(uid, set())


def can_open_panel(uid: int) -> bool:
    return uid == ADMIN_ID or any(can(uid, s) for s in CO_ADMIN_SECTIONS)


def admin_action_allowed(uid: int, action: str) -> bool:
    sec = ADMIN_ACTION_SECTION.get(action, "owner")
    if sec == "any":
        return can_open_panel(uid)
    if sec == "owner":
        return uid == ADMIN_ID
    return can(uid, sec)


async def save_co_admins():
    await tenant_db(ADMIN_ID).set_setting(
        "co_admins", json.dumps({str(k): sorted(v) for k, v in CO_ADMINS.items() if v}))


async def load_co_admins():
    try:
        raw = await tenant_db(ADMIN_ID).get_setting("co_admins", "{}")
        CO_ADMINS.clear()
        for k, v in json.loads(raw or "{}").items():
            CO_ADMINS[int(k)] = set(v)
    except Exception:
        log.warning("could not load co-admins; none active")


def co_admin_view(note=""):
    lines = ["👮 <b>CO-ADMINS</b>", "",
             "Owner grants sections to an approved user. Look &amp; Emoji, Maintenance and this page stay with the owner.", ""]
    rows = []
    if not CO_ADMINS:
        lines.append("No co-admins yet.")
    for uid in sorted(CO_ADMINS):
        secs = CO_ADMINS[uid]
        names = ", ".join(v for s, v in CO_ADMIN_SECTIONS.items() if s in secs) or "no sections"
        flag = "" if uid in APPROVED_USERS else " (not approved now)"
        lines.append(f"• <code>{uid}</code>{flag} · {names}")
        rows.append([InlineKeyboardButton(("✅ " if s in secs else "⬜ ") + label,
                                          callback_data=f"coa:t:{uid}:{s}")
                     for s, label in CO_ADMIN_SECTIONS.items()])
        rows.append([InlineKeyboardButton(f"🗑 Remove {uid}", callback_data=f"coa:r:{uid}")])
    rows.append([InlineKeyboardButton("➕ Add co-admin", callback_data="coa:add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:home")])
    head = (note + "\n\n") if note else ""
    return head + "\n".join(lines), InlineKeyboardMarkup(rows)


async def co_admin_callback(update, context):
    q = update.callback_query
    if not q.from_user or q.from_user.id != ADMIN_ID:
        return await q.answer("⛔ Owner only", show_alert=True)
    await q.answer()
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else "list"
    note = ""
    if act == "add":
        state[q.from_user.id] = "await_coadmin"
        return await q.edit_message_text(
            "➕ Send the Telegram user ID of an approved user to make co-admin.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="coa:list")]]))
    if act == "t" and len(parts) == 4:
        uid, sec = int(parts[2]), parts[3]
        if sec in CO_ADMIN_SECTIONS:
            secs = CO_ADMINS.setdefault(uid, set())
            secs.symmetric_difference_update({sec})
            if not secs:
                CO_ADMINS.pop(uid, None)
            await save_co_admins()
    elif act == "r" and len(parts) == 3:
        CO_ADMINS.pop(int(parts[2]), None)
        await save_co_admins()
        note = "🗑 Removed."
    text, markup = co_admin_view(note)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("co-admin view failed: %s", exc)


async def coadmin_text(update, uid: int, text: str):
    try:
        target = int((text or "").strip())
    except ValueError:
        return await update.message.reply_text("❌ Send a numeric Telegram user ID.")
    if target == ADMIN_ID:
        state.pop(uid, None)
        return await update.message.reply_text("The owner already has every section.")
    if target not in APPROVED_USERS:
        return await update.message.reply_text(
            "❌ That user is not approved yet. Approve them first, then try again.")
    CO_ADMINS.setdefault(target, set())
    await save_co_admins()
    state.pop(uid, None)
    out, markup = co_admin_view(f"✅ {target} added. Tap the sections to allow.")
    await update.message.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=markup)


def admin_markup():
    m = "ON 🔴" if MAINTENANCE["on"] else "OFF 🟢"
    return InlineKeyboardMarkup([
        [styled_button(f"🛠 Maintenance: {m}", "adm:maint")],
        [styled_button("📡 Account Status", "adm:acc")],
        [styled_button("🎨 Look & Emoji", "adm:look")],
        [styled_button("👥 Approved Users", "adm:users")],
        [styled_button("👮 Co-Admins", "coa:list")],
        [styled_button("💎 Subscriptions", "sadm:home")],  # NEW
        [styled_button("⬅️ Back", "adm:back")],
    ])


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.from_user or not can_open_panel(query.from_user.id):
        return await query.answer("⛔ Admin only", show_alert=True)
    action = query.data.split(":", 1)[1]
    if not admin_action_allowed(query.from_user.id, action):
        return await query.answer("⛔ Your admin access does not include this section.", show_alert=True)
    await query.answer()
    text = "🛡 <b>ADMIN PANEL</b>\n\nManage access and bot-wide settings."
    if action == "maint":
        MAINTENANCE["on"] = not MAINTENANCE["on"]
        await tenant_db(ADMIN_ID).set_setting("maintenance_mode", "true" if MAINTENANCE["on"] else "false")
    elif action == "users":
        ops = sorted(APPROVED_USERS - {ADMIN_ID})
        text = (f"👥 <b>APPROVED USERS</b>\n\nAdmin: <code>{ADMIN_ID}</code>\n"
                + ("\n".join(f"• <code>{u}</code>" for u in ops) if ops else "No other users.")
                + "\n\nUse /add &lt;uid&gt; and /rem &lt;uid&gt; to change access.")
    elif action == "cleanpre":
        text, markup = _cleanup_preview_text(await find_undecryptable())
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("cleanup preview failed: %s", exc)
        return
    elif action == "cleanok":
        accounts, bots = await delete_undecryptable()
        await tenant_db(ADMIN_ID).set_setting("cleanup_last", str(int(time.time())))
        text = (f"✅ Deleted <b>{accounts}</b> old account(s) and <b>{bots}</b> old bot(s).\n"
                "Ab naye accounts add karein ya chalu accounts ko check karein.")
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Account Status", callback_data="adm:acc")]]))
        except BadRequest:
            pass
        return
    elif action == "acc":
        text = await account_status_text()
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="adm:acc")],
                [InlineKeyboardButton("🧹 Clean old (undecryptable) accounts", callback_data="adm:cleanpre")],
                [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")]]))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("account status view failed: %s", exc)
        return
    elif action == "back":
        return await _route_label(update, context, "⬅️ Back")
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_markup())
    except Exception:
        pass


async def save_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    if state.get(uid) != "await_post":
        return
    m = update.message
    if m.text in {"⬅️ Back", "❌ Cancel"}:
        state.pop(uid, None)
        return

    html_text = m.text_html or m.caption_html or ""
    media_path = None
    media_type = None
    kind = "text"

    try:
        if m.photo:
            kind = media_type = "photo"
            tgfile = await m.photo[-1].get_file()
            media_path = str(tenant_media_dir() / f"promo_{int(time.time()*1000)}_{m.message_id}.jpg")
            await tgfile.download_to_drive(media_path)
        elif m.video:
            kind = media_type = "video"
            tgfile = await m.video.get_file()
            suffix = Path(m.video.file_name or "video.mp4").suffix or ".mp4"
            media_path = str(tenant_media_dir() / f"promo_{int(time.time()*1000)}_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.document:
            kind = media_type = "document"
            tgfile = await m.document.get_file()
            suffix = Path(m.document.file_name or "file.bin").suffix or ".bin"
            media_path = str(tenant_media_dir() / f"promo_{int(time.time()*1000)}_{m.message_id}{suffix}")
            await tgfile.download_to_drive(media_path)
        elif m.animation:
            kind = media_type = "animation"
            tgfile = await m.animation.get_file()
            media_path = str(tenant_media_dir() / f"promo_{int(time.time()*1000)}_{m.message_id}.mp4")
            await tgfile.download_to_drive(media_path)
        elif not m.text:
            await m.reply_text("❌ Unsupported post type. Send text/photo/video/document/animation.", reply_markup=MENU)
            return

        post_id = await db.add_post(kind, html_text, media_path, media_type)
        state.pop(uid, None)
        context.user_data["post_saved_message_id"] = m.message_id
        await m.reply_text(f"✅ Post #{post_id} added. All existing posts are retained.\n\nSelect 🔀 Sending Mode to choose Rotate or Random.", reply_markup=POST_MENU)
    except Exception as e:
        await m.reply_text(f"❌ Could not save post:\n{type(e).__name__}: {e}", reply_markup=MENU)


async def bio_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    try:
        _, kind, raw_id = q.data.split(":", 2)
        target_id = int(raw_id)
    except (ValueError, TypeError):
        return
    if kind not in {"bot", "user"}:
        return

    uid = update.effective_user.id
    context.user_data["bio_kind"] = kind
    context.user_data["bio_mode"] = "one"
    context.user_data["bio_target"] = target_id
    state[uid] = "await_bio_text"

    label = str(target_id)
    if kind == "bot":
        row = await db.get_bot_account(target_id)
        if not row or not row.get("enabled", True):
            state.pop(uid, None)
            return await q.edit_message_text("❌ This bot account is no longer active.")
        status = BOT_ACCOUNT_STATUS.get((uid, target_id), "")
        if not status.startswith("🟢"):
            state.pop(uid, None)
            return await q.edit_message_text("❌ This bot account is not currently connected/listening.")
        label = row.get("first_name") or ("@" + row.get("username") if row.get("username") else str(target_id))
    else:
        rows = await session_store.get_sessions(enabled_only=True)
        row = next((r for r in rows if int(r.get("tg_user_id") or 0) == target_id), None)
        if not row:
            state.pop(uid, None)
            return await q.edit_message_text("❌ This user account is no longer active.")
        try:
            client = await get_account_client(row)
            if not client.is_connected():
                raise RuntimeError("not connected")
        except Exception:
            state.pop(uid, None)
            return await q.edit_message_text("❌ This user account is not currently connected.")
        label = row.get("first_name") or row.get("label") or row.get("username") or str(target_id)

    await q.edit_message_text(
        f"✏️ Selected: <b>{esc(label)}</b>\n\nSend the new bio now.\nSend <code>CLEAR</code> to remove the bio.",
        parse_mode=ParseMode.HTML,
    )


async def session_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = (q.data or "").split(":", 1)[1]
    rows = await session_store.get_sessions()
    row = next((r for r in rows if str(r["id"]) == sid), None)
    if not row:
        await q.answer("Session not found.", show_alert=True)
        return
    label = row.get("first_name") or row.get("label") or row.get("tg_user_id") or sid
    kb = InlineKeyboardMarkup([[ 
        InlineKeyboardButton("✅ Yes, remove", callback_data=f"sess_rm_yes:{sid}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"sess_rm_no:{sid}"),
    ]])
    await q.edit_message_text(
        f"🗑 <b>REMOVE SESSION?</b>\n\n"
        f"👤 {esc(label)}\n"
        f"🆔 <code>{esc(row.get('tg_user_id') or '—')}</code>\n\n"
        "This will disconnect this account from the Ads Bot and remove its saved session.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def session_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = (q.data or "").split(":", 1)[1]
    rows = await session_store.get_sessions()
    row = next((r for r in rows if str(r["id"]) == sid), None)
    if not row:
        await q.edit_message_text("ℹ️ Session is already removed.")
        return
    label = row.get("first_name") or row.get("label") or row.get("tg_user_id") or sid
    await close_account_client(sid)
    deleted = await session_store.delete_session(sid)
    target_cache.clear()
    stats.cached_group_count.pop(sid, None)
    stats.cached_user_count.pop(sid, None)
    if deleted:
        await subs_core.log_event(f"🗑 Account removed · operator <code>{tenant_id()}</code> · {esc(label)}")
        await q.edit_message_text(
            f"✅ <b>SESSION REMOVED</b>\n\n👤 {esc(label)}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await q.edit_message_text("❌ Session could not be removed. Please refresh Sessions and try again.")


async def session_remove_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Cancelled")
    await q.edit_message_text("✅ Session removal cancelled.")


async def post_init(app: Application):
    async def _timed(label, aw):
        began = time.monotonic()
        try:
            return await aw
        finally:
            log.info("startup: %s %.2fs", label, time.monotonic() - began)

    async def _telegram_setup():
        # Bottom-left 'Menu' button lists /start and /help.
        try:
            await app.bot.set_my_commands([BotCommand("start", "Open the main menu"),
                                           BotCommand("help", "How to use this bot")])
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            log.warning("Could not set Telegram commands/menu button: %s", e)

    # Independent steps run together instead of one after another.
    # A database outage must not stop the bot: failures are logged and operators retry below.
    results = await asyncio.gather(
        _timed("telegram commands", _telegram_setup()),
        _timed("look settings", load_look_settings()),
        _timed("co-admins", load_co_admins()),
        _timed("admin settings", load_admin_settings()),
        return_exceptions=True,
    )
    for res in results:
        if isinstance(res, Exception):
            log.error("startup step failed: %s", res)
    asyncio.create_task(free_bio_guard_loop(), name="free_bio_guard")
    await _timed("subscriptions", sub_start(app))  # expired users are revoked before listeners start
    asyncio.create_task(_operators_with_retry(app), name="operators_startup")


async def start_operators(app: Application) -> bool:
    """Start every approved operator's services. Returns False if any operator failed."""
    ok = True
    began = time.monotonic()
    for uid in sorted(APPROVED_USERS):
        try:
            await initialize_tenant(uid)
            await start_tenant_listener(app, uid)
            await start_join_queue_service(app, uid)
            await sync_bot_account_listeners(app, uid)
        except Exception:
            ok = False
            log.exception("startup: operator %s not ready yet", uid)
    log.info("startup: %d operator(s) checked %.2fs", len(APPROVED_USERS), time.monotonic() - began)
    return ok


async def _operators_with_retry(app: Application):
    """Retries operator startup every 60s until the database answers; the bot keeps running meanwhile."""
    while True:
        try:
            await load_approved_users()
            if await start_operators(app):
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("startup: database not ready (%s); retrying in 60s", type(exc).__name__)
        await asyncio.sleep(60)


async def post_shutdown(app: Application):
    try:
        await subs_core.shutdown()  # NEW
    except Exception:
        log.exception("Subscription shutdown error")
    for st in TENANT_STATS.values():
        st.running = False
    jobs = list(PROMO_TASKS.values()) + list(LISTENER_TASKS.values()) + list(JOIN_QUEUE_TASKS.values()) + list(BOT_ACCOUNT_TASKS.values())
    jobs += [value for key, value in app.bot_data.items() if key.startswith("dm_broadcast_task:") and value]
    for task in jobs:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in jobs if task), return_exceptions=True)
    PROMO_TASKS.clear()
    LISTENER_TASKS.clear()
    JOIN_QUEUE_TASKS.clear()
    BOT_ACCOUNT_TASKS.clear()
    await asyncio.gather(*(close_account_client(sid) for sid in list(_account_dm_clients)),
                         return_exceptions=True)
    for store in TENANT_SESSIONS.values():
        await store.close()
    await asyncio.to_thread(_access_mongo.close)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


def start_health_server():
    """Serve GET/HEAD / with 200 when PORT is set, so web-port health checks and cron pings pass."""
    port = os.getenv("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def _subs_route(self, method):
            """NEW: Razorpay callback/webhook routes. Returns True when the request was handled."""
            if not self.path.startswith("/razorpay/"):
                return False
            body = b""
            if method == "POST":
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = self.rfile.read(length) if length else b""
            res = subs_core.http_dispatch(method, self.path, body, self.headers) or (404, "text/plain", b"not found")
            self.send_response(res[0])
            self.send_header("Content-Type", res[1])
            self.send_header("Content-Length", str(len(res[2])))
            self.end_headers()
            self.wfile.write(res[2])
            return True

        def do_POST(self):
            if not self._subs_route("POST"):
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_GET(self):
            if self._subs_route("GET"):
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    log.info("Health endpoint listening on port %s", port)


def main():
    start_health_server()
    # Slow networks on new hosts caused startup timeouts; timeouts live on the request objects.
    bot = PremiumBot(
        BOT_TOKEN,
        request=HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30),
        get_updates_request=HTTPXRequest(connect_timeout=30, read_timeout=60, write_timeout=30, pool_timeout=30),
    )
    app = Application.builder().bot(bot).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("add", add_operator))
    app.add_handler(CommandHandler("rem", remove_operator))
    app.add_handler(CommandHandler("users", list_operators))
    app.add_handler(CommandHandler("start", authorized_handler(start)))
    app.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(authorized_handler(help_callback), pattern=r"^help:main$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(main_menu_callback), pattern=r"^main:(?:promotion|posts|accounts|autoreply|bio|monitoring)$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(submenu_callback), pattern=r"^sm:[0-9a-f]{12}$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(admin_callback), pattern=r"^adm:(?:home|maint|users|back|acc|cleanpre|cleanok)$"))
    app.add_handler(CallbackQueryHandler(co_admin_callback, pattern=r"^coa:"))
    app.add_handler(CallbackQueryHandler(authorized_handler(look_callback), pattern=r"^(?:adm:(?:look|menu|emoji|btnemoji|front|fbio)|mnu:[ehrx]:\d+|emj:(?:toggle|add|d:\d+)|bem:(?:p:\d+|x:\d+)|frt:[er]|fbio:(?:t|tg|run))$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(bio_pick_callback), pattern=r"^bio_pick:(?:bot|user):\d+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_prompt), pattern=r"^sess_rm:[0-9a-fA-F]+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_confirm), pattern=r"^sess_rm_yes:[0-9a-fA-F]+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_cancel), pattern=r"^sess_rm_no:[0-9a-fA-F]+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(promo_accounts_callback), pattern=r"^pacc:(?:all|none|done|[0-9a-fA-F]+)$"))
    # Save-post handler must run before the generic text/menu handler.
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION), authorized_handler(save_post_message)), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, authorized_handler(save_post_message)), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, authorized_handler(handle_menu)), group=1)
    # A stranger messaging the control bot receives a cooldown-limited response.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, authorized_handler(bot_dm_auto_reply)), group=2)
    subs_ui.configure(ADMIN_ID, APPROVED_USERS, _sub_back_to_main)   # NEW
    subs_ui.CFG["can"] = can
    subs_ui.register_handlers(app)                                    # NEW
    app.add_error_handler(error_handler)
    log.info("Starting Telegram polling")
    # bootstrap_retries=-1: keep retrying the first Telegram connection instead of exiting on one timeout.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
