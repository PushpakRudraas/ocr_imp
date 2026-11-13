"""Evaluation tool for the story-matching engine.

  export  - take articles of two (or more) newspapers from MongoDB `clean_events`, build candidate cross-paper pairs, score them with
            the CURRENT scorer and write a CSV for a person to label (column `label`: same / different / unsure).
  score   - read the labelled CSV, re-score every pair with the current code and report precision, recall, F1, false-positive rate,
            false-negative rate, candidate-retrieval recall (was the partner among the nearest articles?), and the false negatives
            with the signals behind each decision.

Run inside the enrich-worker container, which has the models and the MongoDB address:
  python -m newspaper_pipeline.evaltools.story_eval export --date 2026-11-12 --papers hindustan_times,the_times_of_india --out /data/pairs.csv
  python -m newspaper_pipeline.evaltools.story_eval score  --labels /data/pairs_labelled.csv --date 2026-11-12 --papers hindustan_times,the_times_of_india

The label column is ground truth supplied by a person. This tool never labels anything itself.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass

import numpy as np

LABELS = ("same", "different", "unsure")
MERGE, REVIEW = 0.75, 0.50                  # the pipeline's thresholds (config: same_story_threshold / borderline_threshold)


# ------------------------------------------------------------------------------------------------ pure metrics (unit-tested)
@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)

    @property
    def fpr(self) -> float | None:                       # false-positive rate: different pairs wrongly merged
        return self.fp / (self.fp + self.tn) if (self.fp + self.tn) else None

    @property
    def fnr(self) -> float | None:                       # false-negative rate: same-event pairs missed
        return self.fn / (self.fn + self.tp) if (self.fn + self.tp) else None


def pair_id(a: str, b: str) -> str:
    return hashlib.sha1("|".join(sorted((a, b))).encode()).hexdigest()[:12]


def confusion(rows: list[dict], threshold: float) -> Counts:
    """`rows`: dicts with `label` (same / different; others are ignored) and `score`. A pair is predicted 'same' at score >= threshold."""
    c = Counts()
    for r in rows:
        if r["label"] not in ("same", "different"):
            continue
        pred, truth = r["score"] >= threshold, r["label"] == "same"
        if pred and truth: c.tp += 1
        elif pred: c.fp += 1
        elif truth: c.fn += 1
        else: c.tn += 1
    return c


def recall_at_k(rows: list[dict], ks=(5, 10, 20)) -> dict[int, float | None]:
    """Candidate-retrieval recall: for the pairs labelled 'same', how often the partner was among the k nearest articles by embedding."""
    ranks = [r["rank"] for r in rows if r["label"] == "same" and r.get("rank") is not None]
    return {k: (sum(x <= k for x in ranks) / len(ranks) if ranks else None) for k in ks}


def fmt(x, pct=True) -> str:
    return "n/a" if x is None else (f"{100 * x:.1f}%" if pct else f"{x:.3f}")


def report(rows: list[dict], merge: float = MERGE, review: float = REVIEW) -> str:
    n = {k: sum(r["label"] == k for r in rows) for k in LABELS}
    out = [f"labelled pairs: same={n['same']} different={n['different']} unsure={n['unsure']} (unsure is left out of the metrics)"]
    for name, th in (("merge", merge), ("merge or review", review)):
        c = confusion(rows, th)
        out.append(f"\nthreshold {th:.2f} ({name}):  TP={c.tp} FP={c.fp} FN={c.fn} TN={c.tn}\n"
                   f"  precision {fmt(c.precision)}  recall {fmt(c.recall)}  F1 {fmt(c.f1, False)}  "
                   f"false-positive rate {fmt(c.fpr)}  false-negative rate {fmt(c.fnr)}")
    rk = recall_at_k(rows)
    out.append("\ncandidate retrieval (partner among the k nearest articles by embedding): " +
               "  ".join(f"recall@{k} {fmt(v)}" for k, v in rk.items()))
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------ I/O (needs MongoDB + the models)
def _load_articles(date: str, papers: list[str], min_chars: int = 120) -> list[dict]:
    from pymongo import MongoClient
    db = MongoClient(os.environ.get("NP_MONGO_URI", "mongodb://localhost:27017"))[os.environ.get("NP_MONGO_DB", "newspaper")]
    arts = []
    for a in db.clean_events.find({"date": date, "paper_id": {"$in": papers}}):
        a.pop("_id", None)
        a.setdefault("working_text", a.get("text", ""))
        a.setdefault("working_headline", a.get("headline", ""))
        if len(a.get("working_text") or "") >= min_chars and a["working_headline"]:
            arts.append(a)
    return arts


def _embedder(name: str):
    from ..cluster.embed import HashingEmbedder, SentenceTransformerEmbedder, minilm_embedder
    from ..device import resolve_device
    dev = resolve_device(os.environ.get("NP_DEVICE", "auto"))
    if name == "bge-m3":
        return SentenceTransformerEmbedder("BAAI/bge-m3", dev, cos_ref=0.75)
    if name == "minilm":
        return minilm_embedder(dev)
    return HashingEmbedder()


class Engine:
    """Embeds the corpus once and scores any pair with the current scorer."""

    def __init__(self, arts: list[dict], embedder_name: str) -> None:
        from ..cluster.embed import embedding_text
        from ..cluster.keystats import KeywordStats
        from ..cluster.scorer import RuleScorer
        from ..cluster.signals import keywords
        self.arts = arts
        self.by_id = {a["event_id"]: i for i, a in enumerate(arts)}
        emb = _embedder(embedder_name)
        self.V = emb.embed([embedding_text(a) for a in arts])
        self.scorer = RuleScorer(cos_ref=emb.cos_ref)
        self.stats = KeywordStats()
        for a in arts:
            self.stats.update(a["event_id"], keywords(a["working_text"], a["working_headline"]))
        self.sim = self.V @ self.V.T

    def score(self, ia: int, ib: int):
        from ..cluster.signals import pair_features
        f = pair_features(self.arts[ia], self.arts[ib], float(self.sim[ia, ib]), self.stats)
        return self.scorer.score(f)

    def rank(self, ia: int, ib: int) -> int:
        """Position of b among a's nearest OTHER-PAPER articles (1 = nearest), the better of the two directions."""
        def one(x, y):
            others = [j for j, a in enumerate(self.arts) if a["paper_id"] != self.arts[x]["paper_id"]]
            order = sorted(others, key=lambda j: -self.sim[x, j])
            return order.index(y) + 1 if y in order else None
        r = [v for v in (one(ia, ib), one(ib, ia)) if v is not None]
        return min(r) if r else None


