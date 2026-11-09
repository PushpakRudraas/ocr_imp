"""Pre-clean filter on raw OCR events: drop empties/stubs, then drop duplicates within one newspaper.

1. A record whose headline or text is empty / shorter than `min_chars` (after trimming) is removed - OCR noise such as
   a stray caption or a masthead fragment is not an article.
2. Within one paper (and edition date), records sharing the same normalised headline OR the same normalised text are
   duplicates (a repeated edition block, or OCR splitting the same story twice). The best copy is kept - the one with
   the longest text, ties broken by higher OCR confidence, then input order - and the rest are removed.

Normalisation ignores case, punctuation and whitespace. Returns the kept records plus an audit list of what was
removed and why, so nothing disappears silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_CHARS = 10


def _norm(s: str | None) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


@dataclass
class FilterResult:
    kept: list[dict]
    removed: list[dict] = field(default_factory=list)       # {"article_id", "paper_id", "reason", "duplicate_of"?}

    def stats(self) -> dict:
        reasons: dict[str, int] = {}
        for r in self.removed:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        return {"input": len(self.kept) + len(self.removed), "kept": len(self.kept),
                "removed": len(self.removed), "by_reason": reasons}


def filter_raw_events(recs: list[dict], min_chars: int = MIN_CHARS) -> FilterResult:
    removed: list[dict] = []

    def drop(r: dict, reason: str, dup_of: str | None = None) -> None:
        d = {"article_id": r.get("article_id"), "paper_id": r.get("paper_id"), "date": r.get("date"), "reason": reason}
        if dup_of:
            d["duplicate_of"] = dup_of
        removed.append(d)

    valid: list[dict] = []
    for r in recs:
        h, t = (r.get("headline") or "").strip(), (r.get("text") or "").strip()
        if not h or not t:
            drop(r, "empty_headline" if not h else "empty_text")
        elif len(h) < min_chars or len(t) < min_chars:
            drop(r, "short_headline" if len(h) < min_chars else "short_text")
        else:
            valid.append(r)

    def rank(i: int) -> tuple:
        r = valid[i]
        return (-len((r.get("text") or "").strip()), -(r.get("ocr_confidence") or 0), i)

    # union records of the same paper+date that share a headline or a text
    parent = list(range(len(valid)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: dict[tuple, int] = {}
    for i, r in enumerate(valid):
        for kind, val in (("h", _norm(r["headline"])), ("t", _norm(r["text"]))):
            key = (r.get("paper_id"), r.get("date"), kind, val)
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(valid)):
        groups.setdefault(find(i), []).append(i)
    drop_idx: dict[int, int] = {}
    for members in groups.values():
        best = min(members, key=rank)
        for i in members:
            if i != best:
                drop_idx[i] = best
    kept = []
    for i, r in enumerate(valid):
        if i in drop_idx:
            same_head = _norm(r["headline"]) == _norm(valid[drop_idx[i]]["headline"])
            drop(r, "duplicate_headline" if same_head else "duplicate_text", valid[drop_idx[i]].get("article_id"))
        else:
            kept.append(r)
    return FilterResult(kept, removed)
