"""Rule-based entity extraction: PERSON / ORG / GPE / CARDINAL_GROUP, with acronym canonicalisation and mention
merging. Entity *linking* is left unresolved (kb_id null) exactly as in sample.json; plug Wikidata/ReFinED in later.
Country resolution is adapted from ARGUS enrichment-svc/countries.py."""
from __future__ import annotations

import re
from collections import Counter

import pycountry

from . import lexicons as L

_ALIASES = {"usa": "US", "united states": "US", "america": "US", "uk": "GB", "britain": "GB", "uae": "AE",
            "russia": "RU", "iran": "IR", "syria": "SY", "vietnam": "VN", "south korea": "KR", "north korea": "KP"}
_BY_NAME = {}
for _c in pycountry.countries:
    _BY_NAME[_c.name.casefold()] = _c.alpha_2
    if hasattr(_c, "common_name"):
        _BY_NAME[_c.common_name.casefold()] = _c.alpha_2


def resolve_country(name: str) -> str | None:
    key = (name or "").strip().casefold()
    return _ALIASES.get(key) or _BY_NAME.get(key)


_WORDTOK = r"(?:[A-Z][A-Za-z'’-]*|[A-Z]{2,})"
_JOINERS = r"(?:\s+(?:of|for|de|bin)\s+(?=[A-Z])|\s+)"
_PHRASE = re.compile(rf"\bJammu and Kashmir\b|\bAndaman and Nicobar\b|\b{_WORDTOK}(?:{_JOINERS}{_WORDTOK})*")
_HONOR_RE = re.compile(
    r"\b(?P<h>(?:Chief Minister|Prime Minister|Mr|Mrs|Ms|Dr|Shri|Smt|Sri|Prof|Justice|Captain|Capt|General|Col|"
    r"Minister|Governor|Speaker|President|MLA|MP|Mayor|Commissioner|Coach|Skipper)\.?)\s+$")
_NUM_GROUP = re.compile(r"\b(?P<n>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,4})\s+"
                        r"(?P<unit>MLAs?|legislators|ministers|MPs|people|workers|farmers|students|protesters|soldiers|"
                        r"arrests|deaths|schools|councillors|leaders)\b", re.I)
_ORDINAL_GROUP = re.compile(r"\b(?P<n>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+"
                            r"(?:\w+\s+){0,2}?(?P<unit>MLA|legislator|minister|MP|councillor|leader)s?\b", re.I)
_LEADING = re.compile(r"^(?:the|a|an|in|on|at|by|after|from|with|for|as|but|and|its|his|her|their)\s+", re.I)


_TRAIL_STOP = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
               "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
               "nov", "oct", "said", "he", "she", "it", "they", "after", "by", "for", "with", "as", "from", "but"}
_ROLE_TOKENS = {"chief minister": "Chief_Minister", "prime minister": "Prime_Minister"}


def _strip_stop(words: list[str]) -> list[str]:
    while words and words[0].lower().strip(".") in L.STOP_CAPS:
        words = words[1:]
    while words and words[-1].lower().strip(".") in _TRAIL_STOP:
        words = words[:-1]
    return words


def _pieces(phrase: str) -> list[tuple[list[str], bool]]:
    """Split a capitalised phrase at honorific/role words ('Punjab MLA Kunwar Singh' -> [Punjab], [Kunwar Singh]+honor)."""
    for k, v in _ROLE_TOKENS.items():
        phrase = re.sub(k, v, phrase, flags=re.I)
    out: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    honor_next = False
    for w in phrase.split():
        base = w.lower().strip(".").replace("_", " ")
        if base in L.HONORIFICS:
            if cur:
                out.append((cur, honor_next))
            cur, honor_next = [], True
        else:
            cur.append(w)
    if cur:
        out.append((cur, honor_next))
    return out


def _is_gpe(name: str) -> bool:
    k = name.lower()
    return k in L.INDIA_STATES or k in L.INDIA_CITIES or resolve_country(name) is not None


def _classify(name: str, honorific: bool) -> str | None:
    words = name.split()
    last = words[-1].lower().strip(".")
    low = name.lower()
    if name in L.ORG_ACRONYMS or low in L.PARTY_NAMES:
        return "ORG"
    if _is_gpe(name) and not (len(words) > 1 and last in L.ORG_SUFFIXES):
        return "GPE"
    if len(words) == 1 and last in L.ORG_SUFFIXES:
        return None                       # a bare "Assembly" / "Party" is a common noun here
    if last in L.ORG_SUFFIXES or any(w.lower() in {"bank", "party", "assembly", "committee"} for w in words[1:]):
        return "ORG"
    if honorific and not _is_gpe(name):
        return "PERSON"
    if 2 <= len(words) <= 4 and all(w[0].isupper() and not w.isupper() for w in words):
        return "PERSON"
    return None


def _quantities(text: str) -> list[dict]:
    out, seen = [], set()
    for m in _NUM_GROUP.finditer(text):
        n = m.group("n").lower()
        value = int(n) if n.isdigit() else L.NUMBER_WORDS[n]
        unit = L.GROUP_NOUNS.get(m.group("unit").lower(), m.group("unit"))
        key = (value, unit)
        if key not in seen:
            seen.add(key)
            out.append({"text": f"{m.group('n')} {m.group('unit')}", "type": "CARDINAL_GROUP", "value": value, "unit": unit})
    for m in _ORDINAL_GROUP.finditer(text):
        value = L.NUMBER_WORDS[m.group("n").lower()]
        unit = L.GROUP_NOUNS.get(m.group("unit").lower() + "s", m.group("unit") + "s")
        key = (value, unit)
        if key not in seen:
            seen.add(key)
            out.append({"text": f"{m.group('n')} {m.group('unit')}", "type": "CARDINAL_GROUP", "value": value,
                        "unit": unit, "ordinal": True})
    return out