def cmd_export(a) -> int:
    papers = a.papers.split(",")
    arts = _load_articles(a.date, papers)
    eng = Engine(arts, a.embedder)
    cand: dict[str, tuple[int, int]] = {}
    for i, art in enumerate(arts):
        others = [j for j, b in enumerate(arts) if b["paper_id"] != art["paper_id"]]
        for j in sorted(others, key=lambda j: -eng.sim[i, j])[:a.top_k]:
            cand.setdefault(pair_id(art["event_id"], arts[j]["event_id"]), (i, j))
    rows = []
    for pid, (i, j) in cand.items():
        s = eng.score(i, j)
        rows.append({"pair_id": pid, "event_id_a": arts[i]["event_id"], "event_id_b": arts[j]["event_id"],
                     "paper_a": arts[i]["paper_id"], "headline_a": arts[i]["working_headline"][:120],
                     "text_a": " ".join(arts[i]["working_text"].split())[:300],
                     "paper_b": arts[j]["paper_id"], "headline_b": arts[j]["working_headline"][:120],
                     "text_b": " ".join(arts[j]["working_text"].split())[:300],
                     "cosine": round(float(eng.sim[i, j]), 3), "score": round(s.score, 3),
                     "decision": "merge" if s.score >= MERGE else "review" if s.score >= REVIEW else "apart",
                     "components": json.dumps(s.components), "veto": s.veto or "", "label": "", "note": ""})
    rows.sort(key=lambda r: -r["score"])
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {len(rows)} candidate pairs to {a.out} - fill the `label` column with: same / different / unsure")
    return 0


def cmd_score(a) -> int:
    papers = a.papers.split(",")
    eng = Engine(_load_articles(a.date, papers), a.embedder)
    rows, skipped = [], 0
    with open(a.labels, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            lab = (r.get("label") or "").strip().lower()
            if lab not in LABELS:
                continue
            ia, ib = eng.by_id.get(r["event_id_a"]), eng.by_id.get(r["event_id_b"])
            if ia is None or ib is None:
                skipped += 1
                continue
            s = eng.score(ia, ib)
            rows.append({"label": lab, "score": s.score, "rank": eng.rank(ia, ib), "a": eng.arts[ia]["working_headline"],
                         "b": eng.arts[ib]["working_headline"], "components": s.components, "reasons": s.reasons, "veto": s.veto})
    if skipped:
        print(f"warning: {skipped} labelled pairs reference articles that are not in the loaded set", file=sys.stderr)
    print(report(rows, a.merge, a.review))
    fn = [r for r in rows if r["label"] == "same" and r["score"] < a.merge]
    fp = [r for r in rows if r["label"] == "different" and r["score"] >= a.merge]
    for title, lst in (("MISSED (labelled same, scored below the merge line)", fn), ("WRONGLY MERGED (labelled different)", fp)):
        print(f"\n{title}: {len(lst)}")
        for r in sorted(lst, key=lambda r: -r["score"])[:15]:
            print(f"  score {r['score']:.2f}  nearest-rank {r['rank']}  veto={r['veto']}\n    A: {r['a'][:70]}\n    B: {r['b'][:70]}\n    {r['components']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("export", "score"):
        s = sub.add_parser(name)
        s.add_argument("--date", required=True)
        s.add_argument("--papers", required=True, help="comma-separated paper ids")
        s.add_argument("--embedder", default=os.environ.get("NP_EMBEDDER", "bge-m3"), choices=["bge-m3", "minilm", "hashing"])
    e = sub.choices["export"]; e.add_argument("--out", required=True); e.add_argument("--top-k", type=int, default=6)
    sc = sub.choices["score"]; sc.add_argument("--labels", required=True)
    sc.add_argument("--merge", type=float, default=MERGE); sc.add_argument("--review", type=float, default=REVIEW)
    a = p.parse_args(argv)
    return cmd_export(a) if a.cmd == "export" else cmd_score(a)


if __name__ == "__main__":
    raise SystemExit(main())
