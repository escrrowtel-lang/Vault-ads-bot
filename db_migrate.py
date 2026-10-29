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
    if args.cmd == "check":
        if not (args.src_uri and args.dst_uri):
            raise SystemExit("Set --src-uri and --dst-uri.")
        return 0 if check_dbs(_client(args.src_uri), _client(args.dst_uri), args.db) else 1


if __name__ == "__main__":
    sys.exit(main())
