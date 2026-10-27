# Telegram Ads Promotion Bot — MongoDB-only persistent data

This build keeps the multi-user promotion features, per-account pacing/FloodWait handling, automatic group updates, separate account stats/logs, and the **existing-DM-only** manual DM broadcast rule.

## Persistent storage

The running bot no longer uses SQLite or `promo.db`.

MongoDB collections include:

- `sessions` — encrypted Telethon StringSessions, scoped by operator (`owner_id`)
- `tenant_settings` — interval, group delay, sending mode, auto-reply settings, account cooldowns
- `promo_posts` — saved promotion post metadata, scoped by operator
- `tenant_counters` — per-operator post IDs/cursors
- `approved_operators` — users granted with `/add <uid>`

Downloaded promotion media still stays in the local `media/` / per-operator media directories because Telegram media files are files, not SQLite records. Keep the same `FERNET_KEY` to decrypt existing MongoDB sessions.

## Upgrade from the previous SQLite build

1. Keep your current `.env`, `promo.db`, `operators/`, `media/`, and MongoDB database as a backup.
2. Install updated requirements.
3. Run the one-time migration **before the first start**:

```bash
python3 migrate_sqlite_to_mongo.py
```

It imports the owner's old `promo.db` and any `operators/<uid>/promo.db` settings/posts into MongoDB while preserving post IDs and media paths. The migration script is idempotent: rerunning it updates the same Mongo documents instead of duplicating them.

4. Start the bot:

```bash
python3 bot.py
```

After you verify saved posts/settings in the bot, the old `.db` files are no longer required by runtime and can be archived or deleted.

## Multi-user permissions

Only the owner (`ADMIN_ID`) can manage operators:

```text
/add <uid>   grant access
/rem <uid>   revoke access
/users       list approved users
```

Each operator's sessions, posts and settings are isolated by `owner_id` in MongoDB.

## Manual DM Broadcast target rule

`📨 DM Broadcast` targets only private users available to each connected Telegram account **when that account has already sent at least one outgoing private message to that user before**.

So it includes existing DM users whether they are contacts or non-contacts, while excluding:

- contact-only entries with no existing outgoing DM
- inbound-only chats that the account never replied to
- arbitrary users discovered by username/group membership but never messaged

Duplicate user IDs are removed. Telegram FloodWait/PeerFlood restrictions are still respected.

## Group promotion pacing

- Successful group send → configured group delay applies.
- Non-Flood failures/skips (write forbidden, invalid/deleted target, etc.) → next group is attempted immediately.
- FloodWait → Telegram-required cooldown is persisted in MongoDB and the affected account resumes after it expires.
- PeerFlood/account restrictions are not bypassed.
- Different saved Telegram accounts work independently/concurrently; each account has its own progress/stats/logs.

## Environment

Required:

```env
BOT_TOKEN=...
ADMIN_ID=123456789
API_ID=12345678
API_HASH=...
FERNET_KEY=...
MONGO_URI=mongodb+srv://...
MONGO_DB=promo_bot
MEDIA_DIR=media
```

There is no `DB_PATH` setting anymore.

Optional pacing values remain in `.env.example`.

## Temporary connection retry
Temporary `ConnectionError`, `OSError`, and asyncio timeout failures now retry on the affected account only with backoff: 2s, 5s, 10s, then 20s. Promotion retries the same target after reconnecting. FloodWait, PeerFlood, revoked authorization, and duplicated authorization keys are not treated as temporary network failures.

## Bot Account group-reply mode

Open `🤖 Bot Accounts` in the control bot, then `➕ Add Bot` and send a BotFather token. The token is validated and encrypted with the existing `FERNET_KEY` before being stored in the operator's MongoDB scope.

For each enabled bot account, the service listens for group/supergroup messages and replies directly to the user's original message using the same enabled promotion posts saved in **Posts**. The current Rotate/Random post mode is reused. The cooldown key is **bot + group + user**, so the same user receives at most one reply per configured cooldown window in the same group (default 10 minutes). Another user in the group can receive a reply immediately, and the same user in a different group has a separate cooldown. Cooldowns are persisted in MongoDB.

A bot does not need to be a group administrator to send messages when the group permits normal member messaging. However, to receive every ordinary group message while not an administrator, disable that bot's Privacy Mode in BotFather (`/setprivacy`). The bot must be a member of the group and have permission to send messages. Bot messages are ignored.

The control bot token cannot be added as a Bot Account. If the same added bot token is being polled by another process/server, Telegram may return a polling conflict; run each Bot API token from only one active polling process.


## Categorized main menu + Bio manager
- Main page is grouped into Promotion, Posts, Accounts, Auto Reply, Edit/Set Bio, and Monitoring.
- Edit/Set Bio -> Bot Bio / User Bio -> Individual / Set All.
- Individual user list shows active connected Telethon accounts.
- Individual bot list shows active listening bot accounts.
- Bot bio updates Telegram bot short description (About text).
- User bio updates the connected Telegram user's profile About field.
- Send CLEAR in the bio prompt to remove the bio.

## Join Groups queue
Use **Accounts → Join Groups → Add Group Links** and send one Telegram link per line. Public `https://t.me/name`, `@name`, private `https://t.me/+hash`, and `https://t.me/joinchat/hash` links are accepted. The same list is queued for each currently active real-user session. Each account attempts at most one queued join/request every 5 minutes; Telegram FloodWait/restrictions are obeyed and one account does not stop the others.
