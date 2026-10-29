#!/usr/bin/env python3
"""Export and import the promo bot database as one migration file.

Usage (run where the source and target databases are reachable):

  # 1. Stop the bot first so no data changes during export.
  python3 db_migrate.py export --src-uri "$MONGO_URI" --db promo_bot --out migration.jsonl.gz

  # 2. Copy migration.jsonl.gz to the VPS (scp / storage), then import into the new database.
  python3 db_migrate.py import --dst-uri "mongodb://user:pass@127.0.0.1:27017/?authSource=admin" \
      --db promo_bot --in migration.jsonl.gz

  # 3. Compare counts between the two databases.
  python3 db_migrate.py check --src-uri "$MONGO_URI" --dst-uri "$DST_URI" --db promo_bot

  # 4. Point MONGO_URI in .env to the new database and restart the bot.

The file holds every collection (sessions, settings, subscriptions, payments...) and
their indexes. It is gzipped JSON lines and is NOT encrypted, so keep it private.
"""
import argparse
import datetime as dt
import gzip
import os
import sys

from bson import json_util

try:  # same .env the bot uses, so MONGO_URI does not need to be exported in the shell
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FORMAT = "promo-bot-migration"
VERSION = 1
BATCH = 1000
INDEX_OPTIONS = ("unique", "sparse", "expireAfterSeconds", "partialFilterExpression", "collation")


def _index_spec(name, info):
    opts = {k: info[k] for k in INDEX_OPTIONS if k in info}
    return {"name": name, "key": [list(item) for item in info["key"]], "options": opts}


def export_db(client, db_name, out_path):
    """Write every non-system collection of db_name into out_path (gzipped JSON lines)."""
    db = client[db_name]
    names = sorted(n for n in db.list_collection_names() if not n.startswith("system."))
    meta = []
    for name in names:
        coll = db[name]
        indexes = [_index_spec(n, i) for n, i in coll.index_information().items() if n != "_id_"]
        meta.append({"name": name, "indexes": indexes})
    header = {"format": FORMAT, "version": VERSION, "db": db_name,
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "collections": meta}
    counts = {}
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        f.write(json_util.dumps(header) + "\n")
        for item in meta:
            n = 0
            for doc in db[item["name"]].find({}):
                f.write(json_util.dumps({"c": item["name"], "d": doc}) + "\n")
                n += 1
            counts[item["name"]] = n
        f.write(json_util.dumps({"end": True, "counts": counts}) + "\n")
    return counts


