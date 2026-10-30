"""Telegram UI for subscriptions (NEW file): user purchase flow + Admin Panel section.

Wired into the existing bot with two calls from bot.py: configure(...) and register_handlers(app).
Callback prefixes used: `sub:` (users) and `sadm:` (admin only). Existing prefixes are untouched.
"""
import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import subscriptions as S
from rich_ui import edit_screen, heading, para, footer, rich_table, button_cell

log = logging.getLogger("promo-bot.subs.ui")
CFG = {"admin_id": 0, "approved": set(), "back_to_main": None}
STATE_TTL = 600
BC = {"running": False, "task": None}
DEFAULT_WELCOME = ("📢 <b>Promotion Bot</b>\n\nThis bot requires an active subscription.\n"
                   "Choose a plan and pay securely online — access is activated automatically after payment is verified.")


def configure(admin_id: int, approved_users: set, back_to_main):
    CFG.update(admin_id=int(admin_id), approved=approved_users, back_to_main=back_to_main)


def h(x) -> str:
    return html.escape(str(x), quote=False)


def B(label: str, data: str) -> InlineKeyboardButton:
    if data.startswith("sub:") and "Back" not in label:      # subscription buttons are red
        return InlineKeyboardButton(label, callback_data=data, api_kwargs={"style": "danger"})
    return InlineKeyboardButton(label, callback_data=data)


def _caps_text(caps) -> str:
    accts = "unlimited" if caps["max_accounts"] is None else str(caps["max_accounts"])
    groups = caps.get("max_groups", 10)
    return (f"{accts} account(s) · 📣 {groups} group(s) · 🤖 Bot {'✅' if caps['bot'] else '❌'} · "
            f"🧠 AI {'✅' if caps['ai'] else '❌'}")


DURATION_NAMES = {7: "7 Days", 15: "15 Days", 30: "1 Month", 60: "2 Months", 90: "3 Months"}


def duration_label(days: int) -> str:
    return DURATION_NAMES.get(int(days), f"{int(days)} Days")


async def _edit(q, text, rows):
    if isinstance(text, dict):
        return await edit_screen(q.message, text, InlineKeyboardMarkup(rows))
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
async def welcome_view(uid=None):
    cur = None
    if S.STORE:
        try:
            cur = await S.run(S.STORE.get_setting, "welcome_text")
        except Exception:
            log.warning("could not read welcome text; using default")
    rows = []
    if S.STORE and uid is not None:
        try:
            if await S.run(S.STORE.trial_enabled) and not await S.run(S.STORE.has_sub_record, uid):
                rows.append([B(f"🎁 Start free {S.TRIAL_DAYS}-day trial", "sub:trial")])
        except Exception:
            log.warning("could not check trial eligibility")
    rows.append([B("💎 Purchase Subscription", "sub:home")])
    if S.STORE and await S.run(S.STORE.get_setting, "support_text"):
        rows.append([InlineKeyboardButton("🆘 Support", callback_data="sub:support")])
    return (cur or DEFAULT_WELCOME), rows


async def support_view():
    text = (await S.run(S.STORE.get_setting, "support_text")) if S.STORE else ""
    user = (await S.run(S.STORE.get_setting, "support_username")) if S.STORE else ""
    rows = []
    if user:
        rows.append([InlineKeyboardButton("💬 Contact support", url=f"https://t.me/{user}")])
    rows.append([B("⬅️ Back", "sub:back")])
    return (text or "🆘 <b>Support</b>\n\nContact the bot owner for help."), rows


async def support_admin_view(note=""):
    text = (await S.run(S.STORE.get_setting, "support_text")) or ""
    user = (await S.run(S.STORE.get_setting, "support_username")) or ""
    body = ("🆘 <b>SUPPORT</b>\n\nShown to users from the welcome screen.\n\n"
            f"Text: <b>{'set' if text else 'not set'}</b>\n"
            f"Username: <b>{h('@' + user) if user else 'not set'}</b>")
    lines = (note + "\n\n" if note else "") + body
    return lines, [[B("✏️ Set text", "sadm:supt")], [B("👤 Set username", "sadm:supu")],
                   [B("🗑 Clear support", "sadm:supclr")], [B("⬅️ Back", "sadm:home")]]


async def home_view(uid: int):
    if not S.STORE:
        return "💎 Subscriptions are temporarily unavailable. Please try again shortly.", [[B("⬅️ Back", "sub:back")]]
    plans = await S.run(S.STORE.list_plans, True)
    sub = await S.run(S.STORE.get_sub, uid)
    lines = ["💎 <b>SUBSCRIPTION PLANS</b>", "", _sub_line(sub), "", "Choose a duration:"]
    rows = []
    table_rows = []
    days_of = []
    if not plans:
        lines.append("No plans are available right now. Please contact the owner.")
    for days in sorted({int(p["duration_days"]) for p in plans}):
        group = [p for p in plans if int(p["duration_days"]) == days]
        cheapest = min(p["price_paise"] for p in group)
        days_of.append(days)
        table_rows.append([duration_label(days), S.fmt_inr(cheapest), str(len(group))])
        rows.append([B(f"📅 {duration_label(days)} · from {S.fmt_inr(cheapest)}", f"sub:dur:{days}")])
    if not S.payments_configured():
        lines += ["", "⚠️ Online payments are not enabled yet."]
    rows.append([B("📊 My Subscription", "sub:my")])
    rows.append([B("⬅️ Back", "sub:back")])
    status = para(re.sub(r"<[^>]+>", "", html.unescape(_sub_line(sub))))
    aligns = ["left", "center", "center"]
    blocks = [heading("💎 Subscription Plans"), status]
    cell_blocks = [heading("💎 Subscription Plans"), status]
    if table_rows:
        blocks += [rich_table(["Duration", "From", "Plans"], table_rows, aligns),
                   footer("Tap a duration below to see the account plans.")]
        cell_rows = [[d, f, c, button_cell("📅 Open", f"sub:dur:{days_of[i]}")] for i, (d, f, c) in enumerate(table_rows)]
        cell_blocks += [rich_table(["Duration", "From", "Plans", "Open"], cell_rows, aligns + ["center"]),
                        footer("Tap Open on a duration to see the account plans.")]
    else:
        msg = para("No plans are available right now. Please contact the owner.")
        blocks.append(msg)
        cell_blocks.append(msg)
    cell_markup = InlineKeyboardMarkup([[B("📊 My Subscription", "sub:my")], [B("⬅️ Back", "sub:back")]])
    return {"blocks": blocks, "cell_blocks": cell_blocks, "cell_markup": cell_markup,
            "fallback": "\n".join(lines)}, rows


