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


def keywords(text: str, headline: str = "", stats=None) -> dict[str, float]:
    """Content words (lower-cased, stop words and numbers dropped, light plural stemming) with weights: headline words
    count double, names count 1.5x, and - when `stats` (cluster.keystats.KeywordStats) is given - a word found in most
    articles is scaled down, so event-specific words (a candidate, a constituency) dominate generic ones ("election")."""
    names = proper_nouns(f"{headline}. {text}")
    out: dict[str, float] = {}
    for src, weight in ((headline, 2.0), (text, 1.0)):
        for w in _WORD.findall((src or "").lower()):
            if len(w) < 3 or w.isdigit() or w in _STOP_WORDS:
                continue
            boost = 1.5 if w in names else 1.0
            if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            f = stats.factor(w) if stats is not None else 1.0
            out[w] = max(out.get(w, 0.0), weight * boost * f)
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


# ----------------------------------------------------------------------------------------------- place (step 3)
from ..enrich import lexicons as _L           # noqa: E402  (gazetteer: Indian states and cities)

_FOREIGN = {"pakistan": "PK", "china": "CN", "united states": "US", "america": "US", "britain": "GB", "russia": "RU",
            "ukraine": "UA", "israel": "IL", "bangladesh": "BD", "nepal": "NP", "sri lanka": "LK", "afghanistan": "AF",
            "france": "FR", "germany": "DE", "japan": "JP"}
_WEAK_PLACES = {"delhi", "new delhi"}          # the capital is named in most national stories: never a reason to split two stories
_PLACE_NAMES = sorted({*_L.INDIA_STATES, *_L.INDIA_CITIES, *_FOREIGN}, key=len, reverse=True)
_PLACE_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in _PLACE_NAMES) + r")\b", re.I)


def locations(headline: str, text: str, city: str | None = None) -> dict[str, float]:
    """Places an article is about -> weight. Headline mentions count 3x, the lead 1.5x, the rest 1x; a dateline city adds to
    itself and its state. Keys are lower-case place names (states, cities, foreign countries)."""
    out: dict[str, float] = {}

    def add(name: str, w: float) -> None:
        out[name] = out.get(name, 0.0) + w
        if name in _L.INDIA_CITIES:                       # a city also counts for its state, at half weight
            st = _L.INDIA_CITIES[name][2]
            if st != name:
                out[st] = out.get(st, 0.0) + w * 0.5

    for m in _PLACE_RE.finditer(headline or ""):
        add(m.group(1).lower(), 3.0)
    body = text or ""
    for m in _PLACE_RE.finditer(body[:600]):
        add(m.group(1).lower(), 1.5)
    for m in _PLACE_RE.finditer(body[600:]):
        add(m.group(1).lower(), 1.0)
    if city and city.lower() in _PLACE_NAMES:
        add(city.lower(), 2.0)
    return out


def _strong(loc: dict[str, float], headline_places: set[str]) -> set[str]:
    """Places the article is clearly about (named in the headline, or at least twice): the basis for a conflict."""
    return {p for p, w in loc.items() if p not in _WEAK_PLACES and (p in headline_places or w >= 2.5)}


def location_match(la: dict[str, float], lb: dict[str, float], ha=frozenset(), hb=frozenset()
                   ) -> tuple[float | None, str | None, str]:
    """(score 0..1 or None when either article names no place, conflict note or None, short description).
    Tiers: same city 0.8, same state 0.5, same foreign country 0.2, both place-bearing but nothing shared 0.0. Agreement is weak evidence (many different events happen in one city); disagreement is strong, so it also feeds the veto."""
    if not la or not lb:
        return None, None, "no place named"
    shared = set(la) & set(lb)
    cities = [p for p in shared if p in _L.INDIA_CITIES and p not in _WEAK_PLACES]
    states = [p for p in shared if p in _L.INDIA_STATES and p not in _WEAK_PLACES]
    foreign = [p for p in shared if p in _FOREIGN]
    if cities:
        return 0.8, None, "same city " + cities[0].title()
    if states:
        return 0.5, None, "same state " + states[0].title()
    if foreign:
        return 0.2, None, "same country " + foreign[0].title()
    if shared:                                            # only the capital in common
        return 0.1, None, "only the capital in common"
    sa, sb = _strong(la, ha), _strong(lb, hb)
    if sa and sb:                                         # both clearly about somewhere, and not the same place
        top_a, top_b = max(sa, key=la.get), max(sb, key=lb.get)
        return 0.0, f"different places ({top_a.title()} vs {top_b.title()})", "different places"
    return None, None, "no clear place"                    # incidental mentions only: say nothing either way


