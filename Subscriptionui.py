"""Telegram UI for subscriptions (NEW file): user purchase flow + Admin Panel section.

Wired into the existing bot with two calls from bot.py: configure(...) and register_handlers(app).
Callback prefixes used: `sub:` (users) and `sadm:` (admin only). Existing prefixes are untouched.
"""
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import subscriptions as S

log = logging.getLogger("promo-bot.subs.ui")
CFG = {"admin_id": 0, "approved": set(), "back_to_main": None}
STATE_TTL = 600


def configure(admin_id: int, approved_users: set, back_to_main):
    CFG.update(admin_id=int(admin_id), approved=approved_users, back_to_main=back_to_main)


def h(x) -> str:
    return html.escape(str(x), quote=False)


def B(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


async def _edit(q, text, rows):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
                                  disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _public_start_enabled() -> bool:
    return S.env("SUB_PUBLIC_START", "true").lower() in {"1", "true", "yes", "on"}


STATUS_LABEL = {"creating": "⏳ Creating", "created": "⏳ Awaiting payment", "paid": "✅ Paid", "expired": "⌛ Link expired",
                "cancelled": "❌ Cancelled", "failed": "⚠️ Failed", "review": "🔍 Needs review"}
STATUS_ICON = {"creating": "⏳", "created": "⏳", "paid": "✅", "expired": "⌛", "cancelled": "❌", "failed": "⚠️", "review": "🔍"}


def _sub_is_active(sub) -> bool:
    return bool(sub and sub.get("status") == "active" and S._aware(sub["expires_at"]) > S.now_utc())


def _sub_line(sub) -> str:
    if not sub:
        return "No subscription yet."
    if _sub_is_active(sub):
        return f"✅ Active — plan <b>{h(sub.get('plan_name'))}</b>, until <b>{S.fmt_dt(sub['expires_at'])}</b>"
    if sub.get("status") == "revoked":
        return "🚫 Ended by administrator."
    return f"⌛ Expired on {S.fmt_dt(sub.get('expires_at'))} (plan {h(sub.get('plan_name'))})"


# =============================================================== USER SIDE
def welcome_view():
    return ("📢 <b>Promotion Bot</b>\n\nThis bot requires an active subscription.\n"
            "Choose a plan and pay securely online — access is activated automatically after payment is verified.",
            [[B("💎 Purchase Subscription", "sub:home")]])


async def home_view(uid: int):
    if not S.STORE:
        return "💎 Subscriptions are temporarily unavailable. Please try again shortly.", [[B("⬅️ Back", "sub:back")]]
    plans = await S.run(S.STORE.list_plans, True)
    sub = await S.run(S.STORE.get_sub, uid)
    lines = ["💎 <b>SUBSCRIPTION PLANS</b>", "", _sub_line(sub), ""]
    rows = []
    if not plans:
        lines.append("No plans are available right now. Please contact the owner.")
    for p in plans:
        lines.append(f"• <b>{h(p['name'])}</b> — {S.fmt_inr(p['price_paise'])} for {p['duration_days']} days")
        rows.append([B(f"{S.fmt_inr(p['price_paise'])} · {p['name']} · {p['duration_days']}d", f"sub:plan:{p['plan_id']}")])
    if not S.payments_configured():
        lines += ["", "⚠️ Online payments are not enabled yet."]
    rows.append([B("📊 My Subscription", "sub:my")])
    rows.append([B("⬅️ Back", "sub:back")])
    return "\n".join(lines), rows


async def my_view(uid: int):
    sub = await S.run(S.STORE.get_sub, uid)
    _, pays = await S.run(S.STORE.list_payments, None, uid, 0, 5)
    lines = ["📊 <b>MY SUBSCRIPTION</b>", "", _sub_line(sub)]
    if sub:
        lines += [f"Started: {S.fmt_dt(sub.get('started_at'))}", f"Expires: {S.fmt_dt(sub.get('expires_at'))}",
                  f"Renewals: {int(sub.get('renewals') or 0)}"]
    if pays:
        lines += ["", "<b>Recent payments</b>"]
        for p in pays:
            lines.append(f"• {S.fmt_dt(p['created_at'])[:11]} · {S.fmt_inr(p['amount_paise'])} · "
                         f"{STATUS_LABEL.get(p['status'], p['status'])} · <code>{h(p['_id'])}</code>")
    return "\n".join(lines), [[B("💎 Plans / Renew", "sub:home")], [B("⬅️ Back", "sub:back")]]


def payment_view(doc):
    st = doc["status"]
    head = (f"🧾 <b>PAYMENT</b>\nPlan: <b>{h(doc['plan_name'])}</b> ({doc['duration_days']} days)\n"
            f"Amount: <b>{S.fmt_inr(doc['amount_paise'])}</b>\nReference: <code>{h(doc['_id'])}</code>\n"
            f"Status: {STATUS_LABEL.get(st, st)}\n")
    ref = doc["_id"]
    if st == "paid":
        if doc.get("fulfillment") == "done":
            return head + "\n✅ Payment verified and your subscription is active.", [[B("📊 My Subscription", "sub:my")], [B("⬅️ Back", "sub:back")]]
        return head + "\n✅ Payment verified — activating your access…", [[B("🔄 Check Status", f"sub:chk:{ref}")]]
    if st == "created" and doc.get("short_url"):
        return (head + f"\nPay within ~{S.LINK_EXPIRY_MINUTES} minutes using the button below. "
                       "Access activates automatically once the payment is verified.",
                [[InlineKeyboardButton("💳 Pay Now", url=doc["short_url"])],
                 [B("🔄 I've Paid — Check Status", f"sub:chk:{ref}")],
                 [B("❌ Cancel Payment", f"sub:cxl:{ref}")], [B("⬅️ Back", "sub:home")]])
    msg = {"expired": "This payment link expired. Start a new payment from the plans list.",
           "cancelled": "This payment was cancelled.", "failed": "The payment could not be started. Please try again.",
           "review": "This payment needs manual review. Please contact the owner with the reference above."}.get(st, "")
    return head + "\n" + msg, [[B("💎 Plans", "sub:home")], [B("⬅️ Back", "sub:back")]]


async def sub_callback(update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if not user or user.is_bot or not q.message or q.message.chat.type != "private":
        return await q.answer()
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == "back":
            await q.answer()
            if user.id in CFG["approved"] and CFG["back_to_main"]:
                return await CFG["back_to_main"](update, ctx)
            text, rows = welcome_view()
            return await _edit(q, text, rows)
        if not S.STORE:
            return await q.answer("Subscriptions are temporarily unavailable.", show_alert=True)
        if action == "home":
            await q.answer()
            return await _edit(q, *await home_view(user.id))
        if action == "my":
            await q.answer()
            return await _edit(q, *await my_view(user.id))
        if action == "plan" and len(parts) == 3:
            await q.answer()
            plan = await S.run(S.STORE.get_plan, parts[2])
            if not plan or not plan.get("enabled"):
                return await _edit(q, "This plan is no longer available.", [[B("⬅️ Back", "sub:home")]])
            text = (f"💎 <b>{h(plan['name'])}</b>\n\nPrice: <b>{S.fmt_inr(plan['price_paise'])}</b>\n"
                    f"Duration: <b>{plan['duration_days']} days</b>\n\n"
                    "If you already have an active subscription, the new period is added after your current expiry.")
            rows = [[B(f"💳 Pay {S.fmt_inr(plan['price_paise'])}", f"sub:pay:{plan['plan_id']}")], [B("⬅️ Back", "sub:home")]]
            return await _edit(q, text, rows)
        if action == "pay" and len(parts) == 3:
            await q.answer("Preparing your payment…")
            try:
                doc, _reused = await S.create_checkout(user.id, parts[2])
            except RuntimeError as e:
                return await _edit(q, f"⚠️ {h(e)}", [[B("⬅️ Back", "sub:home")]])
            return await _edit(q, *payment_view(doc))
        if action in ("chk", "cxl") and len(parts) == 3:
            doc = await S.run(S.STORE.get_payment, parts[2])
            if not doc or int(doc["user_id"]) != user.id:
                return await q.answer("Payment not found.", show_alert=True)
            await q.answer("Checking…")
            try:
                if action == "chk":
                    doc = await S.refresh_payment(parts[2])
                else:
                    await S.cancel_payment(parts[2], user.id)
                    doc = await S.run(S.STORE.get_payment, parts[2])
            except Exception as e:
                log.warning("payment refresh failed: %s", e)
                doc = await S.run(S.STORE.get_payment, parts[2])
            return await _edit(q, *payment_view(doc))
        await q.answer()
    except Exception:
        log.exception("sub_callback error")
        try:
            await q.answer("Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


async def start_gate(update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start for people WITHOUT access -> purchase screen. Approved users fall through to the existing /start."""
    user = update.effective_user
    if not user or user.is_bot or not update.message:
        return
    if user.id == CFG["admin_id"]:
        ctx.user_data.pop("sadm", None)
    if user.id in CFG["approved"] or not _public_start_enabled():
        return
    text, rows = welcome_view()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    raise ApplicationHandlerStop


# =============================================================== ADMIN SIDE
def _is_admin(user) -> bool:
    return bool(user and user.id == CFG["admin_id"])


def _paged(prefix, page, total, size=S.PAGE_SIZE):
    row = []
    if page > 0:
        row.append(B("◀️ Prev", f"{prefix}:{page - 1}"))
    if (page + 1) * size < total:
        row.append(B("Next ▶️", f"{prefix}:{page + 1}"))
    return [row] if row else []


def _ok(flag) -> str:
    return "✅" if flag else "❌"


async def dash_view():
    st = await S.run(S.STORE.stats)
    approved = len([u for u in CFG["approved"] if u != CFG["admin_id"]])
    lines = ["💎 <b>SUBSCRIPTIONS</b>", "",
             f"Subscribers: <b>{st['active']}</b> active · {st['expired']} expired · {st['revoked']} ended",
             f"Expiring: {st['exp3']} within 3d · {st['exp7']} within 7d",
             f"Approved users (excl. owner): {approved}",
             f"Payments: <b>{st['pay_paid']}</b> paid · {st['pay_pending']} pending · {st['pay_other']} other",
             f"Revenue: <b>{S.fmt_inr(st['revenue_all'])}</b> total · {S.fmt_inr(st['revenue_30d'])} last 30d"]
    if st["unfulfilled"] or st["review"]:
        lines.append(f"⚠️ Attention: {st['unfulfilled']} paid but not activated · {st['review']} need review")
    lines += ["", f"Razorpay keys {_ok(S.payments_configured())} · Webhook secret {_ok(S.webhook_configured())} · "
                  f"Public URL {_ok(S.public_base().startswith('https://'))}"]
    rows = [[B("📦 Plans", "sadm:plans"), B("💳 Payments", "sadm:pay:all:0")],
            [B("👥 Subscribers", "sadm:subs:all:0"), B("🔎 Find User", "sadm:find")],
            [B("🙋 Approved Users", "sadm:users"), B("⏰ Expiry Check", "sadm:exp")],
            [B("🧾 Audit Log", "sadm:audit:0")],
            [B("⬅️ Back", "adm:home")]]
    return "\n".join(lines), rows


async def plans_view():
    plans = await S.run(S.STORE.list_plans)
    lines = ["📦 <b>PLANS</b>", ""]
    rows = []
    for p in plans:
        flag = "🟢" if p["enabled"] else "🔴"
        lines.append(f"{flag} #{p['plan_id']} <b>{h(p['name'])}</b> — {S.fmt_inr(p['price_paise'])} / {p['duration_days']}d")
        rows.append([B(f"{flag} #{p['plan_id']} {p['name']}", f"sadm:pl:{p['plan_id']}")])
    if not plans:
        lines.append("No plans yet. Create one to start selling.")
    rows += [[B("➕ New Plan", "sadm:pnew")], [B("⬅️ Back", "sadm:home")]]
    return "\n".join(lines), rows


async def plan_view(pid, note=""):
    p = await S.run(S.STORE.get_plan, pid)
    if not p:
        return "Plan not found.", [[B("⬅️ Back", "sadm:plans")]]
    text = (f"📦 <b>Plan #{p['plan_id']}: {h(p['name'])}</b>\n\nPrice: <b>{S.fmt_inr(p['price_paise'])}</b>\n"
            f"Duration: <b>{p['duration_days']} days</b>\nStatus: {'🟢 Enabled' if p['enabled'] else '🔴 Disabled'}\n\n"
            "Price/duration changes apply to new purchases only; payments already started keep their original amount.")
    if note:
        text = note + "\n\n" + text
    pid = p["plan_id"]
    rows = [[B("✏️ Name", f"sadm:pe:{pid}:name"), B("💰 Price", f"sadm:pe:{pid}:price")],
            [B("⏳ Duration", f"sadm:pe:{pid}:days"), B("🔴 Disable" if p["enabled"] else "🟢 Enable", f"sadm:pt:{pid}")],
            [B("⬅️ Back", "sadm:plans")]]
    return text, rows


async def payments_view(flt, page):
    status = None if flt == "all" else flt
    total, docs = await S.run(S.STORE.list_payments, status, None, page * S.PAGE_SIZE, S.PAGE_SIZE)
    lines = [f"💳 <b>PAYMENTS</b> — {flt} ({total})", ""]
    rows = []
    for p in docs:
        lines.append(f"{STATUS_ICON.get(p['status'], '•')} {S.fmt_inr(p['amount_paise'])} · <code>{p['user_id']}</code> · "
                     f"{h(p['plan_name'])} · {S.fmt_dt(p['created_at'])[:11]}")
        rows.append([B(f"{STATUS_ICON.get(p['status'], '•')} {S.fmt_inr(p['amount_paise'])} · {p['user_id']} · {S.fmt_dt(p['created_at'])[:6]}",
                       f"sadm:pd:{p['_id']}")])
    if not docs:
        lines.append("No payments.")
    rows += _paged(f"sadm:pay:{flt}", page, total)
    rows.append([B("All", "sadm:pay:all:0"), B("Paid", "sadm:pay:paid:0"), B("Pending", "sadm:pay:pending:0"), B("Issues", "sadm:pay:other:0")])
    rows.append([B("⬅️ Back", "sadm:home")])
    return "\n".join(lines), rows


async def payment_admin_view(ref, note=""):
    p = await S.run(S.STORE.get_payment, ref)
    if not p:
        return "Payment not found.", [[B("⬅️ Back", "sadm:pay:all:0")]]
    lines = [f"🧾 <b>PAYMENT</b> <code>{h(ref)}</code>", "",
             f"Status: {STATUS_LABEL.get(p['status'], p['status'])}", f"User: <code>{p['user_id']}</code>",
             f"Plan: {h(p['plan_name'])} ({p['duration_days']}d)", f"Amount: {S.fmt_inr(p['amount_paise'])} {p['currency']}",
             f"Razorpay link: <code>{h(p.get('link_id') or '—')}</code>", f"Razorpay payment: <code>{h(p.get('razorpay_payment_id') or '—')}</code>",
             f"Created: {S.fmt_dt(p.get('created_at'))}", f"Paid: {S.fmt_dt(p.get('paid_at'))}",
             f"Activation: {p.get('fulfillment') or 'not started'}"]
    if p.get("failed_attempts"):
        lines.append(f"Failed attempts: {p['failed_attempts']} ({h(p.get('last_failure', ''))})")
    elif p.get("last_failure"):
        lines.append(f"Note: {h(p['last_failure'])}")
    if note:
        lines = [note, ""] + lines
    rows = [[B("🔄 Re-verify with Razorpay", f"sadm:pr:{ref}")], [B("👤 User", f"sadm:su:{p['user_id']}")],
            [B("⬅️ Back", "sadm:pay:all:0")]]
    return "\n".join(lines), rows


async def subs_view(flt, page):
    total, subs = await S.run(S.STORE.list_subs, None if flt == "all" else flt, page * S.PAGE_SIZE, S.PAGE_SIZE)
    lines = [f"👥 <b>SUBSCRIBERS</b> — {flt} ({total})", ""]
    rows = []
    for s in subs:
        icon = "✅" if _sub_is_active(s) else ("🚫" if s.get("status") == "revoked" else "⌛")
        lines.append(f"{icon} <code>{s['user_id']}</code> · {h(s.get('plan_name'))} · until {S.fmt_dt(s['expires_at'])[:11]}")
        rows.append([B(f"{icon} {s['user_id']} · {s.get('plan_name')}", f"sadm:su:{s['user_id']}")])
    if not subs:
        lines.append("No subscribers.")
    rows += _paged(f"sadm:subs:{flt}", page, total)
    rows.append([B("All", "sadm:subs:all:0"), B("Active", "sadm:subs:active:0"), B("Expired", "sadm:subs:expired:0")])
    rows.append([B("⬅️ Back", "sadm:home")])
    return "\n".join(lines), rows


async def user_admin_view(uid, note=""):
    sub = await S.run(S.STORE.get_sub, uid)
    _, pays = await S.run(S.STORE.list_payments, None, uid, 0, 5)
    approved = uid in CFG["approved"]
    lines = [f"👤 <b>USER</b> <code>{uid}</code>", "", f"Bot access: {'✅ approved' if approved else '❌ not approved'}", _sub_line(sub)]
    if sub:
        lines += [f"Started: {S.fmt_dt(sub.get('started_at'))}", f"Expires: {S.fmt_dt(sub.get('expires_at'))}",
                  f"Renewals: {int(sub.get('renewals') or 0)} · Access removed on expiry: {'yes' if sub.get('revoke_on_expiry', True) else 'no (manual access)'}"]
    if pays:
        lines += ["", "<b>Recent payments</b>"]
        for p in pays:
            lines.append(f"{STATUS_ICON.get(p['status'], '•')} {S.fmt_inr(p['amount_paise'])} · {h(p['plan_name'])} · "
                         f"{S.fmt_dt(p['created_at'])[:11]}")
    if note:
        lines = [note, ""] + lines
    rows = [[B("➕ Extend / Grant Days", f"sadm:sx:{uid}")]]
    if _sub_is_active(sub):
        rows.append([B("🚫 Revoke Subscription", f"sadm:sv:{uid}")])
    rows.append([B("⬅️ Back", "sadm:home")])
    return "\n".join(lines), rows


async def approved_users_view():
    ids = sorted(u for u in CFG["approved"] if u != CFG["admin_id"])
    subs = {s["user_id"]: s for s in await S.run(lambda: list(S.STORE.subs.find(
        {"owner_id": S.STORE.owner_id, "user_id": {"$in": ids[:40]}})))}
    lines = ["🙋 <b>APPROVED USERS</b>", "", f"Owner: <code>{CFG['admin_id']}</code>"]
    rows = []
    for u in ids[:40]:
        s = subs.get(u)
        tag = ("✅ until " + S.fmt_dt(s["expires_at"])[:11]) if _sub_is_active(s) else ("⌛ expired" if s else "manual")
        lines.append(f"• <code>{u}</code> — {tag}")
    if len(ids) > 40:
        lines.append(f"…and {len(ids) - 40} more. Use Find User.")
    if not ids:
        lines.append("No other users.")
    lines.append("\nUse /add &lt;uid&gt; and /rem &lt;uid&gt; for manual access.")
    rows.append([B("⬅️ Back", "sadm:home")])
    return "\n".join(lines), rows


async def expiry_view(note=""):
    st = await S.run(S.STORE.stats)
    soon = await S.run(S.STORE.expiring_within, 7, 10)
    overdue = len(await S.run(S.STORE.due_for_expiry))
    lines = ["⏰ <b>EXPIRY CHECK</b>", "", f"Active: {st['active']} · within 3d: {st['exp3']} · within 7d: {st['exp7']}",
             f"Overdue awaiting processing: {overdue}", f"Automatic sweep: every {S.SWEEP_SECONDS}s (also on every restart)"]
    if soon:
        lines += ["", "<b>Next to expire</b>"]
        lines += [f"• <code>{s['user_id']}</code> · {h(s.get('plan_name'))} · {S.fmt_dt(s['expires_at'])}" for s in soon]
    if note:
        lines = [note, ""] + lines
    return "\n".join(lines), [[B("▶️ Run Expiry Sweep Now", "sadm:sweep")], [B("⬅️ Back", "sadm:home")]]


async def audit_view(page):
    items = await S.run(S.STORE.list_audit, page * 10, 11)
    more = len(items) > 10
    lines = ["🧾 <b>AUDIT LOG</b>", ""]
    for a in items[:10]:
        lines.append(f"{S.fmt_dt(a['created_at'])[:17]} · {h(a['actor'])} · {h(a['action'])} · <code>{h(a['target'])}</code>")
    if not items:
        lines.append("No entries.")
    nav = []
    if page > 0:
        nav.append(B("◀️ Prev", f"sadm:audit:{page - 1}"))
    if more:
        nav.append(B("Next ▶️", f"sadm:audit:{page + 1}"))
    return "\n".join(lines), ([nav] if nav else []) + [[B("⬅️ Back", "sadm:home")]]


def _prompt(ctx, act, text, back="sadm:home", **extra):
    ctx.user_data["sadm"] = {"act": act, "ts": time.time(), **extra}
    return text, [[B("❌ Cancel", "sadm:cancel")]]


async def sadm_callback(update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _is_admin(q.from_user):
        return await q.answer("⛔ Admin only", show_alert=True)
    if not q.message or q.message.chat.type != "private
