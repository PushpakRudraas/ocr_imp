"""Per-article analyses: classification, sentiment, emotion/tone, credibility, claims, summary, timeline, knowledge
graph, keyphrases and impact. All offline and deterministic; each returns exactly the sub-document used in sample.json."""
from __future__ import annotations

import re
from collections import Counter

from . import lexicons as L
from .geo import geo_scope

_SENT_SPLIT = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bDr\.)(?<!\bRs\.)(?<!\bNo\.)(?<!\bSt\.)(?<!\bvs\.)(?<!\bProf\.)(?<!\bSmt\.)(?<!\bShri\.)(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(" ".join((text or "").split())) if s.strip()]


def _count_terms(text: str, terms) -> int:
    low = text.lower()
    return sum(1 for t in terms if re.search(rf"\b{re.escape(t)}\b", low))


# -------------------------------------------------------------------------------------------- classification
def classify(headline: str, text: str, event: dict, geo: dict, models=None) -> dict:
    scores: Counter[str] = Counter()
    for topic, kws in L.TOPIC_KEYWORDS.items():
        scores[topic] = 3 * _count_terms(headline, kws) + _count_terms(text, kws)
    etype = event["type"]
    boost = {"Politics.": "politics", "Economy.": "business", "Sports.": "sports", "Crime.": "crime",
             "Environment.": "environment", "Disaster.": "disaster", "Judiciary.": "crime", "Social.": "politics",
             "Conflict.": "world"}
    for prefix, topic in boost.items():
        if etype.startswith(prefix):
            scores[topic] += 4
    if etype == "Government.Approval" and scores["infrastructure"]:
        scores["infrastructure"] += 3
    ranked = scores.most_common(2)
    if models is not None:                                   # zero-shot NLI model decides the topic
        topic, p, _ = models.topic(f"{headline}. {text}")
        conf = "high" if p >= 0.6 else "medium" if p >= 0.35 else "low"
    elif not ranked or ranked[0][1] == 0:
        topic, conf = "misc", "low"
    else:
        topic = ranked[0][0]
        margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)
        conf = "high" if margin >= 4 else "medium" if margin >= 2 else "low"
    suggested, code = L.TOPIC_TO_DOMAIN.get(topic, ("misc", "misc"))
    return {"topic": topic, "subtopic": L.SUBTOPIC_BY_EVENT.get(etype, topic),
            "current_domain_field": "misc", "suggested_domain": suggested, "argus_domain_code": code,
            "geo_scope": geo_scope(geo, text), "topic_confidence": conf}


# -------------------------------------------------------------------------------------------- sentiment / emotion
def _polarity(text: str) -> tuple[int, int, list[str]]:
    words = [w.lower() for w in _WORD.findall(text)]
    pos = sum(w in L.POS_WORDS for w in words)
    neg_words = [w for w in words if w in L.NEG_WORDS]
    return pos, len(neg_words), neg_words


def sentiment(headline: str, text: str, entities: list[dict], models=None) -> dict:
    if models is not None:
        return _model_sentiment(headline, text, entities, models)
    pos, neg, _ = _polarity(f"{headline}. {text}")
    score = round((pos - neg) / (pos + neg + 4), 3)
    label = "positive" if score >= 0.2 else "negative" if score <= -0.2 else "neutral"
    ents = []
    sents = sentences(f"{headline}. {text}")
    for e in [x for x in entities if x["type"] in ("ORG", "PERSON")][:3]:
        names = {e["text"].lower(), e.get("canonical", "").lower()} - {""}
        names |= {k.lower() for k, v in L.KNOWN_ORGS.items() if v == e["text"]}
        hit = [s for s in sents if any(re.search(rf"\b{re.escape(n)}\b", s.lower()) for n in names)]
        p = n_ = 0
        negw: list[str] = []
        for s in hit:
            sp, sn, nw = _polarity(s)
            p += sp; n_ += sn; negw += nw
        if n_ > p and n_ >= 2:
            ents.append({"entity": e["text"], "stance": "negative", "reason": "negative terms in coverage: " + ", ".join(sorted(set(negw))[:4])})
        elif p > n_ and p >= 2:
            ents.append({"entity": e["text"], "stance": "positive", "reason": "positive terms in coverage"})
        else:
            ents.append({"entity": e["text"], "stance": "neutral"})
    return {"document": {"label": label, "score": score}, "entity_level": ents}


