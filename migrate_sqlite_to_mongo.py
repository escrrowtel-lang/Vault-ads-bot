#!/usr/bin/env python3
"""One-time migration of legacy promo.db files into MongoDB.

The running bot does not use SQLite. This helper uses Python's stdlib sqlite3 only
so existing settings/posts can be imported once before the old DB files are archived.
"""
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient

load_dotenv()
MONGO_URI = os.environ["MONGO_URI"].strip()
MONGO_DB = os.getenv("MONGO_DB", "promo_bot").strip()
ADMIN_ID = int(os.environ["ADMIN_ID"])
LEGACY_OWNER_DB = Path(os.getenv("DB_PATH", "promo.db"))
OPERATORS_DIR = Path(os.getenv("OPERATORS_DIR", "operators"))

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client[MONGO_DB]
settings_col = db["tenant_settings"]
posts_col = db["promo_posts"]
counters_col = db["tenant_counters"]

settings_col.create_index([("owner_id", ASCENDING), ("key", ASCENDING)], unique=True)
posts_col.create_index([("owner_id", ASCENDING), ("id", ASCENDING)], unique=True)


def table_exists(conn, name):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def migrate_file(path: Path, owner_id: int):
    if not path.exists():
        return {"settings": 0, "posts": 0, "skipped": True}
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    imported_settings = 0
    imported_posts = 0
    max_post_id = 0
    try:
        if table_exists(conn, "settings"):
            for row in conn.execute("SELECT key,value FROM settings"):
                key = str(row["key"])
                value = str(row["value"])
                doc = {"owner_id": owner_id, "key": key, "value": value,
                       "migrated_from_sqlite": True, "updated_at": now}
                if key == "reply_post_cursor":
                    try:
                        doc["cursor"] = int(value)
                    except ValueError:
                        pass
                settings_col.update_one(
                    {"owner_id": owner_id, "key": key},
                    {"$set": doc, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                )
                imported_settings += 1

        rows = []
        if table_exists(conn, "promo_posts"):
            rows = list(conn.execute(
                "SELECT id,kind,html_text,media_path,media_type,enabled,created_at FROM promo_posts ORDER BY id"
            ))
        elif table_exists(conn, "promo_post"):
            legacy = conn.execute(
                "SELECT id,kind,html_text,media_path,media_type,updated_at FROM promo_post WHERE id=1"
            ).fetchone()
            if legacy:
                rows = [dict(legacy, enabled=1, created_at=legacy["updated_at"])]

        for row in rows:
            row = dict(row)
            pid = int(row.get("id") or 0)
            if pid <= 0:
                continue
            max_post_id = max(max_post_id, pid)
            posts_col.update_one(
                {"owner_id": owner_id, "id": pid},
                {"$set": {
                    "owner_id": owner_id,
                    "id": pid,
                    "kind": row.get("kind"),
                    "html_text": row.get("html_text"),
                    "media_path": row.get("media_path"),
                    "media_type": row.get("media_type"),
                    "enabled": bool(row.get("enabled", 1)),
                    "migrated_from_sqlite": True,
                    "updated_at": now,
                 }, "$setOnInsert": {"created_at": row.get("created_at") or now}},
                upsert=True,
            )
            imported_posts += 1

        if max_post_id:
            counters_col.update_one(
                {"_id": f"post:{owner_id}"}, {"$max": {"seq": max_post_id}}, upsert=True
            )
    finally:
        conn.close()
    return {"settings": imported_settings, "posts": imported_posts, "skipped": False}


def main():
    client.admin.command("ping")
    jobs = [(LEGACY_OWNER_DB, ADMIN_ID)]
    if OPERATORS_DIR.exists():
        for child in sorted(OPERATORS_DIR.iterdir()):
            if child.is_dir() and child.name.isdigit():
                jobs.append((child / "promo.db", int(child.name)))

    found = False
    for path, owner_id in jobs:
        result = migrate_file(path, owner_id)
        if result["skipped"]:
            continue
        found = True
        print(f"owner={owner_id} file={path}: settings={result['settings']} posts={result['posts']}")
    if not found:
        print("No legacy SQLite promo.db files found. Nothing to migrate.")
    else:
        print("Migration complete. Start bot.py; it now uses MongoDB only for persistent settings/posts/sessions/access.")
        print("Keep the old promo.db files as a backup until you verify your posts/settings, then archive or delete them.")


if __name__ == "__main__":
    main()
