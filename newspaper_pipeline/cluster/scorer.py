"""Pair scoring: are these two articles the same event?  0..1.

`RuleScorer` blends, in priority order: embedding similarity (60%), shared entities (20%), date agreement (10%) and
keyword overlap (10%), with extra penalties for date gaps / same-paper pairs. `cos_ref` is the cosine that counts as "clearly
the same story" for the embedder in use (hashing TF-IDF ~0.40, BGE-M3 ~0.75).

For borderline pairs a judge (LLM or cross-encoder) can be plugged in: anything with `judge(a, b) -> float | None`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .signals import PairFeatures


@dataclass
class Scored:
    score: float
    reasons: list[str] = field(default_factory=list)


class PairJudge(Protocol):
    def judge(self, a: dict, b: dict) -> float | None:
        """Probability the two articles describe the same event, or None to abstain."""


class RuleScorer:
    """Weighted blend, in order of importance: semantics (embedding cosine) > entities > date > keywords."""

    W_SEMANTIC, W_ENTITY, W_DATE, W_KEYWORD = 0.60, 0.20, 0.10, 0.10
    DATE_CURVE = {0: 0.7, 1: 0.5, 2: 0.2}              # date-gap days -> date agreement; 3+ days -> 0

    def __init__(self, cos_ref: float = 0.40, min_semantic: float = 0.3) -> None:
        self.cos_ref, self.min_semantic = cos_ref, min_semantic

    def components(self, f: PairFeatures) -> dict[str, float]:
        """Each signal normalised to 0..1."""
        sem = min(1.0, max(0.0, f.cosine) / self.cos_ref)
        ent = (0.30 * f.entity_overlap + 0.30 * f.entity_jaccard + 0.30 * min(1.0, len(f.shared_entities) / 4)
               + 0.10 * max(f.same_city, f.wire_match))                       # dateline city / wire agency
        date = self.DATE_CURVE.get(f.date_gap_days, 0.0)
        kw = (0.60 * f.keyword_overlap + 0.20 * f.number_overlap
              + 0.20 * max(min(1.0, f.shingle_jaccard / 0.3), min(1.0, f.quote_overlap * 2)))
        return {"semantic": sem, "entity": min(1.0, ent), "date": date, "keyword": min(1.0, kw)}

    def score(self, f: PairFeatures) -> Scored:
        c = self.components(f)
        s = (self.W_SEMANTIC * c["semantic"] + self.W_ENTITY * c["entity"]
             + self.W_DATE * c["date"] + self.W_KEYWORD * c["keyword"])
        reasons = [f"cos={f.cosine:.2f}", f"entities={len(f.shared_entities)}", f"keywords={f.keyword_overlap:.2f}"]
        if f.date_gap_days > 1:
            s -= 0.15 * (f.date_gap_days - 1)                                 # stale pairs: hard penalty on top
            reasons.append(f"date_gap={f.date_gap_days}d")
        if f.same_paper and f.date_gap_days == 0:
            s -= 0.15
            reasons.append("same_paper_same_day")
        if c["semantic"] < self.min_semantic:                                 # a shared city name alone is not a story
            s = min(s, 0.3)
            reasons.append("semantic_gate")
        return Scored(max(0.0, min(1.0, s)), reasons)


class LlmJudge:
    """Asks Claude whether two articles cover the same event. `client` is an anthropic.Anthropic (or a fake with
    `messages.create`). Returns None on any failure so the rule score stands."""

    PROMPT = ("Two newspaper articles are below. Do they report the SAME real-world event (same actors, same "
              "occurrence, same day)? Answer with JSON only: {\"same_event\": true|false, \"confidence\": 0..1}.\n\n"
              "ARTICLE A (%s): %s\n%s\n\nARTICLE B (%s): %s\n%s")

    def __init__(self, client=None, model: str = "claude-haiku-5-5", max_chars: int = 1200) -> None:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client, self.model, self.max_chars = client, model, max_chars

    def judge(self, a: dict, b: dict) -> float | None:
        prompt = self.PROMPT % (a["paper_name"], a["working_headline"], a["working_text"][:self.max_chars],
                                b["paper_name"], b["working_headline"], b["working_text"][:self.max_chars])
        try:
            resp = self.client.messages.create(model=self.model, max_tokens=100,
                                               messages=[{"role": "user", "content": prompt}])
            data = json.loads(resp.content[0].text.strip())
            conf = float(data.get("confidence", 0.5))
            return conf if data.get("same_event") else 1.0 - conf
        except Exception:
            return None
