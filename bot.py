import asyncio
import html
import logging
import os
import time
import random
import contextvars
from functools import wraps
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, errors, types, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from database import Database, MongoSessionStore
from pacing import AccountPacer

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.getenv("MONGO_DB", "promo_bot")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
DEFAULT_INTERVAL_MINUTES = int(os.getenv("DEFAULT_INTERVAL_MINUTES", "60"))
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

MENU = ReplyKeyboardMarkup(
    [
        ["📣 Promotion", "📝 Posts"],
        ["👥 Accounts", "💬 Auto Reply"],
        ["✏️ Edit/Set Bio", "📊 Monitoring"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

PROMOTION_MENU = ReplyKeyboardMarkup(
    [
        ["▶️ Start Promo", "⏹ Stop Promo"],
        ["⏱ Interval", "⏳ Group Delay"],
        ["🔄 Rescan Targets", "📨 DM Broadcast"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ACCOUNTS_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Add Session", "👤 Sessions"],
        ["🔗 Join Groups", "🤖 Bot Accounts"],
        ["🗑 Remove Session"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

JOIN_GROUPS_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Add Group Links", "📋 Join Queue"],
        ["🗑 Clear Pending Joins"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MONITORING_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Live Stats", "📊 Account Stats"],
        ["📜 Account Logs"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BIO_MENU = ReplyKeyboardMarkup(
    [
        ["🤖 Set Bot Bio", "👤 Set User Bio"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BIO_SCOPE_MENU = ReplyKeyboardMarkup(
    [
        ["👤 Individual", "🌐 Set All"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POST_MENU = ReplyKeyboardMarkup(
    [
        ["📝 Save Post", "📋 List Posts"],
        ["👁 Preview Post", "🔘 Toggle Post"],
        ["🗑 Delete Post", "🔀 Sending Mode"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POST_MODE_MENU = ReplyKeyboardMarkup(
    [["🔄 Rotate", "🎲 Random"], ["⬅️ Back"]],
    resize_keyboard=True,
    is_persistent=True,
)

AUTO_REPLY_MENU = ReplyKeyboardMarkup(
    [
        ["✏️ Account Reply", "✏️ Control Bot Reply"],
        ["👁 Reply Preview", "🔄 Use Saved Post"],
        ["🟢 Toggle Account Reply", "🟢 Toggle Bot Reply"],
        ["⏱ Reply Cooldown", "📡 Reply Status"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADD_SESSION_MENU = ReplyKeyboardMarkup(
    [
        ["🔑 Session String", "📱 Login with Phone"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BOT_ACCOUNT_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Add Bot", "👤 Bot Accounts"],
        ["📊 Bot Stats", "📜 Bot Logs"],
        ["🟢 Toggle Bot Auto Reply", "⏱ Bot Reply Cooldown"],
        ["🗑 Remove Bot"],
        ["⬅️ Back"],
    ],
    resize_keyboard=True,
    is_persistent=True,
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


async def edit_account_progress(row, metric, *, force=False):
    key = metric_key(row['id'], metric['kind'])
    message = ACCOUNT_PROGRESS_MESSAGES.get(key)
    if not message or (not force and time.monotonic() - metric['last_edit'] < 12):
        return
    metric['last_edit'] = time.monotonic()
    try:
        await message.edit_text(account_stats_text(metric), parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if 'not modified' not in str(exc).lower():
            log.warning('Account progress edit failed: %s', type(exc).__name__)
    except Exception as exc:
        log.warning('Account progress edit failed: %s', type(exc).__name__)


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
            if handler is start and update.message:
                await update.message.reply_text("⛔ Access denied. Contact the bot owner.")
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


async def make_client(session_string: str) -> TelegramClient:
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


async def send_saved_post(client: TelegramClient, entity, post: dict):
    text = post.get("html_text") or ""
    media_path = post.get("media_path")
    if media_path:
        if not Path(media_path).exists():
            raise FileNotFoundError(f"Saved media missing: {media_path}")
        await client.send_file(
            entity,
            media_path,
            caption=text or None,
            parse_mode="html",
        )
    else:
        await client.send_message(entity, text, parse_mode="html", link_preview=False)


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

    progress_msg = None
    try:
        progress_msg = await app.bot.send_message(tenant_id(), stats_text(), parse_mode=ParseMode.HTML)
    except Exception:
        pass

    edit_lock = asyncio.Lock()
    metrics = {}
    for row, client, groups, users in prepared:
        metric = begin_metric(row, 'promo', len(groups)+len(users), len(groups), len(users))
        metrics[str(row['id'])] = metric
        try:
            message = await app.bot.send_message(tenant_id(), account_stats_text(metric), parse_mode=ParseMode.HTML)
            ACCOUNT_PROGRESS_MESSAGES[metric_key(row['id'], 'promo')] = message
        except Exception:
            pass
    async def resume_prepaused(row):
        """Reconnect only this account when a pre-existing FloodWait expires."""
        sid = str(row["id"])
        metric = begin_metric(row, 'promo')
        metrics[sid] = metric
        try:
            message = await app.bot.send_message(tenant_id(), account_stats_text(metric), parse_mode=ParseMode.HTML)
            ACCOUNT_PROGRESS_MESSAGES[metric_key(row['id'], 'promo')] = message
        except Exception:
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
        await _run_session_round(row, client, groups, users, posts, mode, progress_msg, edit_lock, metric)

    # Normal and cooling-down accounts have independent concurrent workers.
    # One account finishing or crashing must never stop the other accounts.
    async def isolated_worker(row, client, groups, users):
        sid = str(row["id"])
        metric = metrics[sid]
        try:
            await _run_session_round(
                row, client, groups, users, posts, mode, progress_msg, edit_lock, metric
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

    if progress_msg:
        try:
            await progress_msg.edit_text(stats_text(), parse_mode=ParseMode.HTML)
        except Exception:
            pass


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

    progress_msg = None
    try:
        progress_msg = await app.bot.send_message(tenant_id(), "📨 <b>DM BROADCAST STARTED</b>\n\n" + stats_text(), parse_mode=ParseMode.HTML)
    except Exception:
        pass

    edit_lock = asyncio.Lock()
    metrics = {}
    for row, client, users in prepared:
        metric = begin_metric(row, 'dm', len(users), users=len(users))
        metrics[str(row['id'])] = metric
        try:
            message = await app.bot.send_message(tenant_id(), account_stats_text(metric), parse_mode=ParseMode.HTML)
            ACCOUNT_PROGRESS_MESSAGES[metric_key(row['id'], 'dm')] = message
        except Exception:
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
    if progress_msg:
        try:
            await progress_msg.edit_text("✅ <b>DM BROADCAST COMPLETE</b>\n\n" + stats_text(), parse_mode=ParseMode.HTML)
        except Exception:
            pass



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


async def scheduler(app: Application):
    stats.running = True
    try:
        while stats.running:
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
            interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES)))
            interval = max(5, interval)
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
    APPROVED_USERS.discard(uid)  # Reject new actions before stopping existing jobs.
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

async def list_operators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID or update.effective_chat.type != "private":
        return
    operators = sorted(APPROVED_USERS - {ADMIN_ID})
    if not operators:
        return await update.message.reply_text("👥 No other users approved. Add one with /add <uid>.")
    lines = ["👥 Approved users:"] + [f"• <code>{uid}</code>" for uid in operators]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await bot_dm_auto_reply(update, context)
    await update.message.reply_text(
        "📢 <b>Promotion Bot</b>\n\nChoose a category below. Promotion, accounts, replies, bio tools and monitoring are grouped to keep the main screen clean.",
        parse_mode=ParseMode.HTML,
        reply_markup=MENU,
    )


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


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text or ""
    if context.user_data.pop("post_saved_message_id", None) == update.message.message_id:
        return

    # Auto-reply submenu: always process Back before text input.
    current = state.get(uid)
    if text == "⬅️ Back":
        state.pop(uid, None)
        context.user_data.pop("bio_kind", None)
        context.user_data.pop("bio_mode", None)
        context.user_data.pop("bio_target", None)
        await cleanup_login_flow(uid)
        await update.message.reply_text("Main menu.", reply_markup=MENU)
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
    if text == "✏️ Edit/Set Bio":
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
            await update.message.reply_text("Bio flow expired. Open ✏️ Edit/Set Bio again.", reply_markup=MENU)
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

    if text == "💬 Auto Reply":
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
        session_string = text.strip()
        await safe_delete_message(update.message)
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
            if minutes < 5:
                raise ValueError
            await db.set_setting("interval_minutes", str(minutes))
            await update.message.reply_text(f"✅ Interval set to {minutes} minutes.", reply_markup=MENU)
        except ValueError:
            await update.message.reply_text("❌ Send a whole number of minutes (minimum 5).", reply_markup=MENU)
        return

    if current == "await_remove":
        state.pop(uid, None)
        try:
            session_ref = text.strip()
            deleted = await session_store.delete_session(session_ref)
            target_cache.clear()
            if not deleted:
                raise ValueError("Session not found")
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
        state[uid] = "choose_add_method"
        await update.message.reply_text(
            "How do you want to add the Telegram account?",
            reply_markup=ADD_SESSION_MENU,
        )

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

    elif text == "⏱ Interval":
        current_interval = await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL_MINUTES))
        state[uid] = "await_interval"
        await update.message.reply_text(
            f"Current interval: {current_interval} min.\n\nSend new interval in minutes (minimum 5).",
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

    elif text == "📊 Live Stats":
        stats.active_posts = len(await db.list_posts(enabled_only=True))
        stats.post_mode = await db.get_setting("post_mode", "rotate")
        await update.message.reply_text(stats_text(), parse_mode=ParseMode.HTML, reply_markup=MENU)

    else:
        await update.message.reply_text("Use the menu buttons below.", reply_markup=MENU)


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
    await load_approved_users()
    # Owner database and legacy encrypted sessions are never migrated/deleted.
    for uid in sorted(APPROVED_USERS):
        await initialize_tenant(uid)
        await start_tenant_listener(app, uid)
        await start_join_queue_service(app, uid)
        await sync_bot_account_listeners(app, uid)


async def post_shutdown(app: Application):
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


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("add", add_operator))
    app.add_handler(CommandHandler("rem", remove_operator))
    app.add_handler(CommandHandler("users", list_operators))
    app.add_handler(CommandHandler("start", authorized_handler(start)))
    app.add_handler(CallbackQueryHandler(authorized_handler(bio_pick_callback), pattern=r"^bio_pick:(?:bot|user):\d+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_prompt), pattern=r"^sess_rm:[0-9a-fA-F]+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_confirm), pattern=r"^sess_rm_yes:[0-9a-fA-F]+$"))
    app.add_handler(CallbackQueryHandler(authorized_handler(session_remove_cancel), pattern=r"^sess_rm_no:[0-9a-fA-F]+$"))
    # Save-post handler must run before the generic text/menu handler.
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION), authorized_handler(save_post_message)), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, authorized_handler(save_post_message)), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, authorized_handler(handle_menu)), group=1)
    # A stranger messaging the control bot receives a cooldown-limited response.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, authorized_handler(bot_dm_auto_reply)), group=2)
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