_WINNERS = r"bjp|congress|cong|aap|tmc|dmk|ncp|sad|jmm|rjd|bsp|jdu|sp|shiv sena|ysrcp|brs|npp"
_WIN_RE = re.compile(rf"\b({_WINNERS})\b(?:\s+(?!and\b|vs\b|as\b|after\b|but\b|(?:{_WINNERS})\b)[\w-]+){{0,2}}?\s+"
                     r"(?:wins?|won|sweeps?|retains?|storms|romps|bags|clinches|scripts)\b", re.I)
_PARTY_ALIAS = {"cong": "congress"}
_PARTY_RE = re.compile(rf"\b({_WINNERS})\b", re.I)


def parties_in(headline: str) -> set[str]:
    """Every party named anywhere in a headline (so "Cong-JMM sweep" names both Congress and the JMM)."""
    return {_PARTY_ALIAS.get(m.lower(), m.lower()) for m in _PARTY_RE.findall(headline or "")}


def winner(headline: str) -> str | None:
    """Party a headline says won ("BJP wins Haryana", "Congress alliance wins ..."), or None."""
    m = _WIN_RE.search(headline or "")
    if not m:
        return None
    w = m.group(1).lower()
    return _PARTY_ALIAS.get(w, w)


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
    location_score: float | None = None      # None = one side names no place (neutral)
    location_note: str = ""
    location_conflict: str | None = None     # both clearly about different places
    winner_conflict: str | None = None       # headlines name different winning parties


def _date(a: dict) -> dt.date:
    return dt.date.fromisoformat(a["date"])


def pair_features(a: dict, b: dict, cosine: float, stats=None) -> PairFeatures:
    ta = a.get("working_headline", "") + ". " + a.get("working_text", "")
    tb = b.get("working_headline", "") + ". " + b.get("working_text", "")
    ea, eb = proper_nouns(ta), proper_nouns(tb)
    qa, qb = quotes(ta), quotes(tb)
    city_a, city_b = (a.get("city") or "").lower(), (b.get("city") or "").lower()
    wa, wb = a.get("wire_source"), b.get("wire_source")
    ha_text, hb_text = a.get("working_headline", ""), b.get("working_headline", "")
    la = locations(ha_text, a.get("working_text", ""), a.get("city"))
    lb = locations(hb_text, b.get("working_text", ""), b.get("city"))
    head_a, head_b = set(locations(ha_text, "")), set(locations(hb_text, ""))
    loc_score, loc_conflict, loc_note = location_match(la, lb, head_a, head_b)
    win_a, win_b = winner(ha_text), winner(hb_text)
    win_conflict = None
    if win_a and win_b and win_a != win_b and win_a not in parties_in(hb_text) and win_b not in parties_in(ha_text):
        win_conflict = f"different winners ({win_a.upper()} vs {win_b.upper()})"      # an alliance headline names both: no conflict
    return PairFeatures(
        cosine=float(cosine),
        entity_overlap=overlap_coeff(ea, eb),
        entity_jaccard=jaccard(ea, eb),
        number_overlap=overlap_coeff(numbers(ta), numbers(tb)),
        quote_overlap=jaccard(qa, qb) if (qa or qb) else 0.0,
        shingle_jaccard=jaccard(shingles(ta), shingles(tb)),
        keyword_overlap=weighted_overlap(keywords(a.get("working_text", ""), a.get("working_headline", ""), stats),
                                         keywords(b.get("working_text", ""), b.get("working_headline", ""), stats)),
        date_gap_days=abs((_date(a) - _date(b)).days),
        same_paper=a["paper_id"] == b["paper_id"],
        same_city=bool(city_a and city_a == city_b),
        wire_match=bool(wa and wa == wb and city_a and city_a == city_b),
        shared_entities=sorted(ea & eb)[:12],
        location_score=loc_score, location_note=loc_note, location_conflict=loc_conflict, winner_conflict=win_conflict,
    )
