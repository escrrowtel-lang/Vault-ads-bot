"""SQLite storage with MongoDB as a live backup.

The bot reads and writes its local SQLite file (SQLITE_PATH). The same API subset as
pymongo is provided, so the rest of the code keeps working.

Backup: every write queues the full document in the _outbox table. A background thread
copies queued documents to MongoDB with upserts by _id, so a document is never duplicated.
Repeated writes to one document are coalesced, and nothing is lost while MongoDB is down.

Startup sync (runs in the background thread): every collection is compared with MongoDB.
  - in MongoDB only  -> copied into SQLite
  - in SQLite only   -> queued for MongoDB
  - in both, differs -> SQLite version wins and is queued for MongoDB
"""
import copy
import datetime as dt
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

from bson import json_util

try:
    from bson import ObjectId
except ImportError:  # pragma: no cover
    ObjectId = None

log = logging.getLogger("storage")

ASCENDING, DESCENDING = 1, -1
UTC = dt.timezone.utc
MISSING = object()
BATCH = 500
FLUSH_SECONDS = 2
REMOTE_RETRY_SECONDS = 30
_JSON = json_util.JSONOptions(tz_aware=True, tzinfo=UTC)


class ReturnDocument:
    BEFORE = False
    AFTER = True


class DuplicateKeyError(Exception):
    pass


class IndexModel:
    def __init__(self, keys, **kwargs):
        self.keys = list(keys)
        self.options = kwargs


