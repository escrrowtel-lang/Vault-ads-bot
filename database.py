import asyncio
import random
from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient, ReturnDocument


class Database:
    """MongoDB-backed tenant settings and promotion-post storage.

    The public async API intentionally matches the former SQLite Database class
    so the rest of the bot does not need to know which storage engine is used.
    Every document is scoped by owner_id, keeping approved operators isolated.
    """

    def __init__(self, uri: str, db_name: str, owner_id: int):
        self.uri = uri
        self.db_name = db_name
        self.owner_id = int(owner_id)
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
        mongo_db = self.client[db_name]
        self.settings = mongo_db["tenant_settings"]
        self.posts = mongo_db["promo_posts"]
        self.counters = mongo_db["tenant_counters"]
        self.bot_accounts = mongo_db["bot_accounts"]
        self.bot_reply_cooldowns = mongo_db["bot_reply_cooldowns"]
        self.bot_account_groups = mongo_db["bot_account_groups"]
        self.bot_account_events = mongo_db["bot_account_events"]
        self.join_queue = mongo_db["account_join_queue"]

    async def init(self):
        def _init():
            self.client.admin.command("ping")
            self.settings.create_index([("owner_id", ASCENDING), ("key", ASCENDING)], unique=True)
            self.posts.create_index([("owner_id", ASCENDING), ("id", ASCENDING)], unique=True)
            self.posts.create_index([("owner_id", ASCENDING), ("enabled", ASCENDING), ("id", ASCENDING)])
            self.bot_accounts.create_index([("owner_id", ASCENDING), ("bot_id", ASCENDING)], unique=True)
            self.bot_accounts.create_index([("owner_id", ASCENDING), ("enabled", ASCENDING)])
            self.bot_reply_cooldowns.create_index([("owner_id", ASCENDING), ("bot_id", ASCENDING), ("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            self.bot_reply_cooldowns.create_index([("updated_at", ASCENDING)])
            self.bot_account_groups.create_index([("owner_id", ASCENDING), ("bot_id", ASCENDING), ("chat_id", ASCENDING)], unique=True)
            self.bot_account_groups.create_index([("owner_id", ASCENDING), ("bot_id", ASCENDING), ("active", ASCENDING)])
            self.bot_account_events.create_index([("owner_id", ASCENDING), ("bot_id", ASCENDING), ("created_at", ASCENDING)])
            self.join_queue.create_index([("owner_id", ASCENDING), ("session_id", ASCENDING), ("normalized_link", ASCENDING)], unique=True)
            self.join_queue.create_index([("owner_id", ASCENDING), ("session_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
            # Keep the post counter at least as high as the largest existing id.
            last = self.posts.find_one({"owner_id": self.owner_id}, sort=[("id", -1)], projection={"id": 1})
            max_id = int(last.get("id", 0)) if last else 0
            if max_id:
                self.counters.update_one(
                    {"_id": f"post:{self.owner_id}"},
                    {"$max": {"seq": max_id}},
                    upsert=True,
                )
        await asyncio.to_thread(_init)

    async def set_setting(self, key: str, value: str):
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            self.settings.update_one,
            {"owner_id": self.owner_id, "key": key},
            {"$set": {"value": str(value), "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            True,
        )

    async def get_setting(self, key: str, default=None):
        def _get():
            doc = self.settings.find_one({"owner_id": self.owner_id, "key": key}, {"value": 1})
            return doc.get("value") if doc else default
        return await asyncio.to_thread(_get)

    async def add_post(self, kind, html_text, media_path=None, media_type=None):
        now = datetime.now(timezone.utc)
        def _add():
            counter = self.counters.find_one_and_update(
                {"_id": f"post:{self.owner_id}"},
                {"$inc": {"seq": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            post_id = int(counter["seq"])
            self.posts.insert_one({
                "owner_id": self.owner_id,
                "id": post_id,
                "kind": kind,
                "html_text": html_text,
                "media_path": media_path,
                "media_type": media_type,
                "enabled": True,
                "created_at": now,
            })
            return post_id
        return await asyncio.to_thread(_add)

    @staticmethod
    def _serialize_post(doc):
        if not doc:
            return None
        return {
            "id": int(doc["id"]),
            "kind": doc.get("kind"),
            "html_text": doc.get("html_text"),
            "media_path": doc.get("media_path"),
            "media_type": doc.get("media_type"),
            "enabled": 1 if doc.get("enabled", True) else 0,
            "created_at": doc.get("created_at"),
        }

    async def list_posts(self, enabled_only=False):
        def _list():
            q = {"owner_id": self.owner_id}
            if enabled_only:
                q["enabled"] = True
            return [self._serialize_post(d) for d in self.posts.find(q).sort("id", ASCENDING)]
        return await asyncio.to_thread(_list)

    async def get_post_by_id(self, post_id):
        def _get():
            return self._serialize_post(self.posts.find_one({"owner_id": self.owner_id, "id": int(post_id)}))
        return await asyncio.to_thread(_get)

    async def get_post(self):
        posts = await self.list_posts(enabled_only=True)
        return posts[0] if posts else None

    async def delete_post(self, post_id):
        def _delete():
            return self._serialize_post(self.posts.find_one_and_delete({"owner_id": self.owner_id, "id": int(post_id)}))
        return await asyncio.to_thread(_delete)

    async def toggle_post(self, post_id):
        def _toggle():
            q = {"owner_id": self.owner_id, "id": int(post_id)}
            doc = self.posts.find_one(q, {"enabled": 1})
            if not doc:
                return None
            new_enabled = not bool(doc.get("enabled", True))
            self.posts.update_one(q, {"$set": {"enabled": new_enabled, "updated_at": datetime.now(timezone.utc)}})
            return new_enabled
        return await asyncio.to_thread(_toggle)

    async def next_reply_post(self, mode="rotate"):
        posts = await self.list_posts(enabled_only=True)
        if not posts:
            return None
        if mode == "random":
            return random.choice(posts)

        def _next_cursor():
            doc = self.settings.find_one_and_update(
                {"owner_id": self.owner_id, "key": "reply_post_cursor"},
                {"$inc": {"cursor": 1},
                 "$setOnInsert": {"value": "0", "created_at": datetime.now(timezone.utc)}},
                upsert=True,
                return_document=ReturnDocument.BEFORE,
            )
            if not doc:
                cursor = 0
            elif "cursor" in doc:
                cursor = int(doc.get("cursor", 0))
            else:
                try:
                    cursor = int(doc.get("value", 0))
                except (TypeError, ValueError):
                    cursor = 0
                self.settings.update_one(
                    {"owner_id": self.owner_id, "key": "reply_post_cursor"},
                    {"$set": {"cursor": cursor + 1, "value": str(cursor + 1)}},
                )
                return cursor
            self.settings.update_one(
                {"owner_id": self.owner_id, "key": "reply_post_cursor"},
                {"$set": {"value": str(cursor + 1)}},
            )
            return cursor

        cursor = await asyncio.to_thread(_next_cursor)
        return posts[cursor % len(posts)]

    async def add_bot_account(self, bot_id: int, username: str | None, first_name: str | None, token_enc: str):
        now = datetime.now(timezone.utc)
        def _add():
            self.bot_accounts.update_one(
                {"owner_id": self.owner_id, "bot_id": int(bot_id)},
                {"$set": {
                    "username": username,
                    "first_name": first_name,
                    "token_enc": token_enc,
                    "enabled": True,
                    "updated_at": now,
                }, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            return self.bot_accounts.find_one(
                {"owner_id": self.owner_id, "bot_id": int(bot_id)}, {"_id": 0}
            )
        return await asyncio.to_thread(_add)

    async def list_bot_accounts(self, enabled_only: bool = False):
        def _list():
            q = {"owner_id": self.owner_id}
            if enabled_only:
                q["enabled"] = True
            docs = list(self.bot_accounts.find(q, {"_id": 0}).sort("created_at", ASCENDING))
            return docs
        return await asyncio.to_thread(_list)

    async def get_bot_account(self, bot_id: int):
        return await asyncio.to_thread(
            self.bot_accounts.find_one,
            {"owner_id": self.owner_id, "bot_id": int(bot_id)},
            {"_id": 0},
        )

    async def set_bot_account_enabled(self, bot_id: int, enabled: bool):
        result = await asyncio.to_thread(
            self.bot_accounts.update_one,
            {"owner_id": self.owner_id, "bot_id": int(bot_id)},
            {"$set": {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc)}},
        )
        return bool(result.matched_count)

    async def delete_bot_account(self, bot_id: int):
        result = await asyncio.to_thread(
            self.bot_accounts.delete_one,
            {"owner_id": self.owner_id, "bot_id": int(bot_id)},
        )
        await asyncio.to_thread(
            self.bot_reply_cooldowns.delete_many,
            {"owner_id": self.owner_id, "bot_id": int(bot_id)},
        )
        await asyncio.to_thread(self.bot_account_groups.delete_many, {"owner_id": self.owner_id, "bot_id": int(bot_id)})
        await asyncio.to_thread(self.bot_account_events.delete_many, {"owner_id": self.owner_id, "bot_id": int(bot_id)})
        return bool(result.deleted_count)

    async def upsert_bot_group(self, bot_id: int, chat_id: int, title: str | None = None, active: bool = True):
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            self.bot_account_groups.update_one,
            {"owner_id": self.owner_id, "bot_id": int(bot_id), "chat_id": int(chat_id)},
            {"$set": {"title": title, "active": bool(active), "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def count_bot_groups(self, bot_id: int) -> int:
        return await asyncio.to_thread(
            self.bot_account_groups.count_documents,
            {"owner_id": self.owner_id, "bot_id": int(bot_id), "active": True},
        )

    async def list_bot_groups(self, bot_id: int, limit: int = 50):
        def _list():
            return list(self.bot_account_groups.find(
                {"owner_id": self.owner_id, "bot_id": int(bot_id), "active": True}, {"_id": 0}
            ).sort("updated_at", -1).limit(max(1, int(limit))))
        return await asyncio.to_thread(_list)

    async def record_bot_event(self, bot_id: int, event: str, scope: str, chat_id: int | None = None,
                               user_id: int | None = None, detail: str = ""):
        now = datetime.now(timezone.utc)
        doc = {
            "owner_id": self.owner_id, "bot_id": int(bot_id), "event": str(event),
            "scope": str(scope), "chat_id": int(chat_id) if chat_id is not None else None,
            "user_id": int(user_id) if user_id is not None else None, "detail": str(detail)[:500],
            "created_at": now,
        }
        await asyncio.to_thread(self.bot_account_events.insert_one, doc)

    async def bot_event_stats(self, bot_id: int):
        def _stats():
            q={"owner_id": self.owner_id, "bot_id": int(bot_id)}
            out={"sent":0,"failed":0,"skipped":0,"group_sent":0,"dm_sent":0}
            for d in self.bot_account_events.aggregate([{"$match":q},{"$group":{"_id":{"event":"$event","scope":"$scope"},"n":{"$sum":1}}}]):
                ev=(d.get("_id") or {}).get("event")
                sc=(d.get("_id") or {}).get("scope")
                n=int(d.get("n",0))
                if ev in out: out[ev]+=n
                if ev=="sent":
                    if sc=="group": out["group_sent"]+=n
                    elif sc=="dm": out["dm_sent"]+=n
            return out
        return await asyncio.to_thread(_stats)

    async def list_bot_events(self, bot_id: int | None = None, limit: int = 30):
        def _list():
            q={"owner_id": self.owner_id}
            if bot_id is not None: q["bot_id"]=int(bot_id)
            return list(self.bot_account_events.find(q,{"_id":0}).sort("created_at",-1).limit(max(1,int(limit))))
        return await asyncio.to_thread(_list)

    async def bot_reply_allowed(self, bot_id: int, chat_id: int, user_id: int, cooldown_seconds: int) -> bool:
        """Atomically reserve one reply slot per bot + group + user."""
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        cutoff = now - timedelta(seconds=max(1, int(cooldown_seconds)))

        def _reserve():
            key = {
                "owner_id": self.owner_id,
                "bot_id": int(bot_id),
                "chat_id": int(chat_id),
                "user_id": int(user_id),
            }
            doc = self.bot_reply_cooldowns.find_one(key, {"updated_at": 1})
            if doc and doc.get("updated_at"):
                previous = doc["updated_at"]
                # PyMongo may return legacy BSON datetimes as offset-naive UTC.
                # Normalize before comparing with the timezone-aware cutoff.
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
                else:
                    previous = previous.astimezone(timezone.utc)
                if previous > cutoff:
                    return False
            self.bot_reply_cooldowns.update_one(
                key,
                {"$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            return True
        return await asyncio.to_thread(_reserve)

    async def release_bot_reply_slot(self, bot_id: int, chat_id: int, user_id: int):
        await asyncio.to_thread(
            self.bot_reply_cooldowns.delete_one,
            {
                "owner_id": self.owner_id,
                "bot_id": int(bot_id),
                "chat_id": int(chat_id),
                "user_id": int(user_id),
            },
        )


    async def enqueue_join_links(self, session_rows, links):
        """Queue the same normalized link list for every currently active session."""
        now = datetime.now(timezone.utc)
        def _add():
            added = 0
            existing = 0
            for row in session_rows:
                sid = str(row["id"])
                for link, normalized in links:
                    result = self.join_queue.update_one(
                        {"owner_id": self.owner_id, "session_id": sid, "normalized_link": normalized},
                        {"$setOnInsert": {
                            "owner_id": self.owner_id,
                            "session_id": sid,
                            "tg_user_id": row.get("tg_user_id"),
                            "label": row.get("first_name") or row.get("label") or str(row.get("tg_user_id") or sid),
                            "link": link,
                            "normalized_link": normalized,
                            "status": "pending",
                            "attempts": 0,
                            "created_at": now,
                            "updated_at": now,
                        }},
                        upsert=True,
                    )
                    if result.upserted_id is not None:
                        added += 1
                    else:
                        existing += 1
            return added, existing
        return await asyncio.to_thread(_add)

    async def next_pending_join(self, session_id: str):
        def _get():
            doc = self.join_queue.find_one(
                {"owner_id": self.owner_id, "session_id": str(session_id), "status": "pending"},
                sort=[("created_at", ASCENDING)],
            )
            if not doc:
                return None
            doc["id"] = str(doc.pop("_id"))
            return doc
        return await asyncio.to_thread(_get)

    async def finish_join_item(self, item_id: str, status: str, detail: str = ""):
        from bson import ObjectId
        now = datetime.now(timezone.utc)
        def _finish():
            return self.join_queue.update_one(
                {"_id": ObjectId(str(item_id)), "owner_id": self.owner_id},
                {"$set": {"status": str(status), "detail": str(detail)[:500], "updated_at": now},
                 "$inc": {"attempts": 1}},
            ).modified_count
        return await asyncio.to_thread(_finish)

    async def retry_join_item(self, item_id: str, detail: str = ""):
        from bson import ObjectId
        now = datetime.now(timezone.utc)
        def _retry():
            return self.join_queue.update_one(
                {"_id": ObjectId(str(item_id)), "owner_id": self.owner_id},
                {"$set": {"status": "pending", "detail": str(detail)[:500], "updated_at": now},
                 "$inc": {"attempts": 1}},
            ).modified_count
        return await asyncio.to_thread(_retry)

    async def join_queue_stats(self):
        def _stats():
            out = {"pending": 0, "joined": 0, "requested": 0, "already": 0, "failed": 0}
            pipeline = [
                {"$match": {"owner_id": self.owner_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
            for row in self.join_queue.aggregate(pipeline):
                key = str(row.get("_id") or "")
                if key in out:
                    out[key] = int(row.get("count", 0))
            return out
        return await asyncio.to_thread(_stats)

    async def list_recent_join_items(self, limit: int = 30):
        def _list():
            docs = list(self.join_queue.find(
                {"owner_id": self.owner_id}, {"_id": 0}
            ).sort("updated_at", -1).limit(max(1, int(limit))))
            return docs
        return await asyncio.to_thread(_list)

    async def clear_pending_join_items(self):
        return await asyncio.to_thread(
            self.join_queue.delete_many,
            {"owner_id": self.owner_id, "status": "pending"},
        )


class MongoSessionStore:
    """MongoDB-backed encrypted Telethon session storage.

    PyMongo itself is synchronous; tiny metadata operations are moved to a worker
    thread so the Telegram event loop stays responsive.
    """

    def __init__(self, uri: str, db_name: str = "promo_bot", owner_id: int | None = None, primary_owner: bool = False):
        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
        self.collection = self.client[db_name]["sessions"]
        self.owner_id = owner_id
        self.primary_owner = primary_owner

    def _owner_filter(self):
        if self.primary_owner:
            # Backward compatibility: pre-multiuser sessions have no owner_id.
            return {"$or": [{"owner_id": self.owner_id}, {"owner_id": {"$exists": False}}]}
        return {"owner_id": self.owner_id}

    async def init(self):
        def _init():
            self.client.admin.command("ping")
            self.collection.create_index([("tg_user_id", ASCENDING)], unique=True)
            self.collection.create_index([("enabled", ASCENDING)])
            self.collection.create_index([("owner_id", ASCENDING), ("enabled", ASCENDING)])

        await asyncio.to_thread(_init)

    async def add_session(self, label, session_enc, tg_user_id, username, first_name):
        now = datetime.now(timezone.utc)

        def _add():
            # One stored session per Telegram user account. Re-adding refreshes it.
            self.collection.update_one(
                dict(self._owner_filter(), tg_user_id=int(tg_user_id)) if not self.primary_owner else {"$and": [self._owner_filter(), {"tg_user_id": int(tg_user_id)}]},
                {
                    "$set": {
                        "label": label,
                        "session_enc": session_enc,
                        "tg_user_id": int(tg_user_id),
                        "owner_id": self.owner_id,
                        "username": username,
                        "first_name": first_name,
                        "enabled": True,
                        "auth_state": "active",
                        "auth_error": None,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            doc = self.collection.find_one({"$and": [self._owner_filter(), {"tg_user_id": int(tg_user_id)}]}, {"_id": 1})
            return str(doc["_id"])

        return await asyncio.to_thread(_add)

    @staticmethod
    def _serialize(doc):
        return {
            "id": str(doc["_id"]),
            "label": doc.get("label", "Account"),
            "session_enc": doc["session_enc"],
            "tg_user_id": doc.get("tg_user_id"),
            "username": doc.get("username"),
            "first_name": doc.get("first_name"),
            "enabled": bool(doc.get("enabled", True)),
            "auth_state": doc.get("auth_state", "active"),
            "auth_error": doc.get("auth_error"),
        }

    async def get_sessions(self, enabled_only=False):
        def _get():
            if enabled_only:
                query = {"$and": [
                    self._owner_filter(),
                    {"enabled": True},
                    {"auth_state": {"$ne": "needs_relogin"}},
                ]}
            else:
                query = self._owner_filter()
            docs = list(self.collection.find(query).sort("created_at", ASCENDING))
            return [self._serialize(d) for d in docs]

        return await asyncio.to_thread(_get)

    async def mark_needs_relogin(self, session_id: str, error: str = "AuthKeyDuplicatedError"):
        from bson import ObjectId
        from bson.errors import InvalidId

        def _mark():
            try:
                lookup = {"_id": ObjectId(session_id)}
            except (InvalidId, TypeError):
                if str(session_id).isdigit():
                    lookup = {"tg_user_id": int(session_id)}
                else:
                    return 0
            query = {"$and": [self._owner_filter(), lookup]}
            result = self.collection.update_one(
                query,
                {"$set": {
                    "auth_state": "needs_relogin",
                    "auth_error": str(error),
                    "auth_error_at": datetime.now(timezone.utc),
                }},
            )
            return result.modified_count

        return await asyncio.to_thread(_mark)

    async def delete_session(self, session_id: str):
        from bson import ObjectId
        from bson.errors import InvalidId

        def _delete():
            # Accept either Mongo ObjectId or Telegram numeric user ID.
            try:
                query = {"_id": ObjectId(session_id)}
            except (InvalidId, TypeError):
                if str(session_id).isdigit():
                    query = {"tg_user_id": int(session_id)}
                else:
                    return 0
            return self.collection.delete_one({"$and": [self._owner_filter(), query]}).deleted_count

        return await asyncio.to_thread(_delete)

    async def close(self):
        await asyncio.to_thread(self.client.close)
