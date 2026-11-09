"""Event extraction (type / trigger / actor / institution / date) and relation triples from entities + text."""
from __future__ import annotations

import datetime as dt
import re

from . import lexicons as L
from .entities import entity_names

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_INSTITUTION_AFTER = re.compile(
    r"\b(?:from|quits?|leaves?|left|resigned from|resigns from|steps? down from)\s+(?:the\s+)?"
    r"((?:[A-Z][A-Za-z]+\s+){0,3}(?:Assembly|Parliament|Sabha|Council|Ministry|Board|Committee|Commission|Party))")
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:per cent|%|percent)")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATELINE_DATE = re.compile(r"\b(?:(\d{1,2})\s+([A-Za-z]{3})[a-z]*|([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}))\b")


def find_trigger(headline: str, text: str) -> tuple[str, str, str, bool]:
    """(event_type, trigger_lemma, matched_word, found_in_headline)."""
    lead = " ".join(re.split(r"(?<=[.!?])\s+", text)[:2])
    for source, in_head in ((headline, True), (lead, False)):
        for pattern, etype, lemma in L.EVENT_TRIGGERS:
            m = re.search(pattern, source, re.I)
            if m:
                return etype, lemma, m.group(0).lower(), in_head
    return "General.Report", "reports", "", False


def resolve_event_date(text: str, pub_date: dt.date, dateline: str | None = None) -> dt.date:
    """'on Monday' -> the latest such weekday on or before the publication date; an explicit 'Nov 2' dateline wins."""
    if dateline:
        m = _DATELINE_DATE.search(dateline)
        if m:
            day, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            month = _MONTHS.get(mon.lower()[:3])
            if month:
                try:
                    d = dt.date(pub_date.year, month, int(day))
                    if d <= pub_date + dt.timedelta(days=1):
                        return d
                except ValueError:
                    pass
    low = text.lower()
    for name in _WEEKDAYS:
        if re.search(rf"\b(?:on\s+)?{name}\b", low):
            back = (pub_date.weekday() - _WEEKDAYS.index(name)) % 7
            return pub_date - dt.timedelta(days=back)
    if re.search(r"\byesterday\b", low):
        return pub_date - dt.timedelta(days=1)
    return pub_date


def extract_event(headline: str, text: str, entities: list[dict], pub_date: dt.date,
                  dateline: str | None = None) -> tuple[dict, list[list[str]], str]:
    etype, lemma, word, in_head = find_trigger(headline, text)
    doc = f"{headline}. {text}"
    persons = entity_names(entities, "PERSON")
    orgs = entity_names(entities, "ORG")
    gpes = entity_names(entities, "GPE")
    parties = [e["text"] for e in entities if e["type"] == "ORG" and e.get("subtype") == "political party"]

    in_headline = lambda n: n.lower() in headline.lower()
    actor = next((n for n in persons if in_headline(n)), persons[0] if persons else None)
    if etype == "Economy.MonetaryPolicy":
        actor = next((o for o in orgs if "bank" in o.lower() or o in L.KNOWN_ORGS), actor)

    institution = None
    m = _INSTITUTION_AFTER.search(doc)
    if m:
        institution = " ".join(m.group(1).split())
        match = next((o for o in orgs if o.lower().endswith(institution.lower().split()[-1]) and institution.lower() in o.lower()), None)
        if match is None and gpes:
            # "from the Punjab Assembly" - prefer an entity already extracted ("Punjab Assembly")
            match = next((o for o in orgs if any(institution.lower().startswith(g.lower()) for g in gpes) and o.lower() in institution.lower()), None)
        institution = match or institution
        if institution.lower().startswith("the "):
            institution = institution[4:]
    if institution is None:
        non_party = [o for o in orgs if o not in parties and o not in L.KNOWN_ORGS.values() or "bank" in o.lower()]
        institution = non_party[0] if non_party else (orgs[0] if orgs else None)
    # a bare "Assembly" in the text + a state in the headline -> "<State> Assembly"
    if institution and institution.lower() in {"assembly", "parliament", "council"} and gpes:
        institution = f"{gpes[0]} {institution}"

    date = resolve_event_date(text, pub_date, dateline)
    confidence = ("high" if in_head and (actor or institution) else
                  "medium" if etype != "General.Report" else "low")
    event = {"type": etype, "trigger": lemma, "actor": actor, "institution": institution,
             "date": date.isoformat(), "confidence": confidence}

    relations: list[list[str]] = []
    notes: list[str] = []
    ordinals = [e for e in entities if e["type"] == "CARDINAL_GROUP" and e.get("ordinal")]
    if etype == "Politics.Resignation":
        if actor and institution:
            relations.append([actor, "resigned_from", institution])
        party = parties[0] if parties else None
        left_party = party and re.search(rf"(?:leave|left|quit|desert|exit)\w*\s+(?:the\s+)?(?:{re.escape(party)}|"
                                         rf"{re.escape(next((k for k, v in L.KNOWN_ORGS.items() if v == party), party))})", doc, re.I)
        if actor and party:
            relations.append([actor, "former_member_of", party])
            notes.append("party link " + ("stated in the text" if left_party else "inferred from context; confirm in the full article"))
        if party and ordinals:
            o = ordinals[0]
            relations.append([party, "has_defections_count", f"{o['value']} {o['unit']}"])
            notes.append("defection count derived from the ordinal ('third'); definitions vary by outlet")
    elif etype == "Economy.MonetaryPolicy":
        rate = _RATE.search(doc)
        if actor and rate:
            relations.append([actor, "holds_rate_at", f"{rate.group(1)} per cent"])
        for p in persons:
            if re.search(rf"(?:Governor)\s+{re.escape(p)}", doc):
                relations.append([p, "governor_of", actor or "central bank"])
    return event, relations, "; ".join(notes)