def _model_sentiment(headline: str, text: str, entities: list[dict], models) -> dict:
    """Document sentiment from the model; per-entity stance from the model run on the sentences naming that entity."""
    label, score = models.sentiment(f"{headline}. {text}")
    sents = sentences(f"{headline}. {text}")
    ents = []
    for e in [x for x in entities if x["type"] in ("ORG", "PERSON")][:3]:
        names = {e["text"].lower(), e.get("canonical", "").lower()} - {""}
        hit = [s for s in sents if any(re.search(rf"\b{re.escape(n)}\b", s.lower()) for n in names)]
        if not hit:
            ents.append({"entity": e["text"], "stance": "neutral"})
            continue
        lab, sc = models.sentiment(" ".join(hit))
        stance = lab if abs(sc) >= 0.3 else "neutral"
        item = {"entity": e["text"], "stance": stance}
        if stance != "neutral":
            item["reason"] = f"sentiment model score {sc:+.2f} over {len(hit)} sentence(s) naming the entity"
        ents.append(item)
    return {"document": {"label": label, "score": score}, "entity_level": ents}


def emotion_tone(headline: str, text: str, event: dict, has_dateline: bool, sentiment_label: str, models=None) -> dict:
    if models is not None:
        emo, p = models.emotion(f"{headline}. {text}")
        dominant = emo if p >= 0.4 else "neutral"
    else:
        words = {w.lower() for w in _WORD.findall(f"{headline}. {text}")}
        counts = {emo: len(words & ws) for emo, ws in L.EMOTION_WORDS.items()}
        emo, n = max(counts.items(), key=lambda kv: kv[1])
        dominant = emo if n >= 2 else "neutral"
    doc = f"{headline} {text}"
    if dominant == "neutral" and has_dateline:
        tone = "factual / hard news"
    elif re.search(r"\b(analysts|observers|experts) said\b", doc, re.I):
        tone = "factual with analysis"
    else:
        tone = "factual / hard news" if dominant == "neutral" else f"{dominant}-laden reporting"
    sev = L.EVENT_SEVERITY.get(event["type"], 0.2)
    urgent = _count_terms(doc, L.BREAKING_TERMS) > 0 or _count_terms(doc, L.CASUALTY_TERMS) > 0
    urgency = "high" if urgent or sev >= 0.8 else "medium" if sev >= 0.5 else "low"
    return {"dominant": dominant, "tone": tone, "urgency": urgency}


# -------------------------------------------------------------------------------------------- credibility
def credibility(rec: dict, headline: str, text: str) -> dict:
    caps = sum(c.isupper() for c in headline if c.isalpha()) / max(1, sum(c.isalpha() for c in headline))
    bait = sum(bool(re.search(p, headline, re.I)) for p in L.CLICKBAIT_PATTERNS) + (headline.count("!") > 0) + (caps > 0.6)
    prop = [t for t in L.PROPAGANDA_TERMS if t in f"{headline} {text}".lower()]
    return {"source": rec.get("paper_name"), "source_type": "newspaper_epaper",
            "source_reputation": rec.get("reputation", "not_rated"), "wire_backed": bool(rec.get("wire_source")),
            "clickbait_score": "high" if bait >= 2 else "medium" if bait == 1 else "low",
            "propaganda_indicators": prop if prop else "none detected in text",
            "bias_label": "not_assessable_from_single_article"}


# -------------------------------------------------------------------------------------------- claims
_REPORTING = re.compile(r"\b(said|says|announced|submitted|stated|told|confirmed|voted|demanded|cited|retained|kept|held)\b", re.I)
_OPINION = re.compile(r"\b(should|must|believe|feel|hope|likely to|expected to|may|might|could)\b", re.I)


