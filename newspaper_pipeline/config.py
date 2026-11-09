"""All thresholds and toggles in one place; every value is env-overridable (same convention as the
ARGUS enrichment-svc config.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    data_dir: str = os.environ.get("NP_DATA_DIR", "data")
    db_path: str = os.environ.get("NP_DB_PATH", "data/pipeline.sqlite")
    render_dpi: int = _i("NP_RENDER_DPI", 300)
    # OCR quality gate
    min_article_conf: float = _f("NP_MIN_ARTICLE_CONF", 0.85)
    min_article_chars: int = _i("NP_MIN_ARTICLE_CHARS", 200)
    # dedup / clustering
    candidate_window_h: int = _i("NP_CANDIDATE_WINDOW_H", 48)
    candidate_top_k: int = _i("NP_CANDIDATE_TOP_K", 10)
    same_story_threshold: float = _f("NP_SAME_STORY_THRESHOLD", 0.75)
    borderline_threshold: float = _f("NP_BORDERLINE_THRESHOLD", 0.50)
    near_dup_jaccard: float = _f("NP_NEAR_DUP_JACCARD", 0.85)
    # backends (all env-driven; docker-compose.yml sets them for the containerised stack)
    store_backend: str = os.environ.get("NP_STORE", "sqlite")          # sqlite | mongo
    mongo_uri: str = os.environ.get("NP_MONGO_URI", "mongodb://localhost:27017")
    mongo_db: str = os.environ.get("NP_MONGO_DB", "newspaper")
    index_backend: str = os.environ.get("NP_INDEX", "memory")          # memory | qdrant | pgvector
    pg_dsn: str = os.environ.get("NP_PG_DSN", "postgresql://newspaper:newspaper-dev@localhost:5432/newspaper")
    embed_dim: int = _i("NP_EMBED_DIM", 4096)
    graph_backend: str = os.environ.get("NP_GRAPH", "none")            # none | neo4j
    neo4j_uri: str = os.environ.get("NP_NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.environ.get("NP_NEO4J_USER", "neo4j")
    neo4j_password: str = os.environ.get("NP_NEO4J_PASSWORD", "newspaper-dev")
    # translation
    target_language: str = os.environ.get("NP_TARGET_LANGUAGE", "en")
    translate_chunk_chars: int = _i("NP_TRANSLATE_CHUNK_CHARS", 2000)