def extract_entities(headline: str, text: str, max_entities: int = 14, models=None) -> list[dict]:
    """Entities in sample.json shape. The headline counts three times so the story's subject ranks first.
    With `models` (enrich.models.HfModels) mentions come from the NER model; otherwise from the capitalisation rules."""
    doc = f"{headline}. {text}"
    counts: Counter[str] = Counter()
    kinds: dict[str, str] = {}
    honorific_seen: set[str] = set()
    first_pos: dict[str, int] = {}
    head_len = len(headline) + 2

    if models is not None:
        for part, w in ((headline, 3), (text, 1)):
            for mn in models.ner(part):
                name = " ".join(mn.text.split())
                if name.lower() in L.STOP_CAPS:
                    continue
                kind = mn.kind
                if name.lower() in L.PARTY_NAMES or name in L.ORG_ACRONYMS or name in L.KNOWN_ORGS:
                    kind = "ORG"                              # NER types "Congress" / "BJP" as places or people
                counts[name] += w
                kinds.setdefault(name, kind)
                first_pos.setdefault(name, doc.find(name) if doc.find(name) >= 0 else len(doc))
        for short in [n for n, k in kinds.items() if k == "ORG"]:        # "Assembly" is part of "Punjab Assembly"
            longer = [n for n, k in kinds.items() if k == "ORG" and n != short and f" {short} " in f" {n} "]
            if longer and short in kinds:
                counts[max(longer, key=lambda n: (len(n), counts[n]))] += counts.pop(short)
                kinds.pop(short)
    else:
        for m in _PHRASE.finditer(doc):
            start = m.start()
            sent_start = start == 0 or re.search(r"[.!?:]\s*$", doc[max(0, start - 3):start]) is not None
            pre_honor = bool(_HONOR_RE.search(doc[max(0, start - 18):start]))
            w = 3 if start < head_len else 1
            for idx, (words, honor) in enumerate(_pieces(m.group(0))):
                words = _strip_stop(words)
                if not words:
                    continue
                honor = honor or (idx == 0 and pre_honor)
                name = " ".join(words)
                if name.isupper() and name not in L.ORG_ACRONYMS and (len(name) > 5 or _is_gpe(name.title())):
                    name = name.title()                      # datelines: CHANDIGARH -> Chandigarh
                if len(words) == 1 and idx == 0 and sent_start and not honor and name not in L.ORG_ACRONYMS and not _is_gpe(name):
                    continue                                  # sentence opener, not a name
                if name.lower() in L.STOP_CAPS or len(name) < 2:
                    continue
                kind = _classify(name, honor)
                if not kind:
                    continue
                counts[name] += w
                kinds.setdefault(name, kind)
                first_pos.setdefault(name, start)

    # fold shorter PERSON mentions ("Singh", "Vijay Pratap Singh") into the longest name that contains them
    persons = [n for n, k in kinds.items() if k == "PERSON"]
    for short in sorted(persons, key=len):
        longer = [n for n in persons if n != short and n in kinds and f" {short} " in f" {n} "]
        if longer:
            target = max(longer, key=lambda n: (len(n), counts[n]))
            counts[target] += counts.pop(short)
            kinds.pop(short)
    # fold acronyms and expansions: AAP == Aam Aadmi Party
    for ac, full in L.KNOWN_ORGS.items():
        if ac in kinds and full in kinds:
            counts[full] += counts.pop(ac); kinds.pop(ac)
    # a person name contained in an org/GPE ("Punjab" in "Punjab Assembly") stays separate; drop only exact dupes
    names = sorted(counts, key=lambda n: (-counts[n], first_pos[n]))[:max_entities]

    ents: list[dict] = []
    for n in names:
        k = kinds[n]
        e: dict = {"text": n, "type": k, "mentions": counts[n]}
        if k == "ORG":
            e["subtype"] = ("political party" if n.lower() in L.PARTY_NAMES else
                            "legislature" if n.split()[-1].lower() in {"assembly", "parliament", "sabha"} else
                            "central bank" if "bank" in n.lower() else "organisation")
            if n in L.KNOWN_ORGS:
                e["canonical"] = L.KNOWN_ORGS[n]
        elif k == "GPE":
            low = n.lower()
            iso = "IN" if low in L.INDIA_STATES or low in L.INDIA_CITIES else resolve_country(n)
            e["subtype"] = ("Indian state" if low in L.INDIA_STATES and low != "chandigarh" else
                            "Indian city" if low in L.INDIA_CITIES else "country" if iso else "location")
            if iso:
                e["country_iso"] = iso
        e["kb_id"] = None
        ents.append(e)
    # role of the first PERSON mentioned in the headline
    for e in ents:
        if e["type"] == "PERSON":
            e["role"] = "subject"
            break
    ents.extend({**q, "kb_id": None} for q in _quantities(doc))
    return ents


def entity_names(ents: list[dict], *types: str) -> list[str]:
    return [e["text"] for e in ents if e["type"] in types]
