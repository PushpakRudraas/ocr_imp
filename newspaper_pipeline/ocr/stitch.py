"""Join articles that continue on another page ("Continued on page 3" ... "CONTINUED FROM PAGE 1")."""
from __future__ import annotations

import re

_WORD = re.compile(r"[\wऀ-ॿ]{3,}")


def _tokens(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s or "")}


def _similarity(src: dict, cont: dict) -> float:
    a = _tokens(src["headline"] + " " + src["text"][:600])
    b = _tokens(cont["headline"] + " " + cont["text"][:400])
    return len(a & b) / (len(b) or 1)


def stitch_continuations(records: list[dict]) -> list[dict]:
    """Merge continuation fragments into their source article (same paper). Returns the new record list.

    A fragment matches a source on an earlier page that points at the fragment's page via `continued_to`.
    With several candidates, the one with the highest token overlap wins. Unmatched fragments are kept and
    flagged `orphan_continuation`; sources whose continuation never shows up get `missing_continuation`.
    """
    by_paper: dict[str, list[dict]] = {}
    for r in records:
        by_paper.setdefault(r["paper_id"], []).append(r)

    out: list[dict] = []
    for paper_id, recs in by_paper.items():
        sources = [r for r in recs if r.get("continued_to")]
        used: set[str] = set()
        merged: set[str] = set()
        for frag in sorted((r for r in recs if "is_continuation" in r["flags"]), key=lambda r: r["page_no"]):
            cands = [s for s in sources if s["article_id"] not in used and s["continued_to"] == frag["page_no"]
                     and s["page_no"] < frag["page_no"]
                     and (frag.get("continued_from") in (None, s["page_no"]))]
            if not cands:
                frag["flags"].append("orphan_continuation")
                continue
            src = max(cands, key=lambda s: _similarity(s, frag))
            used.add(src["article_id"])
            merged.add(frag["article_id"])
            n_src, n_frag = len(src["text"]), len(frag["text"])
            src["text"] = (src["text"].rstrip() + "\n" + frag["text"].lstrip()).strip()
            src["ocr_confidence"] = round(
                (src["ocr_confidence"] * n_src + frag["ocr_confidence"] * n_frag) / max(1, n_src + n_frag), 4)
            src["pages"] = sorted(set(src["pages"] + frag["pages"]))
            src["body_bboxes"] = src["body_bboxes"] + frag["body_bboxes"]
            src["image_refs"] = src["image_refs"] + frag["image_refs"]
            src["flags"] = [f for f in src["flags"] if f != "continued_to"] + ["stitched"]
        for r in recs:
            if r["article_id"] in merged:
                continue
            if r.get("continued_to") and r["article_id"] not in used:
                r["flags"].append("missing_continuation")
            out.append(r)
    out.sort(key=lambda r: (r["paper_id"], r["page_no"], r["headline_bbox"][1] if r["headline_bbox"] else 0))
    return out
