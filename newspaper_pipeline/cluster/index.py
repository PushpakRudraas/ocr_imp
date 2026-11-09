"""Vector index with payload filtering. `InMemoryIndex` (numpy) is the default; `QdrantIndex` uses qdrant-client's
local mode (no server) and satisfies the same interface."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Callable, Protocol

import numpy as np

Filter = Callable[[dict], bool]


class DateWindow:
    """Payload filter: article date within +-`days` of `center`. A class (not a lambda) so SQL-backed indexes can push
    it down into the query instead of filtering in Python."""

    def __init__(self, center: dt.date, days: int) -> None:
        self.center, self.days = center, days

    def __call__(self, payload: dict) -> bool:
        return abs((dt.date.fromisoformat(payload["date"]) - self.center).days) <= self.days


class VectorIndex(Protocol):
    def add(self, item_id: str, vec: np.ndarray, payload: dict) -> None: ...
    def search(self, vec: np.ndarray, k: int, flt: Filter | None = None) -> list[tuple[str, float, dict]]: ...
    def __len__(self) -> int: ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._vecs: list[np.ndarray] = []
        self._payloads: list[dict] = []

    def add(self, item_id: str, vec: np.ndarray, payload: dict) -> None:
        self._ids.append(item_id)
        self._vecs.append(np.asarray(vec, dtype=np.float32))
        self._payloads.append(payload)

    def search(self, vec: np.ndarray, k: int, flt: Filter | None = None) -> list[tuple[str, float, dict]]:
        if not self._ids:
            return []
        sims = np.stack(self._vecs) @ np.asarray(vec, dtype=np.float32)
        order = np.argsort(-sims)
        out = []
        for i in order:
            if flt is None or flt(self._payloads[i]):
                out.append((self._ids[i], float(sims[i]), self._payloads[i]))
                if len(out) >= k:
                    break
        return out

    def __len__(self) -> int:
        return len(self._ids)


class QdrantIndex:
    """pip install qdrant-client. path=None -> in-process memory, path='data/qdrant' -> persisted on disk."""

    def __init__(self, dim: int, collection: str = "articles", path: str | None = None) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        self._c = QdrantClient(path=path) if path else QdrantClient(":memory:")
        self._col = collection
        self._n = 0
        if self._c.collection_exists(collection):
            self._c.delete_collection(collection)
        self._c.create_collection(collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))

    @staticmethod
    def _pid(item_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, item_id))

    def add(self, item_id: str, vec: np.ndarray, payload: dict) -> None:
        from qdrant_client.models import PointStruct
        self._c.upsert(self._col, [PointStruct(id=self._pid(item_id), vector=[float(x) for x in vec],
                                               payload={**payload, "_id": item_id})])
        self._n += 1

    def search(self, vec: np.ndarray, k: int, flt: Filter | None = None) -> list[tuple[str, float, dict]]:
        # Python-side filtering keeps the Filter interface identical; over-fetch to compensate.
        hits = self._c.query_points(self._col, query=[float(x) for x in vec], limit=max(k * 5, k),
                                    with_payload=True).points
        out = []
        for h in hits:
            p = dict(h.payload)
            if flt is None or flt(p):
                out.append((p.pop("_id"), float(h.score), p))
                if len(out) >= k:
                    break
        return out

    def __len__(self) -> int:
        return self._n


class PgVectorIndex:
    """Postgres + pgvector. Embeddings persist across runs (so a story first seen yesterday can be matched by today's
    papers), writes are upserts, and a DateWindow filter becomes a SQL range predicate. Uses an HNSW index when
    pgvector allows it (<= 2000 dims); above that it falls back to an exact scan."""

    def __init__(self, dsn: str, dim: int, table: str = "article_embeddings", connection=None) -> None:
        import psycopg
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError(f"bad table name {table!r}")
        self.dim, self.table = dim, table
        self._owned = connection is None
        self._conn = connection or psycopg.connect(dsn, autocommit=True)
        c = self._conn
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            id text PRIMARY KEY, embedding vector({dim}) NOT NULL, payload jsonb NOT NULL,
            day date NOT NULL, paper_id text, story_id text, created_at timestamptz NOT NULL DEFAULT now())""")
        have = c.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = %s::regclass AND attname = 'embedding'",
                         (table,)).fetchone()[0]
        if have != dim:
            raise ValueError(f"table {table} holds {have}-dim vectors but the embedder produces {dim}; "
                             f"set NP_EMBED_DIM={have} or use another table")
        c.execute(f"CREATE INDEX IF NOT EXISTS {table}_day ON {table} (day)")
        c.execute(f"CREATE INDEX IF NOT EXISTS {table}_story ON {table} (story_id)")
        if dim <= 2000:
            c.execute(f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} USING hnsw (embedding vector_cosine_ops)")
            try:                                   # keep filtered ANN searches complete (pgvector >= 0.8)
                c.execute("SET hnsw.iterative_scan = relaxed_order")
            except Exception:
                pass

    @staticmethod
    def _lit(vec) -> str:
        return "[" + ",".join(f"{float(x):.6g}" for x in vec) + "]"

    def add(self, item_id: str, vec: np.ndarray, payload: dict) -> None:
        from psycopg.types.json import Jsonb
        self._conn.execute(
            f"""INSERT INTO {self.table} (id, embedding, payload, day, paper_id, story_id)
                VALUES (%s, %s::vector, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, payload = EXCLUDED.payload,
                    day = EXCLUDED.day, paper_id = EXCLUDED.paper_id, story_id = EXCLUDED.story_id""",
            (item_id, self._lit(vec), Jsonb(payload), payload["date"], payload.get("paper_id"), payload.get("story_id")))

    def search(self, vec: np.ndarray, k: int, flt: Filter | None = None) -> list[tuple[str, float, dict]]:
        q, args = self._lit(vec), []
        where = ""
        if isinstance(flt, DateWindow):
            where = "WHERE day BETWEEN %s AND %s"
            args = [flt.center - dt.timedelta(days=flt.days), flt.center + dt.timedelta(days=flt.days)]
            limit, py_filter = k, None
        else:
            limit, py_filter = (k if flt is None else max(k * 5, k)), flt
        rows = self._conn.execute(
            f"SELECT id, 1 - (embedding <=> %s::vector) AS score, payload FROM {self.table} {where} "
            f"ORDER BY embedding <=> %s::vector LIMIT %s", [q, *args, q, limit]).fetchall()
        out = [(r[0], float(r[1]), r[2]) for r in rows if py_filter is None or py_filter(r[2])]
        return out[:k]

    def relabel_story(self, old: str, new: str) -> int:
        cur = self._conn.execute(
            f"UPDATE {self.table} SET story_id = %s, payload = jsonb_set(payload, '{{story_id}}', to_jsonb(%s::text)) "
            f"WHERE story_id = %s", (new, new, old))
        return cur.rowcount

    def __len__(self) -> int:
        return self._conn.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]

    def close(self) -> None:
        if self._owned:
            self._conn.close()
