"""Exports: one JSON per article (sample.json schema), stories.json (the same news across papers), review.json
(what a human should look at) and metrics.json."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ..ocr.quality import HARD


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def story_views(docs: list[dict]) -> list[dict]:
    """One entry per story: how every paper covered it. Multi-paper stories first."""
    by: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        by[d["story"]["story_id"] or d["event_id"]].append(d)
    views = []
    for sid, members in by.items():
        members.sort(key=lambda d: (d["newspaper"]["paper"], d["newspaper"]["page_no"]))
        lead = max(members, key=lambda d: d["impact_score"]["value"])
        views.append({
            "story_id": sid,
            "suggested_story_key": lead["dedup_clustering"].get("suggested_story_key"),
            "n_papers": len({m["newspaper"]["paper_id"] for m in members}),
            "event_type": lead["event"]["type"],
            "location": lead["geo"]["primary_location"],
            "topic": lead["classification"]["topic"],
            "impact": max(m["impact_score"]["value"] for m in members),
            "summary": lead["summary"]["multi_doc_summary"],
            "bias_label": lead["credibility"]["bias_label"],
            "coverage": [{
                "paper": m["newspaper"]["paper"], "paper_id": m["newspaper"]["paper_id"],
                "page_no": m["newspaper"]["page_no"], "pages": m["newspaper"]["pages"],
                "headline": m["newspaper"]["headline"], "translated_headline": m["newspaper"]["translated_headline"],
                "byline": m["newspaper"]["byline"], "language": m["newspaper"]["language"],
                "ocr_confidence": m["newspaper"]["ocr_confidence"], "event_id": m["event_id"],
                "sentiment": m["sentiment"]["document"]["label"]} for m in members]})
    views.sort(key=lambda v: (-v["n_papers"], -v["impact"], v["story_id"]))
    return views


def review_items(docs: list[dict]) -> list[dict]:
    items = []
    for d in docs:
        reasons = [q for q in d["newspaper"]["quality_issues"] if q in HARD]      # soft issues (short brief) are not review items
        cands = d["dedup_clustering"].get("review_candidates") or []
        if cands:
            reasons.append("borderline_story_match")
        if d["newspaper"].get("translation_status") in ("failed", "partial", "failed_no_backend"):
            reasons.append("translation_" + d["newspaper"]["translation_status"])
        if reasons:
            items.append({"event_id": d["event_id"], "paper": d["newspaper"]["paper"], "page_no": d["newspaper"]["page_no"],
                          "headline": d["newspaper"]["headline"], "reasons": reasons, "match_candidates": cands,
                          "ocr_confidence": d["newspaper"]["ocr_confidence"]})
    return items


def export_all(docs: list[dict], out_dir: str | Path, metrics: dict | None = None) -> dict:
    out = Path(out_dir)
    for d in docs:
        _dump(out / "articles" / d["newspaper"]["paper_id"] / f"{d['event_id'][:16]}.json", d)
    stories = story_views(docs)
    review = review_items(docs)
    _dump(out / "stories.json", stories)
    _dump(out / "review.json", review)
    if metrics is not None:
        _dump(out / "metrics.json", metrics)
    return {"articles": len(docs), "stories": len(stories), "review": len(review)}