def extract_claims(headline: str, text: str, entities: list[dict], max_claims: int = 4) -> list[dict]:
    head_words = {w.lower() for w in _WORD.findall(headline) if len(w) > 3}
    names = [e["text"] for e in entities if e["type"] in ("PERSON", "ORG", "GPE")]
    scored = []
    for i, s in enumerate(sentences(text)):
        if len(s) < 25:
            continue
        score = 0
        has_num = bool(re.search(r"\d", s))
        has_ord = bool(re.search(r"\b(first|second|third|fourth|fifth)\s+(?!(?:to|and|or|in|of|on|the|a|at|before|then|but)\b)[A-Za-z]+", s, re.I))
        score += 2 * has_num + 2 * has_ord
        score += min(2, sum(n.lower() in s.lower() for n in names))
        score += bool(_REPORTING.search(s))
        score += bool(head_words & {w.lower() for w in _WORD.findall(s)})
        score -= 2 * bool(_OPINION.search(s))
        score += 1 if i == 0 else 0
        scored.append((score, i, s, has_num or has_ord))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for score, i, s, numeric in scored[:max_claims]:
        out.append({"claim": s if len(s) <= 240 else s[:237].rsplit(" ", 1)[0] + "...",
                    "checkworthy": score >= 3, "verification_status": "unverified",
                    "next_step": ("verify the figure/count against other outlets or the official source" if numeric
                                  else "cross-check against other outlets or the official statement")})
    return out


