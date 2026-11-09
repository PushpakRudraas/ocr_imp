"""SQLite persistence: stage outputs (so a run can resume or be inspected) and daily counters for burst/novelty.
Swap for MongoDB by implementing the same few methods; the pipeline only depends on this interface."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def doc_meta(doc: dict) -> tuple[str | None, str | None]:
    """(date, paper_id) of any stage's document: raw/clean docs carry them at top level, enriched docs nest them."""
    date = doc.get("date") or str((doc.get("ingestion_metadata") or {}).get("published_at") or "")[:10] or None
    paper = doc.get("paper_id") or (doc.get("newspaper") or {}).get("paper_id")
    return date, paper


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS docs(stage TEXT, key TEXT, date TEXT, paper_id TEXT, body TEXT,
                                            PRIMARY KEY(stage, key));
            CREATE TABLE IF NOT EXISTS counters(key TEXT, day TEXT, n INTEGER, PRIMARY KEY(key, day));
        """)

    # ----------------------------------------------------------------------------------------- documents
    def put(self, stage: str, key: str, doc: dict) -> None:
        date, paper = doc_meta(doc)
        self.db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)",
                        (stage, key, date, paper, json.dumps(doc, ensure_ascii=False, default=str)))
        self.db.commit()

    def put_many(self, stage: str, docs: list[dict], key_field: str = "event_id") -> None:
        for d in docs:
            self.put(stage, d[key_field], d)

    def get(self, stage: str, key: str) -> dict | None:
        row = self.db.execute("SELECT body FROM docs WHERE stage=? AND key=?", (stage, key)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, stage: str, date: str | None = None) -> list[dict]:
        q, args = "SELECT body FROM docs WHERE stage=?", [stage]
        if date:
            q += " AND date=?"; args.append(date)
        return [json.loads(r[0]) for r in self.db.execute(q + " ORDER BY key", args)]

    def count(self, stage: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM docs WHERE stage=?", (stage,)).fetchone()[0]

    # ------------------------------------------------------------------------------------------ counters
    def increment(self, key: str, day: str, n: int = 1) -> None:
        self.db.execute("INSERT INTO counters VALUES (?,?,?) ON CONFLICT(key, day) DO UPDATE SET n = n + ?",
                        (key, day, n, n))
        self.db.commit()

    def counter(self, key: str, day: str) -> int:
        row = self.db.execute("SELECT n FROM counters WHERE key=? AND day=?", (key, day)).fetchone()
        return row[0] if row else 0

    def history(self, key: str, before_day: str, days: int = 14) -> list[int]:
        rows = self.db.execute("SELECT n FROM counters WHERE key=? AND day<? ORDER BY day DESC LIMIT ?",
                               (key, before_day, days)).fetchall()
        return [r[0] for r in rows]

    def days_of_history(self, before_day: str) -> int:
        return self.db.execute("SELECT COUNT(DISTINCT day) FROM counters WHERE day<?", (before_day,)).fetchone()[0]

    def close(self) -> None:
        self.db.close()


# ================================================================================================ MongoDB
_COLLECTION = {"pages": "pages", "ocr": "raw_events", "clean": "clean_events", "enriched": "enriched_events"}


class MongoStore:
    """Same interface as `Store`, backed by MongoDB.

    Stages map to collections: pages -> `pages`, ocr -> `raw_events` (the raw article events straight out of OCR),
    clean -> `clean_events`, enriched -> `enriched_events`. Documents are stored natively (queryable), `_id` is the key,
    and daily counters live in `counters`. Writes are upserts, so re-running a stage never duplicates."""

    def __init__(self, uri: str = "mongodb://localhost:27017", db: str = "newspaper", client=None) -> None:
        from pymongo import ASCENDING, MongoClient
        self._owned = client is None
        self._client = client or MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        self.db = self._client[db]
        for name in set(_COLLECTION.values()):
            self.db[name].create_index([("_date", ASCENDING)])
            self.db[name].create_index([("_paper_id", ASCENDING), ("_date", ASCENDING)])
        self.db["clean_events"].create_index("story_id")
        self.db["enriched_events"].create_index("story.story_id")
        self.db["counters"].create_index([("key", ASCENDING), ("day", ASCENDING)], unique=True)

    def _col(self, stage: str):
        return self.db[_COLLECTION.get(stage, f"{stage}_events")]

    # ----------------------------------------------------------------------------------------- documents
    @staticmethod
    def _wrap(key: str, doc: dict) -> dict:
        date, paper = doc_meta(doc)
        return {**{k: v for k, v in doc.items() if k != "_id"}, "_id": key, "_date": date, "_paper_id": paper}

    def put(self, stage: str, key: str, doc: dict) -> None:
        self._col(stage).replace_one({"_id": key}, self._wrap(key, doc), upsert=True)

    def put_many(self, stage: str, docs: list[dict], key_field: str = "event_id") -> None:
        from pymongo import ReplaceOne
        if not docs:
            return
        ops = [ReplaceOne({"_id": d[key_field]}, self._wrap(d[key_field], d), upsert=True) for d in docs]
        self._col(stage).bulk_write(ops, ordered=False)

    def get(self, stage: str, key: str) -> dict | None:
        doc = self._col(stage).find_one({"_id": key})
        return self._clean(doc)

    def all(self, stage: str, date: str | None = None) -> list[dict]:
        q = {"_date": date} if date else {}
        return [self._clean(d) for d in self._col(stage).find(q).sort("_id", 1)]

    def count(self, stage: str) -> int:
        return self._col(stage).count_documents({})

    @staticmethod
    def _clean(doc: dict | None) -> dict | None:
        if doc is None:
            return None
        doc = dict(doc)
        for k in ("_id", "_date", "_paper_id"):
            doc.pop(k, None)
        return doc

    # ------------------------------------------------------------------------------------------ counters
    def increment(self, key: str, day: str, n: int = 1) -> None:
        self.db["counters"].update_one({"key": key, "day": day}, {"$inc": {"n": n}}, upsert=True)

    def counter(self, key: str, day: str) -> int:
        row = self.db["counters"].find_one({"key": key, "day": day})
        return row["n"] if row else 0

    def history(self, key: str, before_day: str, days: int = 14) -> list[int]:
        rows = self.db["counters"].find({"key": key, "day": {"$lt": before_day}}).sort("day", -1).limit(days)
        return [r["n"] for r in rows]

    def days_of_history(self, before_day: str) -> int:
        return len(self.db["counters"].distinct("day", {"day": {"$lt": before_day}}))

    def close(self) -> None:
        if self._owned:                      # a client shared with other stores is closed by whoever created it
            self._client.close()


def make_store(settings, shared: dict | None = None):
    """Store chosen by `settings.store_backend`. `shared` lets a long-lived server reuse one Mongo client."""
    if settings.store_backend == "mongo":
        from pymongo import MongoClient
        shared = shared if shared is not None else {}
        client = shared.get("mongo_client")
        if client is None:
            client = shared["mongo_client"] = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        return MongoStore(db=settings.mongo_db, client=client)
    return Store(settings.db_path)
