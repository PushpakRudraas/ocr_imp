"""Pairwise signals that tell whether two articles report the same event, beyond embedding similarity: shared proper
nouns, shared figures, shared quotes, shingle overlap (verbatim wire copy), date gap and wire/dateline agreement.
MinHash comes from ARGUS etl-pipeline-svc/etl/dedup.py."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import numpy as np
from datasketch import MinHash

NUM_PERM = 128
_STOP_CAPS = {"the", "a", "an", "in", "on", "at", "of", "and", "but", "he", "she", "it", "they", "his", "her",
              "this", "that", "these", "those", "mr", "mrs", "ms", "dr", "shri", "smt", "monday", "tuesday",
              "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april",
              "may", "june", "july", "august", "september", "october", "november", "december", "nov", "oct",
              "after", "party", "said", "analysts", "observers", "officials", "opposition", "residents",
              "doctors", "schools", "markets", "bank", "governor", "speaker", "by", "for", "with", "as", "from"}
_CAP_PHRASE = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}))*\b")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_QUOTE = re.compile(r"[\"“]([^\"”]{25,200})[\"”]")
_WORD = re.compile(r"[^\W_]+", re.U)


def proper_nouns(text: str) -> set[str]:
    """Capitalised phrases, split into their words so 'Vijay Pratap Singh' and 'Kunwar Vijay Pratap Singh' overlap.
    Sentence-initial capitals are ignored unless the word is an all-caps acronym or part of a multi-word name."""
    out: set[str] = set()
    for m in _CAP_PHRASE.finditer(text or ""):
        words = m.group(0).split()
        sentence_start = m.start() == 0 or re.search(r"[.!?]\s*$", text[max(0, m.start() - 3):m.start()]) is not None
        for i, w in enumerate(words):
            lw = w.lower()
            if lw in _STOP_CAPS:
                continue
            if len(words) == 1 and sentence_start and not (w.isupper() and len(w) >= 2):
                continue
            out.add(lw)
    return out


_STOP_WORDS = set("""a an the of in on at to for from by with and or but as is are was were be been being it its this that
these those he she they his her their our we you i not no nor so than then there here also after before over under into
out up down off about against between during while when where which who whom whose what how why will would shall should
can could may might must has have had do does did said says say told according new one two more most other some any all
per cent year years day days week month today yesterday monday tuesday wednesday thursday friday saturday sunday
january february march april june july august september october november december""".split())


def keywords(text: str, headline: str = "") -> dict[str, float]:
    """Content words (lower-cased, stop words and numbers dropped, light plural stemming) with weights: headline words
    count double, so the topic of the story dominates incidental body vocabulary."""
    out: dict[str, float] = {}
    for src, weight in ((headline, 2.0), (text, 1.0)):
        for w in _WORD.findall((src or "").lower()):
            if len(w) < 3 or w.isdigit() or w in _STOP_WORDS:
                continue
            if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            out[w] = max(out.get(w, 0.0), weight)
    return out


def weighted_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    """Weighted overlap coefficient of two keyword maps, 0..1."""
    if not a or not b:
        return 0.0
    shared = sum(min(a[k], b[k]) for k in a.keys() & b.keys())
    return shared / min(sum(a.values()), sum(b.values()))


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "") if len(n.replace(",", "")) >= 2}


def quotes(text: str) -> set[str]:
    return {" ".join(q.lower().split()) for q in _QUOTE.findall(text or "")}


def shingles(text: str, n: int = 3) -> set[str]:
    w = [x.lower() for x in _WORD.findall(text or "")]
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def overlap_coeff(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def minhash(text: str, n: int = 3) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    for s in shingles(text, n):
        m.update(s.encode("utf8"))
    return m


@dataclass
class PairFeatures:
    cosine: float
    entity_overlap: float
    entity_jaccard: float
    number_overlap: float
    quote_overlap: float
    shingle_jaccard: float
    keyword_overlap: float
    date_gap_days: int
    same_paper: bool
    same_city: bool
    wire_match: bool
    shared_entities: list[str]


def _date(a: dict) -> dt.date:
    return dt.date.fromisoformat(a["date"])


def pair_features(a: dict, b: dict, cosine: float) -> PairFeatures:
    ta = a.get("working_headline", "") + ". " + a.get("working_text", "")
    tb = b.get("working_headline", "") + ". " + b.get("working_text", "")
    ea, eb = proper_nouns(ta), proper_nouns(tb)
    qa, qb = quotes(ta), quotes(tb)
    city_a, city_b = (a.get("city") or "").lower(), (b.get("city") or "").lower()
    wa, wb = a.get("wire_source"), b.get("wire_source")
    return PairFeatures(
        cosine=float(cosine),
        entity_overlap=overlap_coeff(ea, eb),
        entity_jaccard=jaccard(ea, eb),
        number_overlap=overlap_coeff(numbers(ta), numbers(tb)),
        quote_overlap=jaccard(qa, qb) if (qa or qb) else 0.0,
        shingle_jaccard=jaccard(shingles(ta), shingles(tb)),
        keyword_overlap=weighted_overlap(keywords(a.get("working_text", ""), a.get("working_headline", "")),
                                         keywords(b.get("working_text", ""), b.get("working_headline", ""))),
        date_gap_days=abs((_date(a) - _date(b)).days),
        same_paper=a["paper_id"] == b["paper_id"],
        same_city=bool(city_a and city_a == city_b),
        wire_match=bool(wa and wa == wb and city_a and city_a == city_b),
        shared_entities=sorted(ea & eb)[:12],
    )
