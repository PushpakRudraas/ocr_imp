"""Enrichment orchestration.

`enrich_article`  - everything computable from one article, in sample.json shape.
`enrich_stories`  - the fields sample.json marks `needs_corpus`: multi-doc summary, cross-paper claim verification,
                    framing comparison, coverage, burst/novelty, impact with multi-paper reach.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from .. import ANALYSIS_VERSION, __version__
from ..store import Store
from . import analysis as A
from .entities import extract_entities
from .event import extract_event
from .geo import resolve_geo
from .trends import increment_counters, trend_novelty

ISO_3166_2_IN = {"punjab": "PB", "haryana": "HR", "delhi": "DL", "maharashtra": "MH", "gujarat": "GJ", "rajasthan": "RJ",
                 "uttar pradesh": "UP", "bihar": "BR", "west bengal": "WB", "tamil nadu": "TN", "kerala": "KL",
                 "karnataka": "KA", "andhra pradesh": "AP", "telangana": "TG", "madhya pradesh": "MP", "odisha": "OD",
                 "assam": "AS", "jharkhand": "JH", "chhattisgarh": "CG", "uttarakhand": "UT", "himachal pradesh": "HP",
                 "jammu and kashmir": "JK", "goa": "GA", "chandigarh": "CH", "ladakh": "LA", "manipur": "MN",
                 "meghalaya": "ML", "tripura": "TR"}
DERIVED_FROM = ["headline", "text", "ocr.byline", "ocr.dateline", "manifest.reputation", "manifest.run_date"]


def _geo_counter_key(geo: dict) -> str | None:
    loc = geo.get("primary_location")
    if not loc:
        return None
    if geo["country_iso"] == "IN" and loc.lower() in ISO_3166_2_IN:
        return f"geo:IN-{ISO_3166_2_IN[loc.lower()]}"
    return f"geo:{geo['country_iso']}-{re.sub(r'[^a-z0-9]+', '-', loc.lower()).strip('-')}"


def enrich_article(rec: dict, models=None) -> dict:
    """`models` = enrich.models.HfModels: real NER / topic / sentiment / emotion models instead of the rules."""
    headline, text = rec["working_headline"], rec["working_text"]
    pub = dt.date.fromisoformat(rec["date"])
    ents = extract_entities(headline, text, models=models)
    event, relations, notes = extract_event(headline, text, ents, pub, rec.get("dateline"))
    city = rec.get("city") if (rec.get("city") or "").isascii() else None
    geo = resolve_geo(headline, text, ents, city, event["institution"])
    cls = A.classify(headline, text, event, geo, models)
    sent = A.sentiment(headline, text, ents, models)
    cluster = rec.get("cluster") or {}

    counters = [f"entity:{e['text']}" for e in ents if e["type"] in ("PERSON", "ORG", "GPE")][:6]
    counters.append(f"topic:{cls['topic']}")
    gk = _geo_counter_key(geo)
    if gk:
        counters.append(gk)
    counters.append(f"event:{event['type'].split('.')[-1].lower()}")

    if cluster:
        near = f"near_duplicate_of:{cluster['near_dup_of']}" if cluster.get("near_dup_of") else "unique"
        dedup = {"exact_dup_key": rec["exact_dup_key"], "exact_duplicate_of": rec.get("exact_duplicate_of"),
                 "near_dup_status": near, "cluster_status": cluster["cluster_status"],
                 "suggested_story_key": cluster["suggested_story_key"],
                 "expected_coverage": f"{len(cluster['story_papers'])} paper(s): {', '.join(cluster['story_papers'])}",
                 "matches": cluster.get("matches", []),
                 "review_candidates": cluster.get("review_candidates", [])}
    else:
        dedup = {"exact_dup_key": rec["exact_dup_key"], "near_dup_status": "needs_corpus",
                 "cluster_status": "needs_corpus", "suggested_story_key": None, "expected_coverage": "needs_corpus"}

    doc = {
        "event_id": rec["event_id"],
        "analysis_version": ANALYSIS_VERSION,
        "input_quality": rec["input_quality"],
        "ingestion_metadata": rec["ingestion_metadata"],
        "language": rec["language_block"],
        "entities": ents,
        "entity_linking": {"status": "unresolved", "note": "no KB lookup ran; link via Wikidata/ReFinED to fill kb_id"},
        "event": event,
        "relations": relations,
        "relation_notes": notes,
        "keyphrases": A.keyphrases(headline, text, ents, event),
        "geo": geo,
        "classification": cls,
        "sentiment": sent,
        "emotion_tone": A.emotion_tone(headline, text, event, bool(rec.get("dateline")), sent["document"]["label"], models),
        "credibility": A.credibility(rec, headline, text),
        "claims": A.extract_claims(headline, text, ents),
        "relevance": {"score": None, "note": "depends on your domain profile"},
        "dedup_clustering": dedup,
        "trend_novelty": {"burst_status": "needs_corpus", "novelty_status": "needs_corpus",
                          "counters_to_increment": counters},
        "summary": {"one_line": A.one_line_summary(headline, text, ents), "multi_doc_summary": "needs_corpus"},
        "timeline_entry": A.timeline_entry(event, headline, rec),
        "knowledge_graph": A.knowledge_graph(ents, event, relations),
        "impact_score": A.impact_score(event, cls["geo_scope"], ents, text, rec),
        "forecast_signal": A.forecast_signal(event["type"]),
        "provenance": {
            "pipeline_stage": "enriched",
            "models": ((f"NER={models.names['ner']}; topic={models.names['category']}; sentiment={models.names['sentiment']}; "
                        f"emotion={models.names['emotion']}; keyphrases/claims/summary/relations/event/geo/credibility are rule-based "
                        f"v{__version__}" if models is not None else f"rule-based offline enrichers v{__version__}")
                       + f"; text from {rec['input_quality']['source']}"
                       f"; translation={rec.get('translation_status')}"),
            "derived_from_fields": DERIVED_FROM},
        "newspaper": {
            "paper_id": rec["paper_id"], "paper": rec["paper_name"], "edition": rec.get("edition"),
            "page_no": rec["page_no"], "pages": rec.get("pages", [rec["page_no"]]), "section": rec.get("section"),
            "headline": rec["headline"], "byline": rec.get("byline"), "dateline": rec.get("dateline"),
            "city": rec.get("city"), "wire_source": rec.get("wire_source"), "language": rec["detected_language"],
            "ocr_confidence": rec.get("ocr_confidence"), "ocr_backend": rec.get("ocr_backend"),
            "status": rec.get("status"), "quality_issues": rec.get("quality_issues", []), "flags": rec.get("flags", []),
            "original_headline": rec.get("original_headline"), "original_text": rec.get("original_text"),
            "translated_headline": rec.get("translated_headline"), "translated_text": rec.get("translated_text"),
            "translation_status": rec.get("translation_status"), "image_refs": rec.get("image_refs", []),
            "text": rec["text"], "headline_clean": rec["headline"]},
        "story": {"story_id": rec.get("story_id"), "papers": cluster.get("story_papers", [rec["paper_id"]]),
                  "n_papers": len(cluster.get("story_papers", [rec["paper_id"]]))},
    }
    return doc


# ------------------------------------------------------------------------------------------------- per-story jobs
_STOP = set("the a an of in on at to for from by with and or as is are was were be been it its this that he she they said "
            "his her their will would has have had not but also after about more than into over".split())
_NUMS = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.]+", s.lower()) if w not in _STOP and len(w) > 2}


def _sim(a: str, b: str) -> float:
    wa, wb = _content_words(a), _content_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMS.findall(s)}


def multi_doc_summary(members: list[dict], max_sentences: int = 3) -> str:
    """Extractive: prefer sentences several papers agree on (support), then add distinct details."""
    if len(members) == 1:
        return "single_source: " + members[0]["summary"]["one_line"]
    pool = []   # (sentence, member index)
    for i, m in enumerate(members):
        for s in A.sentences(m["newspaper"]["translated_text"] or m["newspaper"]["text"])[:6]:
            if len(s) > 30:
                pool.append((s, i))
    scored = []
    for s, i in pool:
        support = len({j for s2, j in pool if j != i and _sim(s, s2) >= 0.3})
        pos = pool.index((s, i)) % 6
        scored.append((support - 0.2 * pos, s, i))
    scored.sort(key=lambda t: -t[0])
    chosen: list[str] = []
    for _, s, _ in scored:
        if all(_sim(s, c) < 0.4 for c in chosen):
            chosen.append(s)
        if len(chosen) >= max_sentences:
            break
    papers = sorted({m["newspaper"]["paper"] for m in members})
    return f"Reported by {len(papers)} papers ({', '.join(papers)}): " + " ".join(chosen)


def _verify_claims(doc: dict, others: list[dict]) -> None:
    for claim in doc["claims"]:
        sup, conflict, sources = 0, False, []
        for o in others:
            best = max(((_sim(claim["claim"], s), s) for s in A.sentences(o["newspaper"]["translated_text"] or o["newspaper"]["text"])),
                       default=(0.0, ""))
            if best[0] >= 0.30:
                na, nb = _numbers(claim["claim"]), _numbers(best[1])
                if na and nb and not (na & nb) and best[0] >= 0.4:
                    conflict = True
                else:
                    sup += 1
                    sources.append(o["newspaper"]["paper"])
        if conflict:
            claim["verification_status"] = "conflicting_figures"
            claim["next_step"] = "papers report different figures; check the primary source"
        elif sup:
            claim["verification_status"] = "corroborated"
            claim["corroborated_by"] = sources
            claim["next_step"] = f"corroborated by {sup} other paper(s); confirm with the primary source for publication"


def enrich_stories(docs: list[dict], store: Store | None = None, day: str | None = None) -> list[dict]:
    """Fill the corpus-dependent fields in place and return docs. Idempotent: re-run after cluster changes."""
    by_story: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        by_story[d["story"]["story_id"] or d["event_id"]].append(d)

    for sid, members in by_story.items():
        papers = sorted({m["newspaper"]["paper_id"] for m in members})
        summary = multi_doc_summary(members)
        sent_by_paper = {m["newspaper"]["paper"]: m["sentiment"]["document"]["label"] for m in members}
        for d in members:
            others = [o for o in members if o is not d and o["newspaper"]["paper_id"] != d["newspaper"]["paper_id"]]
            d["summary"]["multi_doc_summary"] = summary
            _verify_claims(d, others)
            d["story"] = {"story_id": sid, "papers": papers, "n_papers": len(papers),
                          "members": [{"event_id": m["event_id"], "paper": m["newspaper"]["paper"],
                                       "page_no": m["newspaper"]["page_no"], "headline": m["newspaper"]["headline"]}
                                      for m in members]}
            if len(papers) > 1:
                labels = set(sent_by_paper.values())
                d["credibility"]["bias_label"] = ("consistent framing across papers" if len(labels) == 1
                                                  else "framing varies across papers")
                d["credibility"]["cross_paper"] = {
                    "sentiment_by_paper": sent_by_paper,
                    "headline_by_paper": {m["newspaper"]["paper"]: m["newspaper"]["headline"] for m in members}}
            rec_like = {"reputation": d["credibility"]["source_reputation"], "wire_source": d["newspaper"]["wire_source"],
                        "paper_name": d["newspaper"]["paper"]}
            ents = d["entities"]
            d["impact_score"] = A.impact_score(d["event"], d["classification"]["geo_scope"], ents,
                                               d["newspaper"]["text"], rec_like, n_papers=len(papers))
            if store is not None and day:
                d["trend_novelty"] = trend_novelty(d["trend_novelty"]["counters_to_increment"], day, store)
    if store is not None and day:      # one increment per story, so five papers covering it do not look like five events
        for members in by_story.values():
            increment_counters(members[0]["trend_novelty"]["counters_to_increment"], day, store)
    return docs