# -------------------------------------------------------------------------------------------- summary
def one_line_summary(headline: str, text: str, entities: list[dict], max_chars: int = 240) -> str:
    sents = sentences(text)[:4]
    if not sents:
        return headline
    head_words = {w.lower() for w in _WORD.findall(headline) if len(w) > 3}
    names = [e["text"].lower() for e in entities if e["type"] in ("PERSON", "ORG", "GPE")]

    def score(i_s):
        i, s = i_s
        sw = {w.lower() for w in _WORD.findall(s)}
        return (len(head_words & sw) + sum(n in s.lower() for n in names) - 0.7 * i, -i)
    best = max(enumerate(sents), key=score)[1]
    if len(best) > max_chars:
        best = best[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return best if best.endswith((".", "!", "?")) else best + "."


def timeline_entry(event: dict, headline: str, rec: dict) -> dict:
    return {"date": event["date"], "event": headline, "source": rec.get("paper_name"),
            "page": rec.get("page_no")}


# -------------------------------------------------------------------------------------------- keyphrases
_EVENT_NOUN = {"Politics.Resignation": "resignation", "Politics.Election": "election", "Economy.MonetaryPolicy": "policy decision",
               "Environment.AirQuality": "air quality", "Government.Approval": "approval", "Sports.Result": "result",
               "Crime.Arrest": "arrest", "Social.Protest": "protest", "Conflict.Fatality": "casualties"}


def keyphrases(headline: str, text: str, entities: list[dict], event: dict, max_n: int = 6) -> list[str]:
    out: list[str] = []

    def add(p):
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    noun = _EVENT_NOUN.get(event["type"])
    if event.get("institution") and noun:
        add(f"{event['institution']} {noun}")
    parties = [e for e in entities if e["type"] == "ORG" and e.get("subtype") == "political party"]
    if parties and event["type"] == "Politics.Resignation":
        short = next((k for k, v in L.KNOWN_ORGS.items() if v == parties[0]["text"]), parties[0]["text"])
        add(f"{short} defections")
    for e in entities:
        if e["type"] in ("PERSON", "ORG", "GPE"):
            add(e["text"])
    try:
        from sklearn.feature_extraction.text import CountVectorizer
        cv = CountVectorizer(ngram_range=(2, 2), stop_words="english", token_pattern=r"(?u)\b[A-Za-z][A-Za-z'-]{2,}\b")
        m = cv.fit_transform([f"{headline}. {text}"])
        terms = cv.get_feature_names_out()
        top = sorted(zip(terms, m.toarray()[0]), key=lambda t: (-t[1], t[0]))
        have = " ".join(out).lower()
        for term, c in top[:12]:
            if c >= 2 and not all(w in have for w in term.split()):
                add(term)
    except Exception:
        pass
    return out[:max_n]


# -------------------------------------------------------------------------------------------- knowledge graph
# relations whose object is a value ("6.5 per cent", "3 MLAs"), not an entity, stay in `relations` but are not graph edges
_REL_NAME = {"resigned_from": "RESIGNED_FROM", "former_member_of": "ASSOCIATED_WITH", "governor_of": "HEADS",
             "holds_rate_at": None, "has_defections_count": None}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def knowledge_graph(entities: list[dict], event: dict, relations: list[list[str]]) -> dict:
    label_of = {"PERSON": "Person", "GPE": "Location", "ORG": "Organization"}
    nodes, ids = [], {}
    for e in entities:
        if e["type"] not in label_of:
            continue
        label = "PoliticalParty" if e.get("subtype") == "political party" else label_of[e["type"]]
        ids[e["text"].lower()] = _slug(e["text"])
        nodes.append({"id": _slug(e["text"]), "label": label})
    def node_for(name):
        k = name.lower()
        if k not in ids:
            ids[k] = _slug(name)
            nodes.append({"id": ids[k], "label": "Organization"})
        return ids[k]
    edges = []
    for a, rel, b in relations:
        r = _REL_NAME.get(rel, rel.upper())
        if not r:
            continue
        edge = {"from": node_for(a), "rel": r, "to": node_for(b)}
        if r == "RESIGNED_FROM":
            edge["date"] = event["date"]
        edges.append(edge)
    for e in entities:                                   # "Punjab Assembly" LOCATED_IN "Punjab"
        if e["type"] == "ORG":
            for g in entities:
                if g["type"] == "GPE" and e["text"].lower().startswith(g["text"].lower() + " "):
                    edges.append({"from": ids[e["text"].lower()], "rel": "LOCATED_IN", "to": ids[g["text"].lower()]})
    return {"nodes": nodes, "edges": edges}


# -------------------------------------------------------------------------------------------- impact
_GEO_SCOPE_VAL = {"local": 0.25, "state": 0.4, "national": 0.6, "international": 0.85, "unknown": 0.3}
_REP_VAL = {"high": 0.7, "medium": 0.5, "low": 0.3, "not_rated": 0.4}


def impact_score(event: dict, scope: str, entities: list[dict], text: str, rec: dict, n_papers: int = 1) -> dict:
    sev = L.EVENT_SEVERITY.get(event["type"], 0.2)
    sev = min(1.0, sev + 0.1 * min(2, _count_terms(text, L.CASUALTY_TERMS)) + 0.05 * min(2, _count_terms(text, L.CONFLICT_TERMS)))
    geo_v = _GEO_SCOPE_VAL.get(scope, 0.3)
    people_orgs = [e for e in entities if e["type"] in ("PERSON", "ORG")]
    roles = _count_terms(text, ["minister", "governor", "mla", "chief minister", "speaker", "president", "court", "bank"])
    ent_v = min(1.0, 0.5 * min(1.0, len(people_orgs) / 6) + 0.5 * min(1.0, roles / 3))
    reach = min(1.0, _REP_VAL.get(rec.get("reputation", "not_rated"), 0.4) + (0.1 if rec.get("wire_source") else 0.0)
                + 0.1 * (n_papers - 1))
    value = round(0.35 * sev + 0.20 * geo_v + 0.20 * ent_v + 0.25 * reach, 2)
    lvl = lambda v: "high" if v >= 0.7 else "medium" if v >= 0.4 else "low"
    return {"value": value, "scale": "0-1", "basis": "heuristic, not a trained model",
            "components": {
                "event_severity": f"{lvl(sev)} ({event['type']})",
                "geo_scope": scope,
                "entity_importance": f"{lvl(ent_v)} ({len(people_orgs)} people/orgs, {roles} role mentions)",
                "source_reach": f"{lvl(reach)} ({rec.get('paper_name')}, {n_papers} paper{'s' if n_papers != 1 else ''})"}}


def forecast_signal(event_type: str) -> str:
    return L.FORECAST_BY_EVENT.get(event_type, L.FORECAST_BY_EVENT["General.Report"])
