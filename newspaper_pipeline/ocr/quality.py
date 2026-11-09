"""Quality gate. conf < threshold -> optional fallback (e.g. a VLM re-OCR); still low -> status 'review'."""
from __future__ import annotations

import re
from typing import Callable

from ..config import Settings

_GARBAGE = re.compile(r"[^\w\s.,;:!?'\"()\-–—/%&@#$₹£€+*ऀ-ॿ஀-௿ఀ-౿]", re.U)


def garbage_ratio(text: str) -> float:
    t = re.sub(r"\s+", "", text or "")
    return len(_GARBAGE.findall(t)) / len(t) if t else 1.0


def assess(rec: dict, s: Settings) -> list[str]:
    problems = []
    if rec["ocr_confidence"] < s.min_article_conf:
        problems.append("low_confidence")
    if garbage_ratio(rec["text"]) > 0.08:
        problems.append("garbled_text")
    if len(rec["text"]) < s.min_article_chars:
        problems.append("short_text")
    if not rec["headline"]:
        problems.append("no_headline")
    if "missing_continuation" in rec["flags"] or "orphan_continuation" in rec["flags"]:
        problems.append("broken_continuation")
    return problems


# a short brief or a missing headline is a content oddity, not an OCR failure
HARD = {"low_confidence", "garbled_text", "broken_continuation"}


def apply_gate(rec: dict, s: Settings, fallback: Callable[[dict], tuple[str, float] | None] | None = None) -> dict:
    """Sets rec['status'] in {'ok','review'} and rec['quality_issues']. `fallback(rec) -> (text, conf)|None`."""
    problems = assess(rec, s)
    if (set(problems) & {"low_confidence", "garbled_text"}) and fallback is not None:
        res = fallback(rec)
        if res:
            rec["text"], rec["ocr_confidence"] = res[0], round(res[1], 4)
            rec["flags"].append("fallback_used")
            problems = assess(rec, s)
    rec["quality_issues"] = problems
    rec["status"] = "review" if set(problems) & HARD else "ok"
    return rec
