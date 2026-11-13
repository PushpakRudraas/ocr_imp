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


def clean_views(arts: list[dict]) -> list[dict]:
    """Phase-1 view of the stored clean events: what OCR + cleaning + translation produced, before any enrichment."""
    out = []
    for a in arts:
        text = a.get("translated_text") or a.get("text") or ""
        out.append({
            "event_id": a["event_id"], "article_id": a.get("article_id"), "paper": a.get("paper_name") or a["paper_id"], "paper_id": a["paper_id"],
            "page_no": a.get("page_no"), "headline": a.get("headline", ""), "translated_headline": a.get("translated_headline"),
            "byline": a.get("byline"), "language": a.get("detected_language") or a.get("language"),
            "translation_status": a.get("translation_status"), "ocr_confidence": a.get("ocr_confidence"),
            "status": a.get("status"), "chars": len(a.get("text") or ""), "excerpt": text[:400],
            "exact_duplicate_of": a.get("exact_duplicate_of")})
    out.sort(key=lambda v: (v["paper"], v["page_no"] or 0, v["event_id"]))
    return out


def ocr_views(recs: list[dict], removed: list[dict] | None = None) -> list[dict]:
    """Every raw OCR article, in reading order (paper as given, then page, then top-to-bottom / left-to-right), including
    the ones the clean filter later dropped (ads, stubs, duplicates) and those with low OCR confidence."""
    why = {r["article_id"]: r for r in (removed or [])}
    paper_rank: dict[str, int] = {}
    for r in recs:
        paper_rank.setdefault(r.get("paper_id"), len(paper_rank))

    def pos(r: dict) -> tuple:
        bb = r.get("headline_bbox") or (r.get("body_bboxes") or [[0, 0]])[0]
        return (paper_rank[r.get("paper_id")], r.get("page_no") or 0, round(bb[1] / 20), bb[0])

    out = []
    for _, r in sorted(enumerate(recs), key=lambda t: (pos(t[1]), t[0])):
        text = r.get("text") or ""
        drop = why.get(r.get("article_id"))
        out.append({
            "article_id": r.get("article_id"), "paper": r.get("paper_name") or r.get("paper_id"), "paper_id": r.get("paper_id"),
            "page_no": r.get("page_no"), "headline": r.get("headline", ""), "byline": r.get("byline"),
            "language": r.get("language"), "ocr_confidence": r.get("ocr_confidence"), "status": r.get("status"),
            "quality_issues": r.get("quality_issues") or [], "flags": r.get("flags") or [],
            "chars": len(text), "text": text,
            "bboxes": ([r["headline_bbox"]] if r.get("headline_bbox") else []) + list(r.get("body_bboxes") or []),
            "ad_label": r.get("ad_label"), "ad_reason": r.get("ad_reason"), "ad_score": r.get("ad_score"),
            "kept": drop is None, "removed_reason": drop["reason"] if drop else None,
            "duplicate_of": drop.get("duplicate_of") if drop else None})
    return out


def export_ocr(recs: list[dict], out_dir: str | Path, removed: list[dict] | None = None) -> dict:
    _dump(Path(out_dir) / "ocr_articles.json", ocr_views(recs, removed))
    return {"ocr": len(recs)}


def export_pages(page_regions: list[dict], out_dir: str | Path) -> dict:
    """pages.json: for every page its size (PDF points) and the YOLO ad/article regions - the UI's page view draws on these."""
    _dump(Path(out_dir) / "pages.json", sorted(page_regions, key=lambda p: (p["paper_id"], p["page_no"])))
    return {"pages": len(page_regions)}


def export_clean(arts: list[dict], out_dir: str | Path) -> dict:
    _dump(Path(out_dir) / "clean_articles.json", clean_views(arts))
    return {"clean": len(arts)}


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
