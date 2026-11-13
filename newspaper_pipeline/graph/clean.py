"""Cleaning rules for the relationship graph: what is good enough to become a node.

The stored articles come from OCR, so some fields are unreliable (a "byline" can be a phone number, a "keyword" can be the word
"said"). Every function here is pure: it takes plain data and returns either a clean node or None, so each rule can be tested on
real examples and nothing junk reaches the database.
"""
from __future__ import annotations

import hashlib
import re

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(s: str | None) -> str:
    return _SLUG.sub("_", (s or "").lower()).strip("_")


def collapse(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def short_hash(s: str, n: int = 16) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def norm(s: str | None) -> str:
    """Lower-case, punctuation-free, single-spaced: the form used to compare phrases."""
    return collapse(re.sub(r"[^\w\s]", " ", (s or "").lower(), flags=re.U))


# ------------------------------------------------------------------------------------------------------------ authors
_WIRES = {"pti": "PTI", "ani": "ANI", "ians": "IANS", "uni": "UNI", "afp": "AFP", "ap": "AP", "reuters": "Reuters",
          "bloomberg": "Bloomberg", "tnn": "TNN", "press trust of india": "PTI", "times news network": "TNN",
          "express news service": "Express News Service", "hindustan times": "Hindustan Times"}
_DESK = re.compile(r"\b(reporter|correspondent|correspondents|bureau|news service|news network|network|desk|staff|agency)\b", re.I)
_SYMBOLS = re.compile(r"[\d@/\\()\[\]{}<>_=+*&%$#!?\"“”;:|]")
_NAME_WORD = re.compile(r"^(?:[A-Z][A-Za-z'’\-]*\.?|[A-Z]\.)$")
_NOT_NAMES = {"the", "and", "said", "says", "also", "but", "for", "from", "with", "this", "that", "after", "before", "loans",
              "report", "reports", "news", "india", "indian", "express", "times", "story", "page", "edition", "section",
              "continued", "contd", "photo", "file", "image", "source", "courtesy", "advertisement"}


def _title(word: str) -> str:
    return word if (word.isupper() and len(word) <= 3) else word.capitalize()


def clean_author(raw: str | None) -> dict | None:
    """A real byline -> {"name", "kind"} ("person", or "desk" for "Staff Reporter" / a wire agency); anything else -> None.

    Rejects what OCR commonly puts in the byline slot: numbers, phone numbers, emails, brackets, fragments of a sentence,
    all-lower-case words, single words that are not an agency, and long strings."""
    t = collapse(raw)
    t = re.sub(r"^(?:written\s+by|story\s+by|text\s+by|by|from)\s+", "", t, flags=re.I).strip(" ,.;:|-–—")
    if not t or len(t) > 60:
        return None
    low = t.lower()
    if low in _WIRES:
        return {"name": _WIRES[low], "kind": "desk"}
    if _SYMBOLS.search(t) or "http" in low or "www" in low or not t[0].isupper():
        return None
    words = t.split()
    if _DESK.search(t):                                    # "Staff Reporter", "HT Correspondent", "Political Bureau"
        return {"name": " ".join(_title(w) for w in words), "kind": "desk"} if len(words) <= 5 else None
    if not 2 <= len(words) <= 4:
        return None
    if any(not _NAME_WORD.match(w) or w.lower().strip(".") in _NOT_NAMES for w in words):
        return None
    if not any(len(w.strip(".")) >= 3 for w in words):       # initials alone ("A. K.") are not a name
        return None
    return {"name": " ".join(_title(w) if w.isupper() else w for w in words), "kind": "person"}


def clean_authors(raw: str | None, limit: int = 3) -> list[dict]:
    """A byline can name several people ("Neeraj Mohan and Bhavey Nagpal"): every part that is a real name or desk is kept."""
    t = re.sub(r"^(?:written\s+by|story\s+by|text\s+by|by|from)\s+", "", collapse(raw), flags=re.I)
    out: list[dict] = []
    for part in re.split(r"\s+(?:and|&)\s+|\s*,\s*", t, flags=re.I):
        a = clean_author(part)
        if a and a not in out:
            out.append(a)
    return out[:limit]


# ----------------------------------------------------------------------------------------------------------- keywords
_GENERIC = {"said", "says", "say", "told", "report", "reports", "year", "years", "day", "days", "today", "time", "times", "new",
            "first", "last", "one", "two", "three", "also", "people", "person", "government", "state", "country", "india",
            "indian", "news", "case", "cases", "week", "month", "months", "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday", "official", "officials", "delhi", "city", "area", "place", "part", "number", "many", "several"}


def clean_keywords(phrases: list[str] | None, entity_names: list[str] | None = None, limit: int = 6) -> list[dict]:
    """Key phrases worth a node: not generic words, not just a number, not the same as an entity the article already links to
    (that would be a duplicate node), at most `limit` per article."""
    entity_keys = {norm(n) for n in (entity_names or [])}
    out, seen = [], set()
    for p in phrases or []:
        text = collapse(p)
        key = norm(text)
        if not 3 <= len(key) <= 60 or key.isdigit() or key in seen or key in entity_keys:
            continue
        if all(w in _GENERIC or w.isdigit() for w in key.split()):
            continue
        seen.add(key)
        out.append({"id": key, "text": text})
        if len(out) >= limit:
            break
    return out


# -------------------------------------------------------------------------------------------------------------- events
def clean_event(doc: dict) -> dict | None:
    """One shared Event node per real-world event: only when the event type is specific (not "General.Report") and the article
    names an actor or an institution. The same event reported by several newspapers gets the same id."""
    ev = doc.get("event") or {}
    typ = ev.get("type") or ""
    if not typ or typ.startswith("General"):
        return None
    actor, inst = collapse(ev.get("actor")), collapse(ev.get("institution"))
    if not (actor or inst):
        return None
    place = (doc.get("geo") or {}).get("primary_location") or ""
    key = "|".join([typ, norm(actor), norm(inst), ev.get("date") or "", norm(place)])
    return {"id": short_hash(key), "type": typ, "trigger": ev.get("trigger"), "actor": actor or None,
            "institution": inst or None, "date": ev.get("date"), "location": place or None}


# -------------------------------------------------------------------------------------------------------------- claims
def clean_claims(claims: list[dict] | None, limit: int = 3, max_chars: int = 300) -> list[dict]:
    """The article's check-worthy claims (at most `limit`), trimmed; the same sentence in two papers is one node."""
    out, seen = [], set()
    for c in claims or []:
        text = collapse(c.get("claim"))
        if not c.get("checkworthy") or len(text) < 25:
            continue
        key = short_hash(norm(text))
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": key, "text": text[:max_chars], "status": c.get("verification_status"),
                    "checkworthy": True, "supported_by": [p for p in (c.get("corroborated_by") or []) if p]})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------------------------------------------------- sections
def clean_section(raw: str | None) -> str | None:
    t = collapse(raw)
    return t if 2 <= len(t) <= 40 and not re.search(r"[\d|@/\\]", t) else None
