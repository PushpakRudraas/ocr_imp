"""Canonical article record: clean -> language -> translate -> sample.json-shaped base fields.

Produces the `input_quality`, `ingestion_metadata`, `language` blocks and the content hash used for exact-duplicate
detection. e-paper only carries a publication *date*, so `published_at` is that date at 00:00Z and
`published_precision` says so.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
from typing import Callable

from ..config import Settings
from .lang import detect_language
from .text import clean_headline, normalize_ocr_text
from .translate import TranslationBackend, translate_article


def _iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"


def content_hash(text: str) -> str:
    """Stable across whitespace/case/punctuation differences, so re-OCR noise of that kind still collides."""
    norm = re.sub(r"[\W_]+", " ", (text or "").lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def clean_article(rec: dict, backend: TranslationBackend | None, settings: Settings | None = None,
                  ingested_ms: float | None = None, clock: Callable[[], float] = time.time) -> dict:
    """OCR record -> canonical clean record (a new dict)."""
    s = settings or Settings()
    t_clean0 = clock() * 1000
    ingested_ms = ingested_ms if ingested_ms is not None else t_clean0

    out = dict(rec)
    out["original_headline"] = rec.get("headline", "")
    out["original_text"] = rec.get("text", "")
    out["headline"] = clean_headline(rec.get("headline", ""))
    out["text"] = normalize_ocr_text(rec.get("text", ""))

    lang, conf = detect_language(f"{out['headline']}\n{out['text']}", hint=rec.get("language"))
    out["detected_language"], out["language_confidence"] = lang, conf
    out = translate_article(out, backend, s)

    working_headline = out.get("translated_headline") or out["headline"]
    working_text = out.get("translated_text") or out["text"]
    out["working_headline"], out["working_text"] = working_headline, working_text

    body_ok = len(out["text"]) >= 80
    cleaned_ms = clock() * 1000
    published = dt.datetime.combine(dt.date.fromisoformat(rec["date"]), dt.time.min, tzinfo=dt.timezone.utc)
    published_ms = published.timestamp() * 1000

    out["input_quality"] = {
        "text_level": "full_body" if body_ok else "headline_only",
        "body_available": body_ok,
        "summary_usable": body_ok,
        "source": "ocr" if rec.get("ocr_backend") == "image_ocr" else "pdf_text_layer",
        "ocr_confidence": rec.get("ocr_confidence"),
        "note": "full article text recovered from the e-paper page" if body_ok
        else "body too short; enrichment relies on the headline",
    }
    out["ingestion_metadata"] = {
        "published_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_precision": "date_only",
        "ingested_at": _iso(ingested_ms),
        "cleaned_at": _iso(cleaned_ms),
        "publish_to_ingest_sec": round((ingested_ms - published_ms) / 1000, 1),
        "ingest_to_clean_ms": int(round(cleaned_ms - ingested_ms)),
        "author": rec.get("byline"),
        "wire_source": rec.get("wire_source"),
    }
    out["language_block"] = {
        "detected": lang,
        "confidence": "high" if conf >= 0.8 else "medium" if conf >= 0.5 else "low",
        "translation_needed": out["translation_status"] not in ("skipped_already_target", "skipped_no_text"),
    }
    out["content_hash"] = content_hash(f"{working_headline} {working_text}")
    out["exact_dup_key"] = f"content_hash:{out['content_hash']}"
    out["event_id"] = hashlib.sha256(
        f"{rec['paper_id']}:{rec['date']}:{rec['article_id']}|{out['content_hash']}".encode()).hexdigest()
    return out


def mark_exact_duplicates(records: list[dict]) -> list[dict]:
    """Within one run: identical normalised text (same paper's repeated edition, or a verbatim wire copy) is flagged
    `exact_duplicate_of=<first event_id>`. Does not remove anything - clustering decides what to do with it."""
    seen: dict[str, str] = {}
    for r in records:
        first = seen.setdefault(r["content_hash"], r["event_id"])
        r["exact_duplicate_of"] = first if first != r["event_id"] else None
    return records