async def duration_view(days: int):
    plans = [p for p in await S.run(S.STORE.list_plans, True) if int(p["duration_days"]) == int(days)]
    plans.sort(key=lambda p: (p.get("max_accounts") is None, p.get("max_accounts") or 0))
    lines = [f"📅 <b>{duration_label(days)}</b>", "", "Choose how many accounts you need:", ""]
    rows = []
    table_rows = []
    plan_ids = []
    for p in plans:
        plan_ids.append(p["plan_id"])
        caps = S.caps_for_plan(p)
        accts = "unlimited" if caps["max_accounts"] is None else caps["max_accounts"]
        groups_txt = "unlimited" if caps["max_groups"] is None else str(caps["max_groups"])
        lines.append(f"• <b>{accts} accounts</b> — {S.fmt_inr(p['price_paise'])} · {groups_txt} groups")
        table_rows.append([str(accts), S.fmt_inr(p["price_paise"]), groups_txt])
        rows.append([B(f"👥 {accts} accounts · {S.fmt_inr(p['price_paise'])}", f"sub:plan:{p['plan_id']}")])
    if not plans:
        lines.append("No plans for this duration right now.")
    rows.append([B("⬅️ Back", "sub:home")])
    aligns = ["center", "center", "center"]
    intro = para("Choose how many accounts you need.")
    blocks = [heading(duration_label(days)), intro]
    cell_blocks = [heading(duration_label(days)), intro]
    cell_markup = InlineKeyboardMarkup([[B("⬅️ Back", "sub:home")]])
    if table_rows:
        blocks += [rich_table(["Accounts", "Price", "Groups"], table_rows, aligns),
                   footer("Tap a plan below to buy it.")]
        cell_rows = [r + [button_cell("🛒 Buy", f"sub:plan:{pid}")] for r, pid in zip(table_rows, plan_ids)]
        cell_blocks += [rich_table(["Accounts", "Price", "Groups", "Buy"], cell_rows, aligns + ["center"]),
                        footer("Tap Buy on the plan you want.")]
    else:
        msg = para("No plans for this duration right now.")
        blocks.append(msg)
        cell_blocks.append(msg)
    return {"blocks": blocks, "cell_blocks": cell_blocks, "cell_markup": cell_markup,
            "fallback": "\n".join(lines)}, rows


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
    if st == "created":
        return (head + "\nTap <b>Pay via UPI QR</b> and scan the code with any UPI app. "
                       "Access activates automatically once the payment is verified.",
                [[B("📱 Pay via UPI QR", f"sub:qr:{ref}")],
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
            text, rows = await welcome_view(user.id)
            return await _edit(q, text, rows)
        if not S.STORE:
            return await q.answer("Subscriptions are temporarily unavailable.", show_alert=True)
        if action == "trial":
            await q.answer()
            ok, msg = await S.start_trial(user.id, user.first_name or "")
            if ok:
                msg += "\n\n👥 Free trial: 1 account. Bot and AI features are not included."
            rows = [[B("🚀 Open bot", "sub:back")]] if ok else [[B("💎 Plans", "sub:home")], [B("⬅️ Back", "sub:back")]]
            return await _edit(q, msg, rows)
        if action == "home":
            await q.answer()
            return await _edit(q, *await home_view(user.id))
        if action == "support":
            await q.answer()
            return await _edit(q, *await support_view())
        if action == "my":
            await q.answer()
            return await _edit(q, *await my_view(user.id))
        if action == "dur" and len(parts) == 3 and parts[2].isdigit():
            await q.answer()
            return await _edit(q, *await duration_view(int(parts[2])))
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
        if action == "qr" and len(parts) == 3:
            doc = await S.run(S.STORE.get_payment, parts[2])
            if not doc or int(doc["user_id"]) != user.id:
                return await q.answer("Payment not found.", show_alert=True)
            await q.answer("Generating QR…")
            try:
                doc = await S.create_qr(parts[2])
            except RuntimeError as e:
                return await ctx.bot.send_message(chat_id=q.message.chat_id, text=f"⚠️ {h(e)}", parse_mode=ParseMode.HTML)
            caption = (f"📱 <b>Scan &amp; pay {S.fmt_inr(doc['amount_paise'])}</b> with any UPI app.\n"
                       f"Plan: {h(doc['plan_name'])} ({doc['duration_days']} days)\n"
                       "This QR accepts one payment of exactly this amount. Access activates after verification.")
            return await ctx.bot.send_photo(chat_id=q.message.chat_id, photo=doc["qr_image_url"], caption=caption,
                                            parse_mode=ParseMode.HTML,
                                            reply_markup=InlineKeyboardMarkup([[B("🔄 I've Paid — Check Status", f"sub:qchk:{parts[2]}")]]))
        if action == "qchk" and len(parts) == 3:
            doc = await S.run(S.STORE.get_payment, parts[2])
            if not doc or int(doc["user_id"]) != user.id:
                return await q.answer("Payment not found.", show_alert=True)
            try:
                doc = await S.refresh_payment(parts[2])
            except Exception as e:
                log.warning("QR check failed: %s", e)
            if doc and doc["status"] == "paid":
                await q.answer("Payment verified ✅")
                text, rows = payment_view(doc)
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            return await q.answer("Not received yet. It can take a minute after you pay.", show_alert=True)
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


_BG_TASKS: set = set()


async def _record_user(uid, first_name):
    try:
        if await S.run(S.STORE.touch_user, uid, first_name):
            await S.log_event(f"👋 New user · {h(first_name or '-')} · <code>{uid}</code>")
    except Exception as exc:
        log.warning("could not register bot user: %s: %s", type(exc).__name__, exc)


async def start_gate(update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start for people WITHOUT access -> purchase screen. Approved users fall through to the existing /start."""
    user = update.effective_user
    if not user or user.is_bot or not update.message:
        return
    if user.id == CFG["admin_id"]:
        ctx.user_data.pop("sadm", None)
    if S.STORE:
        # Recorded in the background so the reply never waits on the database.
        task = asyncio.create_task(_record_user(user.id, user.first_name), name="touch_user")
        _BG_TASKS.add(task)                     # keep a reference until it finishes
        task.add_done_callback(_BG_TASKS.discard)
    if user.id in CFG["approved"] or user.id == CFG["admin_id"]:
        return
    if not _public_start_enabled():
        await update.message.reply_text("⛔ Access is by invitation only. Contact the bot owner.")
        raise ApplicationHandlerStop
    text, rows = await welcome_view(user.id)
    markup = InlineKeyboardMarkup(rows)
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Forbidden:
        log.info("user %s blocked the bot; welcome not delivered", user.id)
        raise ApplicationHandlerStop
    except BadRequest:
        log.warning("welcome text rejected by Telegram; sending default")
        try:
            await update.message.reply_text(DEFAULT_WELCOME, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Forbidden:
            log.info("user %s blocked the bot; welcome not delivered", user.id)
    raise ApplicationHandlerStop


# =============================================================== ADMIN SIDE
def _is_admin(user) -> bool:
    return bool(user and user.id == CFG["admin_id"])


# Co-admin sections. Owner-only unless listed here. "any" = needs only panel access.
SADM_SECTION = {
    "home": "any", "dash": "any", "cancel": "any",
    "users": "users", "find": "users", "su": "users",
    "pl": "subs", "plans": "subs", "pnew": "subs", "pt": "subs", "pf": "subs", "pe": "subs",
    "seed": "subs", "pay": "subs", "pd": "subs", "pr": "subs", "subs": "subs", "sv": "subs",
    "svy": "subs", "sx": "subs", "exp": "subs", "sweep": "subs", "sync": "subs", "trial": "subs",
    "extend": "subs", "audit": "subs",
    "bc": "bc", "bcgo": "bc", "sup": "bc", "supt": "bc", "supu": "bc", "supclr": "bc",
}
# Text-input steps (the act stored in the flow state).
SADM_TEXT_SECTION = {
    "pnew_name": "subs", "pnew_price": "subs", "pnew_days": "subs", "extend": "subs",
    "find": "users", "bc_wait": "bc", "bc_confirm": "bc", "sup_text": "bc", "sup_user": "bc",
}


def _can(uid: int, section: str) -> bool:
    check = CFG.get("can")
    if check is not None:
        return bool(check(uid, section))
    return uid == CFG["admin_id"]


def _can_open(user) -> bool:
    return bool(user) and any(_can(user.id, s) for s in ("users", "subs", "bc"))


def _button_allowed(uid: int, action: str) -> bool:
    sec = SADM_SECTION.get(action, "owner")
    if sec == "any":
        return any(_can(uid, s) for s in ("users", "subs", "bc"))
    if sec == "owner":
        return uid == CFG["admin_id"]
    return _can(uid, sec)


def _text_allowed(uid: int, act: str) -> bool:
    if act.startswith("pedit_"):
        return _can(uid, "subs")
    sec = SADM_TEXT_SECTION.get(act, "owner")
    if sec == "owner":
        return uid == CFG["admin_id"]
    return _can(uid, sec)


def _paged(prefix, page, total, size=S.PAGE_SIZE):
    row = []
    if page > 0:
        row.append(B("◀️ Prev", f"{prefix}:{page - 1}"))
    if (page + 1) * size < total:
        row.append(B("Next ▶️", f"{prefix}:{page + 1}"))
    return [row] if row else []


def _ok(flag) -> str:
    return "✅" if flag else "❌"


async def dash_view(note=""):
    st = await S.run(S.STORE.stats)
    approved = len([u for u in CFG["approved"] if u != CFG["admin_id"]])
    users_n = await S.run(S.STORE.count_users)
    trial_on = await S.run(S.STORE.trial_enabled)
    trials_n = await S.run(S.STORE.trials_started)
    keys_ok = S.payments_configured()
    lines = []
    if note:
        lines += [note, ""]
    lines += ["💎 <b>SUBSCRIPTION ADMIN</b>", "",
              "<b>Subscribers</b>",
              f"  ✅ Active: <b>{st['active']}</b>  ·  ⌛ Expired: {st['expired']}  ·  🚫 Ended: {st['revoked']}",
              f"  ⏰ Expiring in 3 days: {st['exp3']}  ·  in 7 days: {st['exp7']}",
              f"  🎁 Free trial: {'ON' if trial_on else 'OFF'}  ·  trials started: {trials_n}",
              "", "<b>Payments</b>",
              f"  💰 Revenue: <b>{S.fmt_inr(st['revenue_all'])}</b> total · {S.fmt_inr(st['revenue_30d'])} last 30 days",
              f"  ✅ Paid: {st['pay_paid']}  ·  ⏳ Pending: {st['pay_pending']}  ·  ⚠️ Other: {st['pay_other']}",
              "", "<b>Users</b>",
              f"  🙋 Approved: {approved}  ·  📣 Bot users (broadcast list): {users_n}"]
    if st["unfulfilled"] or st["review"]:
        lines += ["", f"🚨 <b>Attention</b>: {st['unfulfilled']} paid but not activated · {st['review']} need review"]
    lines += ["", "<b>System</b>",
              f"  Razorpay keys {_ok(keys_ok)}  ·  Public URL {_ok(S.public_base().startswith('https://'))}",
              f"  Payment verification by polling (every 1 min) {_ok(keys_ok)}  ·  webhook not required"]
    rows = [[B("💳 Payments", "sadm:pay:all:0"), B("⏳ Pending", "sadm:pay:pending:0")],
            [B("👥 Subscribers", "sadm:subs:all:0"), B("🔎 Find User", "sadm:find")],
            [B("📦 Plans", "sadm:plans"), B("⏰ Expiry Check", "sadm:exp")],
            [B("📜 Log Channel", "sadm:logch"), B("🆘 Support", "sadm:sup")],
            [B("🙋 Approved Users", "sadm:users"), B("📣 Broadcast", "sadm:bc")],
            [B("👋 Welcome Msg", "sadm:wm"), B("🧾 Audit Log", "sadm:audit:0")],
            [B("🎁 Toggle free trial", "sadm:trial")],
            [B("🔄 Verify pending now", "sadm:sync")],
            [B("⬅️ Back", "adm:home")]]
    return "\n".join(lines), rows


# (name, price in rupees, duration days, accounts). Prices are placeholders; admin edits them per plan.
# (duration days, account tiers and prices in rupees). Admin can change every price afterwards.
STARTER_GRID = {
    7: [(2, 49), (5, 99), (10, 179), (15, 249)],
    15: [(2, 79), (5, 149), (10, 269), (15, 369)],
    30: [(2, 129), (5, 249), (10, 449), (15, 599)],
    60: [(2, 229), (5, 449), (10, 799), (15, 1099)],
    90: [(2, 329), (5, 649), (10, 1099), (15, 1499)],
}
STARTER_PLANS = [(f"{duration_label(d)} · {n} Accounts", price, d, n)
                 for d, tiers in STARTER_GRID.items() for n, price in tiers]


async def seed_starter_plans() -> tuple[int, int]:
    """Creates the starter plans. Plans with the same name are skipped, so it is safe to press twice."""
    existing = {(int(p["duration_days"]), p.get("max_accounts")) for p in await S.run(S.STORE.list_plans)}
    made = skipped = 0
    for name, rupees, days, accounts in STARTER_PLANS:
        if (days, accounts) in existing:          # same duration and account tier already exists
            skipped += 1
            continue
        plan = await S.run(S.STORE.create_plan, name, rupees * 100, days)
        await S.run(S.STORE.update_plan, plan["plan_id"], max_accounts=accounts, max_groups=10,
                    bot_feature=True, ai_feature=True)
        made += 1
    return made, skipped


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
    rows += [[B("⚡ Add starter plans", "sadm:seed")], [B("➕ New Plan", "sadm:pnew")], [B("⬅️ Back", "sadm:home")]]
    return "\n".join(lines), rows


async def plan_view(pid, note=""):
    p = await S.run(S.STORE.get_plan, pid)
    if not p:
        return "Plan not found.", [[B("⬅️ Back", "sadm:plans")]]
    caps = S.caps_for_plan(p)
    text = (f"📦 <b>Plan #{p['plan_id']}: {h(p['name'])}</b>\n\nPrice: <b>{S.fmt_inr(p['price_paise'])}</b>\n"
            f"Duration: <b>{p['duration_days']} days</b>\nAccounts: <b>{'unlimited' if caps['max_accounts'] is None else caps['max_accounts']}</b>\n"
            f"Target groups: <b>{caps['max_groups']}</b>\n"
            f"Bot feature: <b>{'ON' if caps['bot'] else 'OFF'}</b> · AI auto reply: <b>{'ON' if caps['ai'] else 'OFF'}</b>\n"
            f"Status: {'🟢 Enabled' if p['enabled'] else '🔴 Disabled'}\n\n"
            "Price/duration changes apply to new purchases only; payments already started keep their original amount.")
    if note:
        text = note + "\n\n" + text
    pid = p["plan_id"]
    rows = [[B("✏️ Name", f"sadm:pe:{pid}:name"), B("💰 Price", f"sadm:pe:{pid}:price")],
            [B("⏳ Duration", f"sadm:pe:{pid}:days"), B("🔴 Disable" if p["enabled"] else "🟢 Enable", f"sadm:pt:{pid}")],
            [B("👥 Accounts", f"sadm:pe:{pid}:accounts"), B("📣 Groups", f"sadm:pe:{pid}:groups")],
            [B(f"🤖 Bot: {'ON' if caps['bot'] else 'OFF'}", f"sadm:pf:{pid}:bot"),
             B(f"🧠 AI: {'ON' if caps['ai'] else 'OFF'}", f"sadm:pf:{pid}:ai")],
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


async def logch_view(note=""):
    cur = (await S.run(S.STORE.get_setting, "log_channel")) if S.STORE else ""
    pay = (await S.run(S.STORE.get_setting, "payment_channel")) if S.STORE else ""
    lines = ["📜 <b>LOG CHANNEL</b>", "",
             "Bot events are posted here: new users, accounts added or removed, operators added or removed, "
             "trials, subscriptions and expiries.", "",
             f"Channel: <b>{h(cur) if cur else 'not set'}</b>", "",
             "💳 <b>PAYMENT CHANNEL</b>", "",
             "Payment started and payment verified events go here. If not set, they go to the log channel.", "",
             f"Payment channel: <b>{h(pay) if pay else 'not set (uses log channel)'}</b>", "",
             "Add this bot as an admin in each channel first (permission to post)."]
    rows = [[B("✏️ Set channel", "sadm:logset")], [B("🧪 Send test", "sadm:logtest")],
            [B("🗑 Remove channel", "sadm:logclr")],
            [B("💳 Set payment channel", "sadm:paylog")], [B("🧪 Test payment channel", "sadm:paylogtest")],
            [B("🗑 Remove payment channel", "sadm:paylogclr")], [B("⬅️ Back", "sadm:home")]]
    return (note + "\n\n" if note else "") + "\n".join(lines), rows


async def sadm_callback(update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _can_open(q.from_user):
        return await q.answer("⛔ Admin only", show_alert=True)
    if not q.message or q.message.chat.type != "private":
        return await q.answer()
    if not S.STORE:
        return await q.answer("Subscription service is not running (check logs).", show_alert=True)
    parts = q.data.split(":")
    a = parts[1] if len(parts) > 1 else "home"
    if not _button_allowed(q.from_user.id, a):
        return await q.answer("⛔ Your admin access does not include this section.", show_alert=True)
    actor = q.from_user.id
    try:
        await q.answer()
        if a in ("home", "cancel"):
            ctx.user_data.pop("sadm", None)
            return await _edit(q, *await dash_view())
        if a == "plans":
            ctx.user_data.pop("sadm", None)
            return await _edit(q, *await plans_view())
        if a == "sup":
            return await _edit(q, *await support_admin_view())
        if a == "supt":
            return await _edit(q, *_prompt(ctx, "sup_text", "✏️ Send the support text. HTML formatting is kept "
                                           "(bold, links). Max 1500 characters.", back="sadm:sup"))
        if a == "supu":
            return await _edit(q, *_prompt(ctx, "sup_user", "👤 Send the support username, for example "
                                           "@YourSupport. Send 'none' to remove it.", back="sadm:sup"))
        if a == "supclr":
            await S.run(S.STORE.set_setting, "support_text", "")
            await S.run(S.STORE.set_setting, "support_username", "")
            return await _edit(q, *await support_admin_view("🗑 Support cleared."))
        if a == "seed":
            made, skipped = await seed_starter_plans()
            await S.run(S.STORE.audit, f"admin:{actor}", "plans_seeded", "-", {"made": made, "skipped": skipped})
            text, rows = await plans_view()
            return await _edit(q, f"✅ Starter plans: added <b>{made}</b>, already existed <b>{skipped}</b>.\n\n" + text, rows)
        if a == "logch":
            return await _edit(q, *await logch_view())
        if a == "logset":
            return await _edit(q, *_prompt(ctx, "logch", "📜 Send the log channel ID (like -1001234567890) "
                                           "or the @channel username.", back="sadm:logch"))
        if a == "logclr":
            await S.set_log_channel("")
            return await _edit(q, *await logch_view("🗑 Log channel removed."))
        if a == "paylog":
            return await _edit(q, *_prompt(ctx, "paylog", "💳 Send the <b>payment</b> channel ID (like -1001234567890) "
                                           "or the @channel username.", back="sadm:logch"))
        if a == "paylogclr":
            await S.set_payment_channel("")
            return await _edit(q, *await logch_view("🗑 Payment channel removed. Payments go to the log channel now."))
        if a == "paylogtest":
            ok = await S.payment_log("🧪 Test message: the payment channel is working.")
            return await _edit(q, *await logch_view("✅ Test sent." if ok else
                                                   "❌ Could not post. Check the channel ID and that the bot is an admin there."))
        if a == "logtest":
            ok = await S.log_event("🧪 Test message: the log channel is working.")
            return await _edit(q, *await logch_view("✅ Test sent." if ok else
                                                   "❌ Could not post. Check the channel ID and that the bot is an admin there."))
        if a == "pl":
            return await _edit(q, *await plan_view(parts[2]))
        if a == "pnew":
            return await _edit(q, *_prompt(ctx, "pnew_name", "➕ <b>NEW PLAN</b>\n\nSend the plan name (2–40 characters):"))
        if a == "pe":
            field = parts[3]
            ask = {"name": "Send the new plan name:", "price": "Send the new price in rupees (e.g. 199):",
                   "days": "Send the new duration in days (e.g. 30):",
                   "accounts": "Send how many Telegram accounts this plan allows (1–500):",
                   "groups": "Send how many target groups this plan allows (1–1000):"}[field]
            return await _edit(q, *_prompt(ctx, "pedit_" + field, "✏️ " + ask, pid=parts[2]))
        if a == "pf":
            p = await S.run(S.STORE.get_plan, parts[2])
            if p and parts[3] in ("bot", "ai"):
                key = {"bot": "bot_feature", "ai": "ai_feature"}[parts[3]]
                await S.run(S.STORE.update_plan, parts[2], **{key: not S.caps_for_plan(p)[parts[3]]})
                await S.run(S.STORE.audit, f"admin:{actor}", "plan_feature", parts[2], {key: not S.caps_for_plan(p)[parts[3]]})
            return await _edit(q, *await plan_view(parts[2]))
        if a == "pt":
            p = await S.run(S.STORE.get_plan, parts[2])
            if p:
                await S.run(S.STORE.update_plan, parts[2], enabled=not p["enabled"])
                await S.run(S.STORE.audit, f"admin:{actor}", "plan_toggle", parts[2], {"enabled": not p["enabled"]})
            return await _edit(q, *await plan_view(parts[2]))
        if a == "pay":
            return await _edit(q, *await payments_view(parts[2], int(parts[3])))
        if a == "pd":
            return await _edit(q, *await payment_admin_view(parts[2]))
        if a == "pr":
            doc = await S.run(S.STORE.get_payment, parts[2])
            note = "ℹ️ Nothing to re-verify."
            if doc:
                try:
                    await S.refresh_payment(parts[2])
                    note = "🔄 Re-verified with Razorpay."
                    await S.run(S.STORE.audit, f"admin:{actor}", "payment_reverify", parts[2])
                except Exception as e:
                    note = f"⚠️ Re-verify failed: {h(e)}"
            return await _edit(q, *await payment_admin_view(parts[2], note))
        if a == "subs":
            return await _edit(q, *await subs_view(parts[2], int(parts[3])))
        if a == "su":
            return await _edit(q, *await user_admin_view(int(parts[2])))
        if a == "users":
            return await _edit(q, *await approved_users_view())
        if a == "find":
            return await _edit(q, *_prompt(ctx, "find", "🔎 <b>FIND USER</b>\n\nSend the Telegram user ID:"))
        if a == "sx":
            return await _edit(q, *_prompt(ctx, "extend", f"➕ <b>EXTEND / GRANT</b> for <code>{int(parts[2])}</code>\n\n"
                                           "Send number of days to add. Access is removed automatically when the period ends.",
                                           uid=int(parts[2])))
        if a == "sv":
            uid = int(parts[2])
            return await _edit(q, f"🚫 Revoke subscription of <code>{uid}</code>?\n\nTheir bot access is removed immediately.",
                               [[B("✅ Yes, revoke", f"sadm:svy:{uid}")], [B("❌ Cancel", f"sadm:su:{uid}")]])
        if a == "svy":
            uid = int(parts[2])
            done = await S.admin_revoke(uid, actor)
            return await _edit(q, *await user_admin_view(uid, "🚫 Subscription revoked." if done else "ℹ️ No active subscription."))
        if a == "exp":
            return await _edit(q, *await expiry_view())
        if a == "sweep":
            await S.expiry_sweep()
            await S.run(S.STORE.audit, f"admin:{actor}", "manual_sweep", "all")
            return await _edit(q, *await expiry_view("✅ Expiry sweep completed."))
        if a == "trial":
            now_on = await S.run(S.STORE.trial_enabled)
            await S.run(S.STORE.set_setting, "trial_enabled", not now_on)
            await S.run(S.STORE.audit, f"admin:{actor}", "trial_toggle", "trial_enabled", {"enabled": not now_on})
            return await _edit(q, *await dash_view(f"🎁 Free trial is now {'OFF' if now_on else 'ON'}."))
        if a == "sync":
            res = await S.verify_pending()
            note = (f"🔄 Checked <b>{res['checked']}</b> pending · <b>{res['paid']}</b> newly paid · "
                    f"<b>{res['activated']}</b> activated.")
            return await _edit(q, *await dash_view(note))
        if a == "bc":
            if BC["running"]:
                return await _edit(q, "📣 A broadcast is already running. Wait for its report.", [[B("⬅️ Back", "sadm:home")]])
            n = await S.run(S.STORE.count_users)
            return await _edit(q, *_prompt(ctx, "bc_wait", f"📣 <b>BROADCAST</b>\n\nRecipients: <b>{n}</b> registered users.\n\n"
                                           "Send the message to broadcast (text, photo, video, etc.). It is copied exactly as you send it. "
                                           "Your next message is previewed for confirmation."))
        if a == "bcgo":
            st = ctx.user_data.get("sadm") or {}
            if st.get("act") != "bc_confirm":
                return await _edit(q, "ℹ️ Nothing to send. Start the broadcast again.", [[B("⬅️ Back", "sadm:home")]])
            if BC["running"]:
                return await _edit(q, "📣 A broadcast is already running.", [[B("⬅️ Back", "sadm:home")]])
            ctx.user_data.pop("sadm", None)
            BC["running"] = True
            BC["task"] = asyncio.create_task(_run_broadcast(ctx.bot, actor, st["from"], st["mid"]))
            return await _edit(q, "📣 Broadcast started. The report will be sent here when it finishes.", [[B("⬅️ Back", "sadm:home")]])
        if a == "wm":
            return await _edit(q, *await welcome_admin_view())
        if a == "wme":
            return await _edit(q, *_prompt(ctx, "wm_text", "✏️ Send the new welcome message now. You can use Telegram formatting "
                                           "(bold, italic, links). Max 3500 characters."))
        if a == "wmr":
            await S.run(S.STORE.del_setting, "welcome_text")
            await S.run(S.STORE.audit, f"admin:{actor}", "welcome_reset", "welcome_text")
            return await _edit(q, *await welcome_admin_view("↩️ Reset to the default welcome message."))
        if a == "audit":
            return await _edit(q, *await audit_view(int(parts[2])))
    except Exception:
        log.exception("sadm_callback error")
        try:
            await q.answer("Error — see logs.", show_alert=True)
        except Exception:
            pass


async def sadm_text(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Text input for admin flows. Consumes the message only while a flow is active."""
    user = update.effective_user
    msg = update.message
    if not _can_open(user) or not msg or not S.STORE:
        return
    st = ctx.user_data.get("sadm")
    if not st:
        return
    if time.time() - st["ts"] > STATE_TTL:
        ctx.user_data.pop("sadm", None)
        return
    act = st["act"]
    if not _text_allowed(user.id, act):
        return
    if act in ("bc_wait", "bc_confirm", "wm_text"):
        try:
            text, rows = await _admin_special(ctx, msg, st, user)
        except ValueError as e:
            text, rows = f"❌ {h(e)}\n\nTry again or tap Cancel.", [[B("❌ Cancel", "sadm:cancel")]]
        except Exception:
            log.exception("sadm special step error")
            ctx.user_data.pop("sadm", None)
            text, rows = "❌ Something went wrong. See the logs.", [[B("⬅️ Back", "sadm:home")]]
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
                             disable_web_page_preview=True)
        raise ApplicationHandlerStop
    txt = (msg.text or "").strip()
    actor = user.id
    try:
        if act == "pnew_name":
            st.update(name=S.parse_name(txt), act="pnew_price", ts=time.time())
            text, rows = "💰 Send the price in rupees (e.g. 199):", [[B("❌ Cancel", "sadm:cancel")]]
        elif act == "pnew_price":
            st.update(price=S.parse_price_paise(txt), act="pnew_days", ts=time.time())
            text, rows = "⏳ Send the duration in days (e.g. 30):", [[B("❌ Cancel", "sadm:cancel")]]
        elif act == "pnew_days":
            plan = await S.run(S.STORE.create_plan, st["name"], st["price"], S.parse_days(txt))
            ctx.user_data.pop("sadm", None)
            await S.run(S.STORE.audit, f"admin:{actor}", "plan_create", plan["plan_id"], {"name": plan["name"]})
            text, rows = await plan_view(plan["plan_id"], "✅ Plan created and enabled.")
        elif act.startswith("pedit_"):
            field = act[6:]
            val = {"name": S.parse_name, "price": S.parse_price_paise, "days": S.parse_days,
                   "accounts": S.parse_accounts, "groups": S.parse_groups}[field](txt)
            key = {"name": "name", "price": "price_paise", "days": "duration_days", "accounts": "max_accounts",
                   "groups": "max_groups"}[field]
            await S.run(S.STORE.update_plan, st["pid"], **{key: val})
            ctx.user_data.pop("sadm", None)
            await S.run(S.STORE.audit, f"admin:{actor}", "plan_edit", st["pid"], {field: val})
            text, rows = await plan_view(st["pid"], "✅ Plan updated.")
        elif act == "paylog":
            chat = txt.strip()
            if not chat or len(chat) > 64 or " " in chat:
                raise ValueError("Send a channel ID like -1001234567890 or @channelname.")
            await S.set_payment_channel(chat)
            if not await S.payment_log("✅ Payment channel connected. Payment events will be posted here."):
                await S.set_payment_channel("")
                raise ValueError("Could not post to that channel. Add this bot as an admin there and send the ID again.")
            ctx.user_data.pop("sadm", None)
            text, rows = await logch_view("✅ Payment channel saved.")
        elif act == "logch":
            chat = txt.strip()
            if not chat or len(chat) > 64 or " " in chat:
                raise ValueError("Send a channel ID like -1001234567890 or @channelname.")
            await S.set_log_channel(chat)
            if not await S.log_event("✅ Log channel connected. Bot events will be posted here."):
                await S.set_log_channel("")
                raise ValueError("Could not post to that channel. Add this bot as an admin there and send the ID again.")
            ctx.user_data.pop("sadm", None)
            text, rows = await logch_view("✅ Log channel saved.")
        elif act == "sup_text":
            body = msg.text_html or ""
            if not body or len(body) > 1500:
                raise ValueError("Send 1–1500 characters.")
            try:
                await ctx.bot.send_message(chat_id=user.id, text=body, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                raise ValueError(f"Telegram rejected the formatting ({e.message}). Edit and send again.")
            await S.run(S.STORE.set_setting, "support_text", body)
            ctx.user_data.pop("sadm", None)
            text, rows = await support_admin_view("✅ Support text saved.")
        elif act == "sup_user":
            name = txt.strip().lstrip("@")
            if name.lower() == "none":
                name = ""
            elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
                raise ValueError("Send a valid Telegram username like @YourSupport, or 'none'.")
            await S.run(S.STORE.set_setting, "support_username", name)
            ctx.user_data.pop("sadm", None)
            text, rows = await support_admin_view("✅ Support username saved.")
        elif act == "find":
            if not txt.isdigit() or int(txt) <= 0:
                raise ValueError("Send a numeric Telegram user ID.")
            ctx.user_data.pop("sadm", None)
            text, rows = await user_admin_view(int(txt))
        elif act == "extend":
            days = S.parse_days(txt)
            ctx.user_data.pop("sadm", None)
            await S.admin_extend(st["uid"], days, actor)
            text, rows = await user_admin_view(st["uid"], f"✅ Added {days} day(s).")
        else:
            ctx.user_data.pop("sadm", None)
            return
    except ValueError as e:
        text, rows = f"❌ {h(e)}\n\nTry again or tap Cancel.", [[B("❌ Cancel", "sadm:cancel")]]
    except Exception:
        log.exception("sadm_text error")
        ctx.user_data.pop("sadm", None)
        text, rows = "❌ Something went wrong. See the logs.", [[B("⬅️ Back", "sadm:home")]]
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
                         disable_web_page_preview=True)
    raise ApplicationHandlerStop


async def welcome_admin_view(note=""):
    cur = None
    try:
        cur = await S.run(S.STORE.get_setting, "welcome_text")
    except Exception:
        log.warning("could not read welcome text")
    head = (note + "\n\n") if note else ""
    text = (head + "👋 <b>WELCOME MESSAGE</b>\n\nShown to people who send /start without an active subscription.\n\n"
            + ("<i>Custom message active.</i>" if cur else "<i>Using the default message.</i>") + "\n\n"
            "— Preview —\n" + (cur or DEFAULT_WELCOME))
    return text, [[B("✏️ Edit", "sadm:wme")], [B("↩️ Reset to default", "sadm:wmr")], [B("⬅️ Back", "sadm:home")]]


async def _admin_special(ctx, msg, st, user):
    """Broadcast and welcome-message steps. Returns (text, rows)."""
    act = st["act"]
    if act == "bc_wait":
        ctx.user_data["sadm"] = {"act": "bc_confirm", "ts": time.time(), "from": msg.chat_id, "mid": msg.message_id}
        n = await S.run(S.STORE.count_users)
        try:
            await ctx.bot.copy_message(chat_id=user.id, from_chat_id=msg.chat_id, message_id=msg.message_id)
        except TelegramError:
            ctx.user_data.pop("sadm", None)
            raise ValueError("Telegram could not copy this message. Send a different one.")
        return (f"📣 <b>CONFIRM BROADCAST</b>\n\nThe message above goes to <b>{n}</b> registered users "
                "(users who blocked the bot are skipped).\n\nSend it now?",
                [[B("✅ Send now", "sadm:bcgo")], [B("❌ Cancel", "sadm:cancel")]])
    if act == "bc_confirm":
        ctx.user_data.pop("sadm", None)
        return "ℹ️ Broadcast cancelled. Open Broadcast again to start over.", [[B("⬅️ Back", "sadm:home")]]
    if act == "wm_text":
        if not msg.text:
            raise ValueError("Send the welcome message as text.")
        body = msg.text_html
        if len(body) > 3500:
            raise ValueError("Too long: keep it under 3500 characters.")
        try:
            await ctx.bot.send_message(chat_id=user.id, text=body, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except BadRequest as e:
            raise ValueError(f"Telegram rejected the formatting ({e.message}). Edit it and send again.")
        await S.run(S.STORE.set_setting, "welcome_text", body)
        await S.run(S.STORE.audit, f"admin:{user.id}", "welcome_edit", "welcome_text", {"chars": len(body)})
        ctx.user_data.pop("sadm", None)
        return ("✅ Welcome message saved. The preview above is what new users will see.",
                [[B("👋 Welcome Msg", "sadm:wm")], [B("⬅️ Back", "sadm:home")]])
    return "ℹ️ Nothing to do.", [[B("⬅️ Back", "sadm:home")]]


async def _run_broadcast(bot, admin_id: int, from_chat: int, mid: int):
    started = time.time()
    sent = failed = blocked = 0
    try:
        recips = await S.run(S.STORE.recipients)
        for uid in recips:
            for attempt in (1, 2):
                try:
                    await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=mid)
                    sent += 1
                    break
                except RetryAfter as e:
                    if attempt == 2:
                        failed += 1
                        break
                    await asyncio.sleep(float(e.retry_after) + 0.5)
                except Forbidden:
                    blocked += 1
                    await S.run(S.STORE.mark_blocked, uid)
                    break
                except TelegramError as e:
                    failed += 1
                    log.info("broadcast to %s failed: %s", uid, type(e).__name__)
                    break
            await asyncio.sleep(0.05)          # stays well under Telegram's ~30 msg/s limit
        duration = int(time.time() - started)
        stats = {"total": len(recips), "sent": sent, "blocked": blocked, "failed": failed, "seconds": duration}
        await S.run(S.STORE.log_broadcast, {"created_at": S.now_utc(), "by": admin_id, **stats})
        await S.run(S.STORE.audit, f"admin:{admin_id}", "broadcast", str(mid), stats)
        report = (f"📣 <b>Broadcast finished</b>\n\nRecipients: <b>{stats['total']}</b>\nSent: <b>{sent}</b>\n"
                  f"Blocked bot (removed from list): {blocked}\nFailed: {failed}\nTime: {duration}s")
    except Exception:
        log.exception("broadcast crashed")
        report = f"⚠️ Broadcast stopped with an error after {sent} sent. See the logs."
    finally:
        BC["running"] = False
    try:
        await bot.send_message(chat_id=admin_id, text=report, parse_mode=ParseMode.HTML)
    except TelegramError:
        log.warning("could not send broadcast report")


def register_handlers(app):
    # group=-1 handlers only act for their own cases and otherwise let the existing handlers run unchanged.
    app.add_handler(CommandHandler("start", start_gate, filters=filters.ChatType.PRIVATE), group=-1)
    app.add_handler(MessageHandler(~filters.COMMAND & filters.ChatType.PRIVATE, sadm_text), group=-1)
    app.add_handler(CallbackQueryHandler(sub_callback, pattern=r"^sub:"))
    app.add_handler(CallbackQueryHandler(sadm_callback, pattern=r"^sadm:"))