def _read_archive(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [json_util.loads(line) for line in f if line.strip()]
    if not lines or lines[0].get("format") != FORMAT:
        raise SystemExit("Not a promo bot migration file.")
    if lines[0].get("version") != VERSION:
        raise SystemExit(f"Unsupported migration version {lines[0].get('version')}.")
    if not lines[-1].get("end"):
        raise SystemExit("Migration file is incomplete (no end marker). Export again.")
    return lines[0], lines[1:-1], lines[-1]["counts"]


def import_db(client, db_name, in_path, drop=False):
    header, docs, expected = _read_archive(in_path)
    db = client[db_name]
    for item in header["collections"]:
        existing = db[item["name"]].count_documents({})
        if existing and not drop:
            raise SystemExit(f"Target collection '{item['name']}' already has {existing} documents. "
                             "Use --drop to replace it, or import into an empty database.")
    if drop:
        for item in header["collections"]:
            db[item["name"]].drop()

    buffers, inserted = {}, {}

    def flush(name):
        batch = buffers.pop(name, [])
        if batch:
            db[name].insert_many(batch, ordered=False)
            inserted[name] = inserted.get(name, 0) + len(batch)

    for line in docs:
        name = line["c"]
        buffers.setdefault(name, []).append(line["d"])
        if len(buffers[name]) >= BATCH:
            flush(name)
    for name in list(buffers):
        flush(name)

    for item in header["collections"]:
        for ix in item["indexes"]:
            opts = dict(ix["options"], name=ix["name"])
            db[item["name"]].create_index([tuple(k) for k in ix["key"]], **opts)

    problems = []
    for name, want in expected.items():
        got = db[name].count_documents({})
        if got != want:
            problems.append(f"{name}: expected {want}, found {got}")
    if problems:
        raise SystemExit("Import count mismatch:\n  " + "\n  ".join(problems))
    return expected


def to_sqlite(in_path, out_path):
    """Convert a migration file into one SQLite file: a table per collection.

    Each row holds the document's _id and its full JSON in one column, so no
    data is lost. Index definitions go into the _indexes table.
    """
    import json
    import sqlite3

    header, docs, expected = _read_archive(in_path)
    if os.path.exists(out_path):
        raise SystemExit(f"{out_path} already exists. Choose another name or remove it first.")
    con = sqlite3.connect(out_path)
    try:
        con.execute('CREATE TABLE "_indexes" (collection TEXT, name TEXT, keys TEXT, options TEXT)')
        for item in header["collections"]:
            con.execute(f'CREATE TABLE "{item["name"]}" (_id TEXT PRIMARY KEY, doc TEXT NOT NULL)')
            for ix in item["indexes"]:
                con.execute("INSERT INTO _indexes VALUES (?, ?, ?, ?)",
                            (item["name"], ix["name"], json.dumps(ix["key"]), json.dumps(ix["options"])))
        buffers = {}

        def flush(name):
            rows = buffers.pop(name, [])
            if rows:
                con.executemany(f'INSERT INTO "{name}" (_id, doc) VALUES (?, ?)', rows)

        for line in docs:
            name, doc = line["c"], line["d"]
            key = json_util.dumps(doc.get("_id"))
            buffers.setdefault(name, []).append((key, json_util.dumps(doc)))
            if len(buffers[name]) >= BATCH:
                flush(name)
        for name in list(buffers):
            flush(name)
        con.commit()
        problems = []
        for name, want in expected.items():
            got = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if got != want:
                problems.append(f"{name}: expected {want}, found {got}")
        if problems:
            raise SystemExit("SQLite count mismatch:\n  " + "\n  ".join(problems))
    finally:
        con.close()
    return expected


def check_dbs(src, dst, db_name):
    a, b = src[db_name], dst[db_name]
    names = sorted(set(a.list_collection_names()) | set(b.list_collection_names()))
    names = [n for n in names if not n.startswith("system.")]
    ok = True
    for n in names:
        sa, sb = a[n].count_documents({}), b[n].count_documents({})
        mark = "OK " if sa == sb else "DIFF"
        ok = ok and sa == sb
        print(f"{mark} {n}: source={sa} target={sb}")
    return ok


def _client(uri):
    from pymongo import MongoClient
    return MongoClient(uri, serverSelectionTimeoutMS=15000, tz_aware=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="export a database to a migration file")
    e.add_argument("--src-uri", default=os.getenv("MONGO_URI"))
    e.add_argument("--db", default=os.getenv("MONGO_DB", "promo_bot"))
    e.add_argument("--out", required=True)

    i = sub.add_parser("import", help="import a migration file into a database")
    i.add_argument("--dst-uri", default=os.getenv("DST_MONGO_URI"))
    i.add_argument("--db", default=os.getenv("MONGO_DB", "promo_bot"))
    i.add_argument("--in", dest="infile", required=True)
    i.add_argument("--drop", action="store_true", help="replace existing collections")

    c = sub.add_parser("check", help="compare document counts of two databases")
    c.add_argument("--src-uri", default=os.getenv("MONGO_URI"))
    c.add_argument("--dst-uri", default=os.getenv("DST_MONGO_URI"))
    c.add_argument("--db", default=os.getenv("MONGO_DB", "promo_bot"))

    t = sub.add_parser("to-sqlite", help="convert a migration file into a SQLite file")
    t.add_argument("--in", dest="infile", required=True)
    t.add_argument("--out", required=True)

    args = p.parse_args(argv)
    if args.cmd == "export":
        if not args.src_uri:
            raise SystemExit("Set --src-uri or MONGO_URI.")
        counts = export_db(_client(args.src_uri), args.db, args.out)
        total = sum(counts.values())
        print(f"Exported {total} documents in {len(counts)} collections to {args.out}")
        for n, c_ in counts.items():
            print(f"  {n}: {c_}")
        return 0
    if args.cmd == "import":
        if not args.dst_uri:
            raise SystemExit("Set --dst-uri or DST_MONGO_URI.")
        counts = import_db(_client(args.dst_uri), args.db, args.infile, drop=args.drop)
        print(f"Imported and verified {sum(counts.values())} documents in {len(counts)} collections.")
        return 0
    if args.cmd == "to-sqlite":
        counts = to_sqlite(args.infile, args.out)
        print(f"Wrote {sum(counts.values())} documents in {len(counts)} tables to {args.out}")
        return 0
    if args.cmd == "check":
        if not (args.src_uri and args.dst_uri):
            raise SystemExit("Set --src-uri and --dst-uri.")
        return 0 if check_dbs(_client(args.src_uri), _client(args.dst_uri), args.db) else 1


if __name__ == "__main__":
    sys.exit(main())