class UpdateResult:
    def __init__(self, matched, modified, upserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id


class InsertOneResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class DeleteResult:
    def __init__(self, deleted):
        self.deleted_count = deleted


def _new_id():
    return ObjectId() if ObjectId is not None else uuid.uuid4().hex


def _enc(doc):
    return json_util.dumps(doc, json_options=_JSON)


def _dec(text):
    return json_util.loads(text, json_options=_JSON)


def _key_of(_id):
    return json_util.dumps(_id, json_options=_JSON)


def _utc(v):
    if isinstance(v, dt.datetime) and v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def _setp(doc, path, value):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _unsetp(doc, path):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _eq(v, c):
    if v is MISSING:
        return c is None
    if isinstance(v, list) and not isinstance(c, list):
        return any(_eq(x, c) for x in v)
    if isinstance(v, dt.datetime) or isinstance(c, dt.datetime):
        if isinstance(v, dt.datetime) and isinstance(c, dt.datetime):
            return _utc(v) == _utc(c)
        return False
    return v == c


def _cmp(v, c, op):
    if v is MISSING or v is None or c is None:
        return False
    a, b = _utc(v), _utc(c)
    try:
        return op(a, b)
    except TypeError:
        return False


def _type_ok(v, name):
    if v is MISSING:
        return False
    checks = {
        "string": lambda x: isinstance(x, str),
        "int": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "long": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "double": lambda x: isinstance(x, float),
        "bool": lambda x: isinstance(x, bool),
        "date": lambda x: isinstance(x, dt.datetime),
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "null": lambda x: x is None,
    }
    fn = checks.get(name)
    if fn is None:
        raise ValueError(f"unsupported $type {name!r}")
    return fn(v)


def _op(v, op, arg):
    if op == "$eq":
        return _eq(v, arg)
    if op == "$ne":
        return not _eq(v, arg)
    if op == "$gt":
        return _cmp(v, arg, lambda a, b: a > b)
    if op == "$gte":
        return _cmp(v, arg, lambda a, b: a >= b)
    if op == "$lt":
        return _cmp(v, arg, lambda a, b: a < b)
    if op == "$lte":
        return _cmp(v, arg, lambda a, b: a <= b)
    if op == "$in":
        return any(_eq(v, x) for x in arg)
    if op == "$nin":
        return not any(_eq(v, x) for x in arg)
    if op == "$exists":
        return (v is not MISSING) == bool(arg)
    if op == "$type":
        return _type_ok(v, arg)
    raise ValueError(f"unsupported query operator {op}")


def match(doc, flt):
    for key, cond in (flt or {}).items():
        if key == "$and":
            if not all(match(doc, x) for x in cond):
                return False
            continue
        if key == "$or":
            if not any(match(doc, x) for x in cond):
                return False
            continue
        if key == "$nor":
            if any(match(doc, x) for x in cond):
                return False
            continue
        if key.startswith("$"):
            raise ValueError(f"unsupported top-level operator {key}")
        value = _get(doc, key)
        if isinstance(cond, dict) and cond and all(isinstance(k, str) and k.startswith("$") for k in cond):
            for op, arg in cond.items():
                if not _op(value, op, arg):
                    return False
        elif not _eq(value, cond):
            return False
    return True


def _apply_update(doc, upd, is_insert):
    if not any(str(k).startswith("$") for k in upd):
        new = dict(upd)
        new["_id"] = doc.get("_id")
        return new
    new = copy.deepcopy(doc)
    for op, fields in upd.items():
        for path, val in fields.items():
            if op == "$set":
                _setp(new, path, val)
            elif op == "$setOnInsert":
                if is_insert:
                    _setp(new, path, val)
            elif op == "$unset":
                _unsetp(new, path)
            elif op == "$inc":
                cur = _get(new, path)
                base = 0 if cur is MISSING or cur is None else cur
                _setp(new, path, base + val)
            elif op in ("$max", "$min"):
                cur = _get(new, path)
                if cur is MISSING or cur is None:
                    _setp(new, path, val)
                else:
                    better = _cmp(val, cur, (lambda a, b: a > b) if op == "$max" else (lambda a, b: a < b))
                    if better:
                        _setp(new, path, val)
            elif op == "$addToSet":
                cur = _get(new, path)
                arr = list(cur) if isinstance(cur, list) else []
                if not any(_eq(x, val) for x in arr):
                    arr.append(val)
                _setp(new, path, arr)
            elif op == "$push":
                cur = _get(new, path)
                arr = list(cur) if isinstance(cur, list) else []
                arr.append(val)
                _setp(new, path, arr)
            else:
                raise ValueError(f"unsupported update operator {op}")
    return new


def _project(doc, projection):
    if not projection:
        return copy.deepcopy(doc)
    keys = {k: v for k, v in projection.items() if k != "_id"}
    inclusive = any(bool(v) for v in keys.values())
    if inclusive:
        out = {}
        if projection.get("_id", 1):
            if "_id" in doc:
                out["_id"] = doc["_id"]
        for k, v in keys.items():
            if v and k in doc:
                out[k] = copy.deepcopy(doc[k])
        return out
    out = copy.deepcopy(doc)
    for k, v in keys.items():
        if not v:
            out.pop(k, None)
    if not projection.get("_id", 1):
        out.pop("_id", None)
    return out


def _sort_key(v):
    if v is MISSING or v is None:
        return (0, 0)
    v = _utc(v)
    if isinstance(v, dt.datetime):
        return (1, v.timestamp())
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (1, v)
    return (2, str(v))


def _sort_docs(docs, spec):
    """spec: list of (key, direction). Stable sort applied from the last key."""
    for key, direction in reversed(spec):
        docs.sort(key=lambda d: _sort_key(_get(d, key)), reverse=(direction == DESCENDING))
    return docs


def _normalize_sort(sort, direction=None):
    if sort is None:
        return []
    if isinstance(sort, str):
        return [(sort, direction or ASCENDING)]
    return [(k, d) for k, d in sort]


def _norm(v):
    if isinstance(v, dt.datetime):
        v = _utc(v)
        return v.replace(microsecond=(v.microsecond // 1000) * 1000)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


class _Engine:
    """One SQLite file shared by all collections in this process."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute('CREATE TABLE IF NOT EXISTS "_indexes" '
                         "(collection TEXT, name TEXT, keys TEXT, options TEXT, PRIMARY KEY (collection, name))")
        self.con.execute('CREATE TABLE IF NOT EXISTS "_outbox" '
                         "(collection TEXT, k TEXT, op TEXT, doc TEXT, PRIMARY KEY (collection, k))")
        self.tables = set(r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))

    def table(self, name):
        with self.lock:
            if name not in self.tables:
                self.con.execute(f'CREATE TABLE IF NOT EXISTS "{name}" '
                                 "(_id TEXT PRIMARY KEY, doc TEXT NOT NULL)")
                self.tables.add(name)

    def all_docs(self, name):
        with self.lock:
            self.table(name)
            rows = self.con.execute(f'SELECT _id, doc FROM "{name}" ORDER BY rowid').fetchall()
        return [(k, _dec(doc)) for k, doc in rows]

    def put(self, name, k, doc, insert_only=False):
        with self.lock:
            self.table(name)
            verb = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
            self.con.execute(f'{verb} INTO "{name}" (_id, doc) VALUES (?, ?)', (k, _enc(doc)))

    def get(self, name, k):
        with self.lock:
            self.table(name)
            row = self.con.execute(f'SELECT doc FROM "{name}" WHERE _id = ?', (k,)).fetchone()
        return _dec(row[0]) if row else None

    def delete(self, name, k):
        with self.lock:
            self.table(name)
            cur = self.con.execute(f'DELETE FROM "{name}" WHERE _id = ?', (k,))
            return cur.rowcount

    def collection_names(self):
        with self.lock:
            return sorted(n for n in self.tables if not n.startswith("_"))

    def index_rows(self, name):
        with self.lock:
            rows = self.con.execute("SELECT name, keys, options FROM _indexes WHERE collection = ?",
                                    (name,)).fetchall()
        return [{"name": n, "key": json.loads(k), "options": json.loads(o)} for n, k, o in rows]

    def save_index(self, name, index_name, keys, options):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO _indexes VALUES (?, ?, ?, ?)",
                             (name, index_name, json.dumps(keys), json.dumps(options, default=str)))

    def outbox_put(self, name, k, op, doc_text):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO _outbox VALUES (?, ?, ?, ?)", (name, k, op, doc_text))

    def outbox_rows(self, limit):
        with self.lock:
            return self.con.execute("SELECT collection, k, op, doc FROM _outbox ORDER BY rowid LIMIT ?",
                                    (limit,)).fetchall()

    def outbox_done(self, name, k, op, doc_text):
        with self.lock:
            self.con.execute("DELETE FROM _outbox WHERE collection = ? AND k = ? AND op = ? AND doc = ?",
                             (name, k, op, doc_text))

    def outbox_count(self):
        with self.lock:
            return self.con.execute("SELECT COUNT(*) FROM _outbox").fetchone()[0]


class Backup:
    """Copies queued documents to MongoDB in the background. Writes never wait for it."""

    def __init__(self, engine, uri, db_name):
        self.engine = engine
        self.uri = uri
        self.db_name = db_name
        self.client = None
        self.down_until = 0.0
        self.warned = False
        self.synced = False
        thread = threading.Thread(target=self._loop, name="storage-backup", daemon=True)
        thread.start()

    def _remote_db(self):
        if time.time() < self.down_until:
            return None
        try:
            if self.client is None:
                from pymongo import MongoClient
                self.client = MongoClient(self.uri, serverSelectionTimeoutMS=3000, tz_aware=True)
            self.client.admin.command("ping")
            if self.warned:
                log.info("MongoDB backup is reachable again")
                self.warned = False
            return self.client[self.db_name]
        except Exception as exc:
            self.down_until = time.time() + REMOTE_RETRY_SECONDS
            if not self.warned:
                log.warning("MongoDB backup unavailable (%s); queued writes will be sent later", type(exc).__name__)
                self.warned = True
            return None

    def flush_once(self):
        remote = self._remote_db()
        if remote is None:
            return 0
        rows = self.engine.outbox_rows(BATCH)
        if not rows:
            return 0
        by_coll = {}
        for name, k, op, doc_text in rows:
            by_coll.setdefault(name, []).append((k, op, doc_text))
        sent = 0
        try:
            for name, items in by_coll.items():
                ops = []
                from pymongo import DeleteOne, ReplaceOne
                for k, op, doc_text in items:
                    doc = _dec(doc_text)
                    if op == "upsert":
                        ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
                    else:
                        ops.append(DeleteOne({"_id": doc["_id"]}))
                remote[name].bulk_write(ops, ordered=False)
                for k, op, doc_text in items:
                    self.engine.outbox_done(name, k, op, doc_text)
                    sent += 1
        except Exception as exc:
            self.down_until = time.time() + REMOTE_RETRY_SECONDS
            log.warning("MongoDB backup write failed (%s); will retry", type(exc).__name__)
        return sent

    def sync_once(self):
        """Compare SQLite with MongoDB and make both sides hold the union (SQLite wins conflicts)."""
        remote = self._remote_db()
        if remote is None:
            return None
        report = {}
        names = set(self.engine.collection_names())
        names |= {n for n in remote.list_collection_names() if not n.startswith("system.")}
        for name in sorted(names):
            local = {k: _norm(d) for k, d in self.engine.all_docs(name)}
            remote_docs = {_key_of(d["_id"]): _norm(d) for d in remote[name].find({})}
            copied_in = copied_out = 0
            for k, doc in remote_docs.items():
                if k not in local:
                    self.engine.put(name, k, doc, insert_only=True)
                    copied_in += 1
            for k, doc in local.items():
                if k not in remote_docs:
                    self.engine.outbox_put(name, k, "upsert", _enc(doc))
                    copied_out += 1
                elif remote_docs[k] != doc:
                    self.engine.outbox_put(name, k, "upsert", _enc(doc))
                    copied_out += 1
            report[name] = {"local": len(local), "remote": len(remote_docs),
                            "copied_in": copied_in, "queued_out": copied_out}
        return report

    def _loop(self):
        while True:
            try:
                if not self.synced:
                    report = self.sync_once()
                    if report is not None:
                        self.synced = True
                        for name, r in report.items():
                            log.info("storage sync %s: local=%s mongo=%s copied_in=%s queued_out=%s",
                                     name, r["local"], r["remote"], r["copied_in"], r["queued_out"])
                        log.info("storage sync finished (%d collections)", len(report))
                if self.engine.outbox_count():
                    self.flush_once()
            except Exception as exc:
                log.warning("storage backup loop error: %s", type(exc).__name__)
            time.sleep(FLUSH_SECONDS)


_ENGINE = None
_BACKUP = None
_LOCK = threading.RLock()


def _get_engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            path = os.getenv("SQLITE_PATH", "promo_bot.sqlite")
            _ENGINE = _Engine(path)
            log.info("SQLite storage: %s", path)
        return _ENGINE


def _get_backup(uri, db_name):
    global _BACKUP
    if os.getenv("BACKUP_MONGO", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    if not uri:
        return None
    with _LOCK:
        if _BACKUP is None:
            _BACKUP = Backup(_get_engine(), uri, db_name)
        return _BACKUP


class Collection:
    def __init__(self, db, name):
        self._db = db
        self.name = name
        self._engine = _get_engine()
        self._engine.table(name)

    def _docs(self):
        return self._engine.all_docs(self.name)

    def _backup(self):
        return self._db._backup

    def _write(self, k, doc):
        self._engine.put(self.name, k, doc)
        b = self._backup()
        if b is not None:
            self._engine.outbox_put(self.name, k, "upsert", _enc(doc))

    def _remove(self, k, doc):
        self._engine.delete(self.name, k)
        b = self._backup()
        if b is not None:
            self._engine.outbox_put(self.name, k, "delete", _enc({"_id": doc["_id"]}))

    def _check_unique(self, doc, exclude_k=None):
        for ix in self._engine.index_rows(self.name):
            if not ix["options"].get("unique"):
                continue
            partial = ix["options"].get("partialFilterExpression")
            if partial and not match(doc, partial):
                continue
            fields = [k for k, _ in ix["key"]]
            want = [_enc({"v": _get(doc, f) if _get(doc, f) is not MISSING else None}) for f in fields]
            for k, other in self._docs():
                if k == exclude_k:
                    continue
                if partial and not match(other, partial):
                    continue
                have = [_enc({"v": _get(other, f) if _get(other, f) is not MISSING else None}) for f in fields]
                if have == want:
                    raise DuplicateKeyError(f"duplicate key on {self.name} index {ix['name']}")

    def _find_first(self, flt, sort=None):
        """All (key, doc) pairs matching flt, in sort order (natural order if no sort)."""
        docs = [(k, d) for k, d in self._docs() if match(d, flt or {})]
        for key, direction in reversed(_normalize_sort(sort)):
            docs.sort(key=lambda kd, key=key: _sort_key(_get(kd[1], key)),
                      reverse=(direction == DESCENDING))
        return docs

    # --- reads ---
    def find(self, flt=None, projection=None):
        return Cursor(self, flt or {}, projection)

    def find_one(self, flt=None, projection=None, sort=None):
        docs = self._find_first(flt, sort)
        if not docs:
            return None
        return _project(docs[0][1], projection)

    def count_documents(self, flt=None):
        return sum(1 for _, d in self._docs() if match(d, flt or {}))

    def estimated_document_count(self):
        return len(self._docs())

    def aggregate(self, pipeline):
        docs = [d for _, d in self._docs()]
        for stage in pipeline:
            (name, arg), = stage.items()
            if name == "$match":
                docs = [d for d in docs if match(d, arg)]
            elif name == "$sort":
                docs = _sort_docs(docs, _normalize_sort(list(arg.items())))
            elif name == "$group":
                docs = self._group(docs, arg)
            else:
                raise ValueError(f"unsupported aggregation stage {name}")
        return docs

    def _group(self, docs, spec):
        id_expr = spec["_id"]
        buckets = {}
        for d in docs:
            if isinstance(id_expr, dict):
                key_doc = {k: self._expr(d, v) for k, v in id_expr.items()}
            else:
                key_doc = self._expr(d, id_expr)
            key = _enc({"k": key_doc})
            buckets.setdefault(key, {"_id": key_doc, "_rows": []})["_rows"].append(d)
        out = []
        for bucket in buckets.values():
            row = {"_id": bucket["_id"]}
            for field, acc in spec.items():
                if field == "_id":
                    continue
                (op, expr), = acc.items()
                values = [self._expr(d, expr) for d in bucket["_rows"]]
                if op == "$sum":
                    row[field] = sum(v for v in values if isinstance(v, (int, float)))
                elif op == "$max":
                    row[field] = max(values)
                elif op == "$min":
                    row[field] = min(values)
                else:
                    raise ValueError(f"unsupported accumulator {op}")
            out.append(row)
        return out

    @staticmethod
    def _expr(doc, expr):
        if isinstance(expr, str) and expr.startswith("$"):
            v = _get(doc, expr[1:])
            return None if v is MISSING else v
        return expr

    # --- writes ---
    def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = _new_id()
        k = _key_of(doc["_id"])
        if self._engine.get(self.name, k) is not None:
            raise DuplicateKeyError(f"duplicate _id in {self.name}")
        self._check_unique(doc)
        self._write(k, doc)
        return InsertOneResult(doc["_id"])

    def _upsert_base(self, flt):
        base = {}
        for key, cond in (flt or {}).items():
            if key == "$and":
                for part in cond:
                    base.update(self._upsert_base(part))
            elif not key.startswith("$"):
                if isinstance(cond, dict) and cond and all(str(c).startswith("$") for c in cond):
                    continue
                _setp(base, key, cond)
        return base

    def _upsert_doc(self, flt, upd, replace):
        if replace:
            new = dict(upd)
        else:
            new = _apply_update(self._upsert_base(flt), upd, is_insert=True)
        if "_id" not in new:
            new["_id"] = _new_id()
        k = _key_of(new["_id"])
        self._check_unique(new)
        self._write(k, new)
        return new

    def _modify(self, k, old, new):
        self._check_unique(new, exclude_k=k)
        self._write(k, new)
        return new

    def update_one(self, flt, upd, upsert=False):
        found = self._find_first(flt)
        if found:
            k, old = found[0]
            new = self._modify(k, old, _apply_update(old, upd, is_insert=False))
            return UpdateResult(1, 1 if new != old else 0)
        if upsert:
            new = self._upsert_doc(flt, upd, replace=False)
            return UpdateResult(0, 0, upserted_id=new["_id"])
        return UpdateResult(0, 0)

    def update_many(self, flt, upd, upsert=False):
        matched = modified = 0
        for k, old in self._find_first(flt):
            new = self._modify(k, old, _apply_update(old, upd, is_insert=False))
            matched += 1
            modified += 1 if new != old else 0
        return UpdateResult(matched, modified)

    def replace_one(self, flt, doc, upsert=False):
        found = self._find_first(flt)
        if found:
            k, old = found[0]
            new = dict(doc)
            new["_id"] = old["_id"]
            self._modify(k, old, new)
            return UpdateResult(1, 1 if new != old else 0)
        if upsert:
            new = self._upsert_doc(flt, doc, replace=True)
            return UpdateResult(0, 0, upserted_id=new["_id"])
        return UpdateResult(0, 0)

    def find_one_and_update(self, flt, upd, upsert=False, return_document=ReturnDocument.BEFORE, sort=None):
        found = self._find_first(flt, sort)
        if found:
            k, old = found[0]
            new = self._modify(k, old, _apply_update(old, upd, is_insert=False))
            return new if return_document == ReturnDocument.AFTER else copy.deepcopy(old)
        if upsert:
            new = self._upsert_doc(flt, upd, replace=False)
            return new if return_document == ReturnDocument.AFTER else None
        return None

    def find_one_and_delete(self, flt, sort=None):
        found = self._find_first(flt, sort)
        if not found:
            return None
        k, doc = found[0]
        self._remove(k, doc)
        return doc

    def delete_one(self, flt):
        found = self._find_first(flt)
        if not found:
            return DeleteResult(0)
        k, doc = found[0]
        self._remove(k, doc)
        return DeleteResult(1)

    def delete_many(self, flt):
        found = self._find_first(flt)
        for k, doc in found:
            self._remove(k, doc)
        return DeleteResult(len(found))

    # --- indexes ---
    def create_index(self, keys, unique=False, **kwargs):
        if isinstance(keys, str):
            keys = [(keys, ASCENDING)]
        keys = [(k, d) for k, d in keys]
        index_name = kwargs.pop("name", None) or "_".join(f"{k}_{d}" for k, d in keys)
        options = dict(kwargs)
        if unique:
            options["unique"] = True
        self._engine.save_index(self.name, index_name, [list(x) for x in keys], options)
        if options.get("expireAfterSeconds"):
            self._expire(keys[0][0], int(options["expireAfterSeconds"]))
        return index_name

    def create_indexes(self, models):
        return [self.create_index(m.keys, **m.options) for m in models]

    def index_information(self):
        return {ix["name"]: {"key": [tuple(k) for k in ix["key"]], **ix["options"]}
                for ix in self._engine.index_rows(self.name)}

    def _expire(self, field, seconds):
        cutoff = dt.datetime.now(UTC) - dt.timedelta(seconds=seconds)
        for k, doc in self._docs():
            value = _get(doc, field)
            if isinstance(value, dt.datetime) and _utc(value) < cutoff:
                self._remove(k, doc)


class Cursor:
    def __init__(self, coll, flt, projection):
        self._coll = coll
        self._flt = flt
        self._projection = projection
        self._sort = []
        self._skip = 0
        self._limit = 0

    def sort(self, key, direction=None):
        self._sort = _normalize_sort(key, direction)
        return self

    def skip(self, n):
        self._skip = int(n)
        return self

    def limit(self, n):
        self._limit = int(n)
        return self

    def _rows(self):
        docs = [d for _, d in self._coll._find_first(self._flt, self._sort or None)]
        if self._skip:
            docs = docs[self._skip:]
        if self._limit:
            docs = docs[: self._limit]
        return [_project(d, self._projection) for d in docs]

    def __iter__(self):
        return iter(self._rows())

    def to_list(self, length=None):
        rows = self._rows()
        return rows[:length] if length else rows


class _Admin:
    def command(self, name, *args, **kwargs):
        return {"ok": 1.0}


class _Database:
    def __init__(self, name, backup):
        self.name = name
        self._backup = backup
        self._collections = {}

    def __getitem__(self, name):
        if name not in self._collections:
            self._collections[name] = Collection(self, name)
        return self._collections[name]

    def list_collection_names(self):
        return self._engine_names()

    def _engine_names(self):
        return _get_engine().collection_names()


class DBClient:
    """Drop-in for pymongo.MongoClient, backed by SQLite with MongoDB backup."""

    def __init__(self, uri=None, **kwargs):
        self._uri = uri
        self.admin = _Admin()
        self._dbs = {}

    def __getitem__(self, db_name):
        if db_name not in self._dbs:
            backup = _get_backup(self._uri, db_name)
            self._dbs[db_name] = _Database(db_name, backup)
        return self._dbs[db_name]

    def close(self):
        return None
