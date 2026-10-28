"""Subscription + Razorpay payment core (NEW file; no Telegram imports).

Flow: user picks a plan -> Razorpay Payment Link is created from the PLAN PRICE STORED
IN MONGODB (never from user input) -> payment is confirmed only by (a) a signed webhook or
(b) a signed callback, and in BOTH cases the payment is re-fetched from the Razorpay API and
checked (status, amount, currency, reference) before anything is activated.
Activation = add the user to `approved_operators` (same steps as /add) via the bot hook.
Expiry   = background sweep that removes access (same steps as /rem) via the bot hook.
All records are scoped by owner_id (= ADMIN_ID) and live in MongoDB.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

import httpx
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("promo-bot.subs")
UTC = timezone.utc
RZ_API = "https://api.razorpay.com/v1"
LINK_EXPIRY_MINUTES = 30          # Razorpay requires >= 15
STALE_LOCK_SECONDS = 300
REMINDER_DAYS = 3
SWEEP_SECONDS = 60
PAGE_SIZE = 8
# Free trial limits: 1 account, no Bot feature, no AI auto reply.
TRIAL_CAPS = {"max_accounts": 1, "bot": False, "ai": False}
TRIAL_DAYS = 7                    # free trial length for every new user (one per Telegram user)


# ----------------------------------------------------------------- config / helpers
def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def payments_configured() -> bool:
    return bool(env("RAZORPAY_KEY_ID") and env("RAZORPAY_KEY_SECRET"))


def webhook_configured() -> bool:
    return bool(env("RAZORPAY_WEBHOOK_SECRET"))


def public_base() -> str:
    return env("PUBLIC_BASE_URL").rstrip("/")


def now_utc() -> datetime:
    return datetime.now(UTC)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        offset = int(env("DISPLAY_TZ_OFFSET_MINUTES", "330"))
    except ValueError:
        offset = 330
    local = _aware(dt).astimezone(timezone(timedelta(minutes=offset)))
    label = "IST" if offset == 330 else f"UTC{offset / 60:+g}"
    return local.strftime("%d %b %Y, %I:%M %p ") + label


def fmt_inr(paise: int) -> str:
    rupees = int(paise) / 100
    return f"₹{rupees:,.0f}" if int(paise) % 100 == 0 else f"₹{rupees:,.2f}"


def parse_price_paise(text: str) -> int:
    try:
        d = Decimal((text or "").strip().replace("₹", "").replace(",", ""))
        if not d.is_finite() or d != d.quantize(Decimal("0.01")):
            raise ValueError
        paise = int((d * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid price in rupees, e.g. 199 or 199.50 (max 2 decimals).")
    if not 100 <= paise <= 50_000_000:
        raise ValueError("Price must be between ₹1 and ₹500000.")
    return paise


def caps_for_plan(plan) -> dict:
    """Limits of a stored plan. Missing fields on older plans mean no limit / features on."""
    mx = plan.get("max_accounts")
    return {"max_accounts": None if mx is None else int(mx),
            "bot": bool(plan.get("bot_feature", True)), "ai": bool(plan.get("ai_feature", True))}


def parse_accounts(text: str) -> int:
    try:
        n = int((text or "").strip())
    except ValueError:
        raise ValueError("Enter a whole number, e.g. 5.")
    if not 1 <= n <= 500:
        raise ValueError("Accounts must be between 1 and 500.")
    return n


def parse_days(text: str) -> int:
    try:
        n = int((text or "").strip())
    except ValueError:
        raise ValueError("Enter whole days, e.g. 30.")
    if not 1 <= n <= 3650:
        raise ValueError("Duration must be between 1 and 3650 days.")
    return n


def parse_name(text: str) -> str:
    name = " ".join((text or "").split())
    if not 2 <= len(name) <= 40:
        raise ValueError("Plan name must be 2–40 characters.")
    return name


# ----------------------------------------------------------------- MongoDB store (sync)
class Store:
    def __init__(self, uri: str, db_name: str, owner_id: int):
        self.owner_id = int(owner_id)
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
        d = self.client[db_name]
        self.plans = d["sub_plans"]
        self.payments = d["sub_payments"]
        self.subs = d["subscriptions"]
        self.audit_log = d["sub_audit_log"]
        self.events = d["sub_webhook_events"]
        self.counters = d["sub_counters"]
        self.users = d["bot_users"]
        self.settings = d["bot_settings"]
        self.broadcasts = d["broadcasts"]

    def init(self):
        self.client.admin.command("ping")
        self.plans.create_index([("owner_id", ASCENDING), ("plan_id", ASCENDING)], unique=True)
        self.plans.create_index([("owner_id", ASCENDING), ("enabled", ASCENDING)])
        self.payments.create_index([("owner_id", ASCENDING), ("link_id", ASCENDING)], unique=True,
                                   partialFilterExpression={"link_id": {"$type": "string"}})
        self.payments.create_index([("owner_id", ASCENDING), ("razorpay_payment_id", ASCENDING)], unique=True,
                                   partialFilterExpression={"razorpay_payment_id": {"$type": "string"}})
        self.payments.create_index([("owner_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)])
        self.payments.create_index([("owner_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
        self.subs.create_index([("owner_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
        self.subs.create_index([("owner_id", ASCENDING), ("status", ASCENDING), ("expires_at", ASCENDING)])
        self.audit_log.create_index([("owner_id", ASCENDING), ("created_at", DESCENDING)])
        self.events.create_index([("created_at", ASCENDING)], expireAfterSeconds=30 * 86400)
        self.payments.create_index([("owner_id", ASCENDING), ("qr_id", ASCENDING)], unique=True,
                                   partialFilterExpression={"qr_id": {"$type": "string"}})
        self.users.create_index([("owner_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

    def close(self):
        self.client.close()

    # ---- audit
    def audit(self, actor, action, target, details=None):
        self.audit_log.insert_one({"owner_id": self.owner_id, "actor": actor, "action": action,
                                   "target": str(target), "details": details or {}, "created_at": now_utc()})

    def list_audit(self, skip=0, limit=10):
        return list(self.audit_log.find({"owner_id": self.owner_id}).sort("created_at", DESCENDING).skip(skip).limit(limit))

    # ---- plans
    def create_plan(self, name, price_paise, days):
        seq = self.counters.find_one_and_update({"_id": f"plan:{self.owner_id}"}, {"$inc": {"seq": 1}},
                                                upsert=True, return_document=ReturnDocument.AFTER)["seq"]
        doc = {"owner_id": self.owner_id, "plan_id": str(seq), "plan_no": int(seq), "name": name,
               "price_paise": int(price_paise), "duration_days": int(days), "enabled": True,
               "max_accounts": 5, "bot_feature": True, "ai_feature": True,
               "created_at": now_utc(), "updated_at": now_utc()}
        self.plans.insert_one(doc)
        return doc

    def list_plans(self, enabled_only=False):
        q = {"owner_id": self.owner_id}
        if enabled_only:
            q["enabled"] = True
        return list(self.plans.find(q).sort("plan_no", ASCENDING))

    def get_plan(self, plan_id):
        return self.plans.find_one({"owner_id": self.owner_id, "plan_id": str(plan_id)})

    def update_plan(self, plan_id, **fields):
        fields["updated_at"] = now_utc()
        return self.plans.find_one_and_update({"owner_id": self.owner_id, "plan_id": str(plan_id)},
                                              {"$set": fields}, return_document=ReturnDocument.AFTER)

    # ---- payments
    def insert_payment(self, doc):
        self.payments.insert_one(doc)

    def get_payment(self, ref):
        return self.payments.find_one({"_id": ref, "owner_id": self.owner_id})

    def get_payment_by_link(self, link_id):
        return self.payments.find_one({"owner_id": self.owner_id, "link_id": link_id})

    def get_payment_by_qr(self, qr_id):
        return self.payments.find_one({"owner_id": self.owner_id, "qr_id": qr_id})

    def set_qr(self, ref, qr_id, image_url, close_by):
        """Attach a UPI QR to a still-open payment. Only the first QR per payment is kept."""
        return self.payments.find_one_and_update(
            {"_id": ref, "owner_id": self.owner_id, "status": "created", "qr_id": None},
            {"$set": {"qr_id": qr_id, "qr_image_url": image_url, "qr_close_by": close_by, "updated_at": now_utc()}},
            return_document=ReturnDocument.AFTER)

    # ---- bot users (registered on /start) for broadcast
    def touch_user(self, uid, name):
        self.users.update_one(
            {"owner_id": self.owner_id, "user_id": int(uid)},
            {"$set": {"name": (name or "")[:64], "last_seen": now_utc(), "blocked": False},
             "$setOnInsert": {"first_seen": now_utc()}}, upsert=True)

    def recipients(self):
        return [d["user_id"] for d in self.users.find({"owner_id": self.owner_id, "blocked": {"$ne": True}},
                                                      {"user_id": 1}).sort("user_id", ASCENDING)]

    def count_users(self):
        return self.users.count_documents({"owner_id": self.owner_id, "blocked": {"$ne": True}})

    def mark_blocked(self, uid):
        self.users.update_one({"owner_id": self.owner_id, "user_id": int(uid)}, {"$set": {"blocked": True}})

    def log_broadcast(self, doc):
        doc["owner_id"] = self.owner_id
        self.broadcasts.insert_one(doc)

    # ---- editable bot texts
    def get_setting(self, key):
        d = self.settings.find_one({"_id": f"{self.owner_id}:{key}"})
        return d["value"] if d else None

    def set_setting(self, key, value):
        self.settings.replace_one({"_id": f"{self.owner_id}:{key}"},
                                  {"_id": f"{self.owner_id}:{key}", "value": value, "updated_at": now_utc()}, upsert=True)

    def del_setting(self, key):
        self.settings.delete_one({"_id": f"{self.owner_id}:{key}"})

    def has_sub_record(self, uid):
        return self.subs.find_one({"_id": self.sid(uid)}, {"_id": 1}) is not None

    def trial_enabled(self):
        return self.get_setting("trial_enabled") is not False

    def trials_started(self):
        return self.subs.count_documents({"owner_id": self.owner_id, "plan_id": "trial"})

    def find_open_payment(self, user_id, plan_id, amount_paise):
        return self.payments.find_one(
            {"owner_id": self.owner_id, "user_id": int(user_id), "plan_id": str(plan_id), "status": "created",
             "amount_paise": int(amount_paise), "link_id": {"$type": "string"},
             "link_expires_at": {"$gt": now_utc() + timedelta(minutes=3)}},
            sort=[("created_at", DESCENDING)])

    def set_link(self, ref, link_id, short_url, expires_at):
        return self.payments.find_one_and_update(
            {"_id": ref, "owner_id": self.owner_id, "status": "creating"},
            {"$set": {"status": "created", "link_id": link_id, "short_url": short_url,
                      "link_expires_at": expires_at, "updated_at": now_utc()}},
            return_document=ReturnDocument.AFTER)

    def set_status(self, ref, from_statuses, to_status, **extra):
        extra.update({"status": to_status, "updated_at": now_utc()})
        return self.payments.find_one_and_update(
            {"_id": ref, "owner_id": self.owner_id, "status": {"$in": list(from_statuses)}},
            {"$set": extra}, return_document=ReturnDocument.AFTER)

    def note_failure(self, ref, text):
        self.payments.update_one({"_id": ref, "owner_id": self.owner_id},
                                 {"$inc": {"failed_attempts": 1},
                                  "$set": {"last_failure": (text or "")[:200], "updated_at": now_utc()}})

    def claim_paid(self, ref, payment_id):
        """Atomic created->paid transition. Only ONE caller ever receives the document."""
        try:
            return self.payments.find_one_and_update(
                {"_id": ref, "owner_id": self.owner_id,
                 "status": {"$in": ["creating", "created", "expired", "cancelled"]}},
                {"$set": {"status": "paid", "paid_at": now_utc(), "razorpay_payment_id": payment_id,
                          "updated_at": now_utc()}},
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            return "dup"

    def claim_fulfillment(self, ref):
        now = now_utc()
        return self.payments.find_one_and_update(
            {"_id": ref, "owner_id": self.owner_id, "status": "paid",
             "$or": [{"fulfillment": None},
                     {"fulfillment": "in_progress",
                      "fulfillment_started": {"$lt": now - timedelta(seconds=STALE_LOCK_SECONDS)}}]},
            {"$set": {"fulfillment": "in_progress", "fulfillment_started": now}},
            return_document=ReturnDocument.AFTER)

    def finish_fulfillment(self, ref):
        self.payments.update_one({"_id": ref, "owner_id": self.owner_id},
                                 {"$set": {"fulfillment": "done", "fulfilled_at": now_utc()}})

    def release_fulfillment(self, ref, err):
        self.payments.update_one({"_id": ref, "owner_id": self.owner_id, "fulfillment": "in_progress"},
                                 {"$set": {"fulfillment": None, "fulfillment_error": str(err)[:200]}})

    def unfulfilled_paid(self, limit=25):
        return list(self.payments.find({"owner_id": self.owner_id, "status": "paid",
                                        "fulfillment": {"$ne": "done"}}).limit(limit))

    def pending_qr(self, limit=25):
        now = now_utc()
        return list(self.payments.find(
            {"owner_id": self.owner_id, "status": "created", "qr_id": {"$type": "string"},
             "created_at": {"$lt": now - timedelta(seconds=45), "$gt": now - timedelta(hours=26)}}).limit(limit))

    def pending_links(self, limit=25):
        now = now_utc()
        return list(self.payments.find(
            {"owner_id": self.owner_id, "status": "created", "link_id": {"$type": "string"},
             "created_at": {"$lt": now - timedelta(seconds=90), "$gt": now - timedelta(hours=26)}}).limit(limit))

    def list_payments(self, status=None, user_id=None, skip=0, limit=PAGE_SIZE):
        q = {"owner_id": self.owner_id}
        if status == "pending":
            q["status"] = {"$in": ["creating", "created"]}
        elif status == "other":
            q["status"] = {"$in": ["expired", "cancelled", "failed", "review"]}
        elif status:
            q["status"] = status
        if user_id is not None:
            q["user_id"] = int(user_id)
        return (self.payments.count_documents(q),
                list(self.payments.find(q).sort("created_at", DESCENDING).skip(skip).limit(limit)))

    # ---- webhook events (audit / dedupe of fully processed events)
    def event_done(self, event_id):
        return bool(event_id and self.events.find_one({"_id": event_id, "done": True}))

    def mark_event(self, event_id, event):
        if event_id:
            self.events.update_one({"_id": event_id}, {"$set": {"event": event, "done": True, "created_at": now_utc()}},
                                   upsert=True)

    # ---- subscriptions
    def sid(self, uid):
        return f"{self.owner_id}:{int(uid)}"

    def get_sub(self, uid):
        return self.subs.find_one({"_id": self.sid(uid), "owner_id": self.owner_id})

    def apply_subscription(self, uid, plan_id, plan_name, days, ref, was_approved, *, manual=False):
        """Idempotent per `ref`. Returns (sub_doc, applied_now)."""
        sid = self.sid(uid)
        now = now_utc()
        sub = self.subs.find_one({"_id": sid})
        if sub and ref in (sub.get("applied_payments") or []):
            return sub, False
        active = bool(sub and sub.get("status") == "active" and _aware(sub["expires_at"]) > now)
        base = _aware(sub["expires_at"]) if active else now
        fields = {"status": "active", "payment_status": "manual" if manual else "paid",
                  "expires_at": base + timedelta(days=int(days)), "last_payment_ref": ref, "updated_at": now}
        if active:
            revoke = bool(sub.get("revoke_on_expiry", True))
            if not manual:
                fields.update({"plan_id": plan_id, "plan_name": plan_name})
        else:
            revoke = not was_approved
            fields.update({"plan_id": plan_id, "plan_name": plan_name, "started_at": now, "reminded_for": None,
                           "access_revoked": None})
        fields["revoke_on_expiry"] = revoke
        try:
            self.subs.update_one(
                {"_id": sid, "applied_payments": {"$ne": ref}},
                {"$set": fields, "$addToSet": {"applied_payments": ref},
                 "$inc": {"renewals": 1 if active else 0},
                 "$setOnInsert": {"owner_id": self.owner_id, "user_id": int(uid), "created_at": now}},
                upsert=True)
        except DuplicateKeyError:
            return self.subs.find_one({"_id": sid}), False
        return self.subs.find_one({"_id": sid}), True

    def due_for_expiry(self, limit=100):
        now = now_utc()
        return list(self.subs.find({"owner_id": self.owner_id, "$or": [
            {"status": "active", "expires_at": {"$lte": now}},
            {"status": "expired", "revoke_on_expiry": True, "access_revoked": False}]}).limit(limit))

    def claim_expired(self, uid):
        """Atomic active->expired; a renewal that already moved expires_at forward will NOT match."""
        return self.subs.find_one_and_update(
            {"_id": self.sid(uid), "owner_id": self.owner_id, "status": "active", "expires_at": {"$lte": now_utc()}},
            {"$set": {"status": "expired", "access_revoked": False, "expired_at": now_utc(), "updated_at": now_utc()}},
            return_document=ReturnDocument.AFTER)

    def mark_access_revoked(self, uid):
        self.subs.update_one({"_id": self.sid(uid), "owner_id": self.owner_id, "status": "expired"},
                             {"$set": {"access_revoked": True, "updated_at": now_utc()}})

    def revoke_sub(self, uid):
        return self.subs.find_one_and_update(
            {"_id": self.sid(uid), "owner_id": self.owner_id, "status": "active"},
            {"$set": {"status": "revoked", "access_revoked": True, "revoked_at": now_utc(), "updated_at": now_utc()}},
            return_document=ReturnDocument.AFTER)

    def due_for_reminder(self, limit=50):
        now = now_utc()
        out = []
        for s in self.subs.find({"owner_id": self.owner_id, "status": "active",
                                 "expires_at": {"$gt": now, "$lte": now + timedelta(days=REMINDER_DAYS)}}).limit(limit):
            if s.get("reminded_for") != s["expires_at"]:
                out.append(s)
        return out

    def mark_reminded(self, uid, expires_at):
        self.subs.update_one({"_id": self.sid(uid)}, {"$set": {"reminded_for": expires_at}})

    def list_subs(self, status=None, skip=0, limit=PAGE_SIZE):
        q = {"owner_id": self.owner_id}
        if status:
            q["status"] = status
        return (self.subs.count_documents(q),
                list(self.subs.find(q).sort("expires_at", ASCENDING).skip(skip).limit(limit)))

    def expiring_within(self, days, limit=15):
        now = now_utc()
        return list(self.subs.find({"owner_id": self.owner_id, "status": "active",
                                    "expires_at": {"$gt": now, "$lte": now + timedelta(days=days)}})
                    .sort("expires_at", ASCENDING).limit(limit))

    def stats(self):
        now = now_utc()
        o = {"owner_id": self.owner_id}
        out = {
            "active": self.subs.count_documents({**o, "status": "active", "expires_at": {"$gt": now}}),
            "expired": self.subs.count_documents({**o, "status": "expired"}),
            "revoked": self.subs.count_documents({**o, "status": "revoked"}),
            "exp3": self.subs.count_documents({**o, "status": "active", "expires_at": {"$gt": now, "$lte": now + timedelta(days=3)}}),
            "exp7": self.subs.count_documents({**o, "status": "active", "expires_at": {"$gt": now, "$lte": now + timedelta(days=7)}}),
            "pay_paid": self.payments.count_documents({**o, "status": "paid"}),
            "pay_pending": self.payments.count_documents({**o, "status": {"$in": ["creating", "created"]}}),
            "pay_other": self.payments.count_documents({**o, "status": {"$in": ["expired", "cancelled", "failed", "review"]}}),
            "unfulfilled": self.payments.count_documents({**o, "status": "paid", "fulfillment": {"$ne": "done"}}),
            "review": self.payments.count_documents({**o, "status": "review"}),
        }
        rev = {"all": 0, "d30": 0}
        for p in self.payments.find({**o, "status": "paid"}, {"amount_paise": 1, "paid_at": 1}):
            rev["all"] += int(p.get("amount_paise", 0))
            if p.get("paid_at") and _aware(p["paid_at"]) > now - timedelta(days=30):
                rev["d30"] += int(p.get("amount_paise", 0))
        out["revenue_all"], out["revenue_30d"] = rev["all"], rev["d30"]
        return out


# ----------------------------------------------------------------- runtime wiring
STORE: Store | None = None
LOOP: asyncio.AbstractEventLoop | None = None
HOOKS: dict = {}          # is_approved(uid)->bool, grant(uid)->awaitable, revoke(uid)->awaitable, notify(uid,text)->awaitable
_locks: dict[int, asyncio.Lock] = {}
_bg_task: asyncio.Task | None = None


def user_lock(uid: int) -> asyncio.Lock:
    return _locks.setdefault(int(uid), asyncio.Lock())


async def run(fn, *a, **k):
    return await asyncio.to_thread(lambda: fn(*a, **k))


async def init(uri, db_name, owner_id, loop, hooks):
    global STORE, LOOP, HOOKS
    STORE = Store(uri, db_name, owner_id)
    await asyncio.to_thread(STORE.init)
    LOOP, HOOKS = loop, hooks


async def start_background():
    global _bg_task
    if _bg_task and not _bg_task.done():
        return
    _bg_task = asyncio.create_task(_background_loop(), name="subscription_service")


async def shutdown():
    global _bg_task
    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
        await asyncio.gather(_bg_task, return_exceptions=True)
    _bg_task = None
    if STORE:
        await asyncio.to_thread(STORE.close)


async def _notify(uid, text):
    try:
        if HOOKS.get("notify"):
            await HOOKS["notify"](int(uid), text)
    except Exception as e:
        log.warning("notify %s failed: %s", uid, type(e).__name__)


# ----------------------------------------------------------------- Razorpay REST
async def _rz(method: str, path: str, payload: dict | None = None) -> dict:
    auth = (env("RAZORPAY_KEY_ID"), env("RAZORPAY_KEY_SECRET"))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, RZ_API + path, auth=auth, json=payload)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        desc = ((data.get("error") or {}).get("description") or f"HTTP {r.status_code}")
        raise RuntimeError(f"Razorpay error: {desc}"[:200])
    return data


# ----------------------------------------------------------------- checkout creation
async def create_checkout(user_id: int, plan_id: str):
    """Returns (payment_doc, reused). Price always comes from the stored plan."""
    if not STORE:
        raise RuntimeError("Subscriptions are not initialised.")
    if not payments_configured():
        raise RuntimeError("Online payments are not configured yet.")
    plan = await run(STORE.get_plan, plan_id)
    if not plan or not plan.get("enabled"):
        raise RuntimeError("This plan is not available.")
    async with user_lock(user_id):
        existing = await run(STORE.find_open_payment, user_id, plan["plan_id"], plan["price_paise"])
        if existing:
            return existing, True
        ref = "sp_" + uuid.uuid4().hex[:24]
        expires = now_utc() + timedelta(minutes=LINK_EXPIRY_MINUTES)
        doc = {"_id": ref, "owner_id": STORE.owner_id, "user_id": int(user_id), "plan_id": plan["plan_id"],
               "plan_name": plan["name"], "duration_days": int(plan["duration_days"]),
               "amount_paise": int(plan["price_paise"]), "currency": "INR", "status": "creating",
               "link_id": None, "short_url": None, "razorpay_payment_id": None, "fulfillment": None,
               "failed_attempts": 0, "created_at": now_utc(), "updated_at": now_utc()}
        await run(STORE.insert_payment, doc)
        body = {"amount": doc["amount_paise"], "currency": "INR", "accept_partial": False,
                "description": f"{plan['name']} - {plan['duration_days']} days",
                "reference_id": ref, "expire_by": int(expires.timestamp()),
                "notes": {"telegram_user_id": str(user_id), "plan_id": plan["plan_id"], "payment_ref": ref},
                "notify": {"sms": False, "email": False}, "reminder_enable": False}
        if public_base().startswith("https://"):
            body["callback_url"] = public_base() + "/razorpay/callback"
            body["callback_method"] = "get"
        try:
            link = await _rz("POST", "/payment_links", body)
            updated = await run(STORE.set_link, ref, link["id"], link["short_url"], expires)
        except Exception as e:
            await run(STORE.set_status, ref, ["creating"], "failed", last_failure=str(e)[:200])
            log.warning("payment link creation failed for user %s: %s", user_id, e)
            raise RuntimeError("Could not start the payment. Please try again in a moment.")
        await run(STORE.audit, f"user:{user_id}", "payment_created", ref, {"plan": plan["plan_id"], "amount_paise": doc["amount_paise"]})
        return updated, False


# ----------------------------------------------------------------- verification + fulfilment
async def process_link(link_id: str, hint_payment_id: str | None = None) -> str:
    """Re-fetch the link from Razorpay (authoritative) and reconcile our record. Idempotent."""
    doc = await run(STORE.get_payment_by_link, link_id)
    if not doc:
        return "unknown"
    remote = await _rz("GET", f"/payment_links/{link_id}")
    rstatus = remote.get("status")
    ref = doc["_id"]
    if rstatus == "paid":
        ok = (int(remote.get("amount") or 0) == int(doc["amount_paise"])
              and int(remote.get("amount_paid") or 0) == int(doc["amount_paise"])
              and (remote.get("currency") or "INR") == "INR"
              and remote.get("reference_id") == ref)
        if not ok:
            await run(STORE.set_status, ref, ["creating", "created", "expired", "cancelled"], "review",
                      last_failure="amount/currency/reference mismatch")
            await run(STORE.audit, "system", "payment_mismatch", ref, {"remote_amount_paid": remote.get("amount_paid")})
            log.error("Payment %s flagged for review: remote data mismatch", ref)
            return "review"
        pay_id = hint_payment_id
        for p in (remote.get("payments") or []):
            if isinstance(p, dict) and p.get("payment_id") and p.get("status") in (None, "captured", "authorized"):
                pay_id = p["payment_id"]
        if not pay_id:
            pay_id = f"link:{link_id}"        # keeps the unique-payment guard even if the API omits ids
        claimed = await run(STORE.claim_paid, ref, pay_id)
        if claimed == "dup":
            await run(STORE.set_status, ref, ["creating", "created", "expired", "cancelled"], "review",
                      last_failure="razorpay payment id already used")
            return "review"
        if claimed:
            await run(STORE.audit, "system", "payment_verified", ref, {"payment_id": pay_id})
        await activate(ref)
        return "paid"
    if rstatus == "cancelled":
        await run(STORE.set_status, ref, ["creating", "created"], "cancelled")
        return "cancelled"
    if rstatus == "expired":
        await run(STORE.set_status, ref, ["creating", "created"], "expired")
        return "expired"
    return "pending"


async def activate(ref: str) -> bool:
    """Grant access exactly once per verified payment."""
    doc = await run(STORE.claim_fulfillment, ref)
    if not doc:
        return False
    uid = int(doc["user_id"])
    try:
        async with user_lock(uid):
            was_approved = bool(HOOKS["is_approved"](uid))
            sub, applied = await run(STORE.apply_subscription, uid, doc["plan_id"], doc["plan_name"],
                                     doc["duration_days"], ref, was_approved)
            await HOOKS["grant"](uid)
            await run(STORE.finish_fulfillment, ref)
        await run(STORE.audit, "system", "subscription_activated", ref,
                  {"user_id": uid, "expires_at": sub["expires_at"].isoformat()})
    except Exception as e:
        log.exception("Activation failed for %s", ref)
        await run(STORE.release_fulfillment, ref, e)
        return False
    if applied:
        await _notify(uid, "✅ <b>Payment verified!</b>\n\n"
                           f"Plan: <b>{_h(doc['plan_name'])}</b>\nAmount: {fmt_inr(doc['amount_paise'])}\n"
                           f"Active until: <b>{fmt_dt(sub['expires_at'])}</b>\n\nSend /start to open the bot.")
    return True


def _h(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def cancel_payment(ref: str, user_id: int):
    """User-initiated cancel. If it was actually paid, it is fulfilled instead."""
    doc = await run(STORE.get_payment, ref)
    if not doc or int(doc["user_id"]) != int(user_id):
        return "unknown"
    if doc["status"] != "created" or not doc.get("link_id"):
        return doc["status"]
    state = await process_link(doc["link_id"])
    if state != "pending":
        return state
    try:
        await _rz("POST", f"/payment_links/{doc['link_id']}/cancel")
    except Exception as e:
        log.warning("cancel failed: %s", e)
        return await process_link(doc["link_id"])
    await run(STORE.set_status, ref, ["created"], "cancelled")
    await run(STORE.audit, f"user:{user_id}", "payment_cancelled", ref)
    return "cancelled"


async def refresh_payment(ref: str):
    doc = await run(STORE.get_payment, ref)
    if doc and doc["status"] == "paid" and doc.get("fulfillment") != "done":
        await activate(ref)
    elif doc and doc["status"] in ("created", "expired", "cancelled") and doc.get("link_id"):
        await process_link(doc["link_id"])
    doc = await run(STORE.get_payment, ref)
    if doc and doc["status"] == "created" and doc.get("qr_id"):
        await process_qr(doc["qr_id"])
    return await run(STORE.get_payment, ref)


# ----------------------------------------------------------------- UPI QR (same Razorpay account)
QR_MAX_SECONDS = 2 * 3600          # Razorpay caps close_by for single_use QR codes at 2 hours
QR_MIN_SECONDS = 180


async def create_qr(ref: str):
    """Create (once) a single-use, fixed-amount UPI QR for an open payment. Amount comes from the stored payment."""
    doc = await run(STORE.get_payment, ref)
    if not doc:
        raise RuntimeError("Payment not found.")
    if doc.get("qr_id") and doc.get("qr_image_url"):
        return doc
    if doc["status"] != "created":
        raise RuntimeError("This payment is no longer open.")
    now = now_utc()
    close_ts = int(min(doc["link_expires_at"].timestamp() if doc.get("link_expires_at") else now.timestamp() + QR_MAX_SECONDS,
                       now.timestamp() + QR_MAX_SECONDS))
    if close_ts - now.timestamp() < QR_MIN_SECONDS:
        raise RuntimeError("This payment is about to expire. Please start a new payment.")
    body = {"type": "upi_qr", "usage": "single_use", "fixed_amount": True,
            "payment_amount": int(doc["amount_paise"]), "name": "Subscription",
            "description": f"{doc['duration_days']} days access"[:255], "close_by": close_ts,
            "notes": {"payment_ref": ref, "telegram_user_id": str(doc["user_id"])}}
    try:
        qr = await _rz("POST", "/payments/qr_codes", body)
    except Exception as e:
        log.warning("UPI QR creation failed for %s: %s", ref, e)
        raise RuntimeError("UPI QR is not available right now. Please use the payment link instead.")
    updated = await run(STORE.set_qr, ref, qr["id"], qr.get("image_url"), close_ts)
    if not updated:
        return await run(STORE.get_payment, ref)
    await run(STORE.audit, f"user:{doc['user_id']}", "qr_created", ref, {"qr_id": qr["id"]})
    return updated


async def process_qr(qr_id: str, hint_payment_id: str | None = None) -> str:
    """Server-side verification for a UPI QR: re-fetch the QR and its captured payments from Razorpay, then activate."""
    doc = await run(STORE.get_payment_by_qr, qr_id)
    if not doc:
        return "unknown"
    if doc["status"] == "paid":
        await activate(doc["_id"])
        return "paid"
    if doc["status"] not in ("created", "expired", "cancelled"):
        return "pending"
    ref, amount = doc["_id"], int(doc["amount_paise"])
    remote = await _rz("GET", f"/payments/qr_codes/{qr_id}")
    if int(remote.get("payments_count_received") or 0) < 1:
        return "pending"
    if int(remote.get("payments_amount_received") or 0) != amount:
        await run(STORE.set_status, ref, ["created", "expired", "cancelled"], "review",
                  last_failure="QR amount mismatch")
        await run(STORE.audit, "system", "payment_mismatch", ref, {"qr_amount_received": remote.get("payments_amount_received")})
        log.error("QR payment %s flagged for review: amount mismatch", ref)
        return "review"
    pays = await _rz("GET", f"/payments/qr_codes/{qr_id}/payments", None)
    pay_id = None
    for p in (pays.get("items") or []):
        if not isinstance(p, dict) or p.get("status") != "captured":
            continue
        if int(p.get("amount") or 0) != amount or (p.get("currency") or "INR") != "INR":
            continue
        if ((p.get("notes") or {}).get("payment_ref") or ref) != ref:
            continue
        pay_id = p.get("id")
        break
    if not pay_id:
        return "pending"
    claimed = await run(STORE.claim_paid, ref, pay_id)
    if claimed == "dup":
        await run(STORE.set_status, ref, ["created", "expired", "cancelled"], "review",
                  last_failure="razorpay payment id already used")
        return "review"
    if claimed:
        await run(STORE.audit, "system", "payment_verified", ref, {"payment_id": pay_id, "via": "upi_qr"})
    await activate(ref)
    return "paid"


# ----------------------------------------------------------------- expiry / reconcile
async def expiry_sweep():
    if not STORE:
        return
    for sub in await run(STORE.due_for_expiry):
        uid = int(sub["user_id"])
        try:
            async with user_lock(uid):
                if sub["status"] == "active":
                    claimed = await run(STORE.claim_expired, uid)
                    if not claimed:
                        continue          # renewed in the meantime
                    notify = True
                else:
                    claimed = await run(STORE.get_sub, uid)
                    if not claimed or claimed.get("status") != "expired" or claimed.get("access_revoked") is not False:
                        continue
                    notify = False
                if claimed.get("revoke_on_expiry", True):
                    await HOOKS["revoke"](uid)
                await run(STORE.mark_access_revoked, uid)
            await run(STORE.audit, "system", "subscription_expired", uid, {"revoked": bool(claimed.get("revoke_on_expiry", True))})
            if notify:
                await _notify(uid, "⌛ <b>Your subscription has expired.</b>\n\nTap /start → 💎 Purchase Subscription to renew.")
        except Exception:
            log.exception("Expiry handling failed for user %s", uid)


async def send_reminders():
    for sub in await run(STORE.due_for_reminder):
        await _notify(sub["user_id"], f"⏰ Your subscription expires on <b>{fmt_dt(sub['expires_at'])}</b>.\n"
                                      "Renew from 💎 Purchase Subscription to keep access.")
        await run(STORE.mark_reminded, sub["user_id"], sub["expires_at"])


async def reconcile():
    """Polling verification (no webhook needed): re-checks open payment links and UPI QRs with Razorpay."""
    for p in await run(STORE.unfulfilled_paid):
        await activate(p["_id"])
    if payments_configured():
        for p in await run(STORE.pending_links):
            try:
                await process_link(p["link_id"])
            except Exception as e:
                log.warning("reconcile link %s failed: %s", p["_id"], e)
        for p in await run(STORE.pending_qr):
            try:
                await process_qr(p["qr_id"])
            except Exception as e:
                log.warning("reconcile qr %s failed: %s", p["_id"], e)


async def verify_pending() -> dict:
    """Manual 'verify now' for the admin panel. Same logic as the background poll."""
    checked = paid = activated = 0
    for p in await run(STORE.unfulfilled_paid):
        if await activate(p["_id"]):
            activated += 1
    if payments_configured():
        for p in await run(STORE.pending_links):
            checked += 1
            try:
                paid += (await process_link(p["link_id"])) == "paid"
            except Exception as e:
                log.warning("verify link %s failed: %s", p["_id"], e)
        for p in await run(STORE.pending_qr):
            checked += 1
            try:
                paid += (await process_qr(p["qr_id"])) == "paid"
            except Exception as e:
                log.warning("verify qr %s failed: %s", p["_id"], e)
    return {"checked": checked, "paid": paid, "activated": activated}


async def _background_loop():
    while True:
        try:
            await expiry_sweep()
            await reconcile()
            await send_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Subscription background loop error")
        await asyncio.sleep(SWEEP_SECONDS)


async def start_trial(uid: int, name: str = "") -> tuple[bool, str]:
    """Free trial: once per Telegram user, grants full access for TRIAL_DAYS. No payment involved."""
    if not STORE:
        return False, "Subscriptions are temporarily unavailable. Please try again shortly."
    if not await run(STORE.trial_enabled):
        return False, "The free trial is not available right now."
    async with user_lock(uid):
        if await run(STORE.has_sub_record, uid):
            return False, "You have already used your free trial or had a subscription. Please choose a paid plan."
        was_approved = bool(HOOKS["is_approved"](uid))
        ref = f"trial_{int(uid)}"
        sub, _applied = await run(STORE.apply_subscription, uid, "trial", "Free Trial", TRIAL_DAYS, ref, was_approved,
                                  manual=True)
        await HOOKS["grant"](uid)
        await run(STORE.audit, f"user:{uid}", "trial_started", ref, {"days": TRIAL_DAYS, "name": name[:40]})
    return True, (f"🎁 <b>Free trial started!</b>\nFull access is active until <b>{fmt_dt(sub['expires_at'])}</b>.\n\n"
                  "Tap below to open the bot.")


# ----------------------------------------------------------------- admin manual actions
async def admin_extend(uid: int, days: int, actor: int):
    async with user_lock(uid):
        # An explicit admin extension is time-limited: access is removed when it ends (owner is never revoked).
        ref = "manual_" + uuid.uuid4().hex[:12]
        sub, _ = await run(STORE.apply_subscription, uid, None, "Manual (admin)", days, ref, False, manual=True)
        await HOOKS["grant"](uid)
    await run(STORE.audit, f"admin:{actor}", "manual_extend", uid, {"days": days, "expires_at": sub["expires_at"].isoformat()})
    await _notify(uid, f"✅ Your subscription was extended by {days} day(s). Active until <b>{fmt_dt(sub['expires_at'])}</b>.")
    return sub


async def admin_revoke(uid: int, actor: int):
    async with user_lock(uid):
        sub = await run(STORE.revoke_sub, uid)
        if not sub:
            return None
        await HOOKS["revoke"](uid)
    await run(STORE.audit, f"admin:{actor}", "manual_revoke", uid)
    await _notify(uid, "🚫 Your subscription has been ended by the administrator.")
    return sub


# ----------------------------------------------------------------- HTTP (called from the health-server thread)
def _page(title: str, msg: str, status=200):
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style='font-family:sans-serif;text-align:center;padding:12vh 24px'>"
            f"<h2>{title}</h2><p>{msg}</p></body>").encode()
    return status, "text/html; charset=utf-8", body


def _on_loop(coro, timeout=25):
    if LOOP is None:
        raise RuntimeError("not ready")
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout=timeout)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = env("RAZORPAY_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def verify_callback_signature(params: dict) -> bool:
    secret = env("RAZORPAY_KEY_SECRET")
    sig = params.get("razorpay_signature", "")
    if not secret or not sig:
        return False
    payload = "|".join(params.get(k, "") for k in ("razorpay_payment_link_id", "razorpay_payment_link_reference_id",
                                                   "razorpay_payment_link_status", "razorpay_payment_id"))
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def http_dispatch(method: str, raw_path: str, body: bytes, headers):
    """Returns (status, content_type, body_bytes) or None when the path is not ours."""
    path = urlparse(raw_path).path
    try:
        if method == "GET" and path == "/razorpay/callback":
            q = {k: v[0] for k, v in parse_qs(urlparse(raw_path).query).items()}
            if not verify_callback_signature(q):
                return _page("Invalid request", "This payment confirmation could not be verified.", 400)
            if q.get("razorpay_payment_link_status") == "paid" and STORE:
                try:
                    _on_loop(process_link(q["razorpay_payment_link_id"], q.get("razorpay_payment_id")))
                except Exception:
                    log.exception("callback processing failed")
            return _page("Payment received ✅", "You can close this page and return to Telegram. "
                                                  "Your access is activated automatically after verification.")
        if method == "POST" and path == "/razorpay/webhook":
            if not webhook_configured():
                return 503, "text/plain", b"webhook not configured"
            if not verify_webhook_signature(body, headers.get("X-Razorpay-Signature", "")):
                log.warning("Rejected webhook with invalid signature")
                return 400, "text/plain", b"invalid signature"
            payload = json.loads(body.decode("utf-8"))
            event = payload.get("event", "")
            event_id = headers.get("X-Razorpay-Event-Id", "")
            if STORE is None:
                return 503, "text/plain", b"not ready"
            if _on_loop(run(STORE.event_done, event_id)):
                return 200, "text/plain", b"duplicate"
            _on_loop(_handle_event(event, payload))
            _on_loop(run(STORE.mark_event, event_id, event))
            return 200, "text/plain", b"ok"
    except Exception:
        log.exception("HTTP handler error on %s", path)
        return 500, "text/plain", b"error"
    return None


async def _handle_event(event: str, payload: dict):
    ents = payload.get("payload") or {}
    if event == "qr_code.credited":
        qr = ((ents.get("qr_code") or {}).get("entity") or {})
        pay = ((ents.get("payment") or {}).get("entity") or {})
        if qr.get("id"):
            await process_qr(qr["id"], pay.get("id"))
    elif event in ("payment_link.paid", "payment_link.expired", "payment_link.cancelled", "payment_link.partially_paid"):
        link = ((ents.get("payment_link") or {}).get("entity") or {})
        pay = ((ents.get("payment") or {}).get("entity") or {})
        if link.get("id"):
            await process_link(link["id"], pay.get("id"))
    elif event == "payment.failed":     # informational only; never changes status
        pay = ((ents.get("payment") or {}).get("entity") or {})
        notes = pay.get("notes") if isinstance(pay.get("notes"), dict) else {}
        ref = notes.get("payment_ref")
        if ref:
            await run(STORE.note_failure, ref, pay.get("error_description") or "payment failed")
