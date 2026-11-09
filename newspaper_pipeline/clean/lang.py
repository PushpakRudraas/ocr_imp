"""Language detection for newspaper text: Unicode-script share first (reliable for Indic scripts), plus ARGUS's
`looks_english` heuristic for Latin text. A manifest hint disambiguates languages that share a script
(Hindi/Marathi/Nepali all use Devanagari)."""
from __future__ import annotations

import re

SCRIPTS = [  # (name, unicode range start, end, candidate language codes in preference order)
    ("devanagari", 0x0900, 0x097F, ["hi", "mr", "ne"]),
    ("bengali", 0x0980, 0x09FF, ["bn", "as"]),
    ("gurmukhi", 0x0A00, 0x0A7F, ["pa"]),
    ("gujarati", 0x0A80, 0x0AFF, ["gu"]),
    ("oriya", 0x0B00, 0x0B7F, ["or"]),
    ("tamil", 0x0B80, 0x0BFF, ["ta"]),
    ("telugu", 0x0C00, 0x0C7F, ["te"]),
    ("kannada", 0x0C80, 0x0CFF, ["kn"]),
    ("malayalam", 0x0D00, 0x0D7F, ["ml"]),
    ("arabic", 0x0600, 0x06FF, ["ur", "ar"]),
]

# adapted from ARGUS etl-pipeline-svc/etl/translate.py
_EN_STOPWORDS = frozenset(
    "the of and to in is that for it as with was on are by this be from at or an have has had not but they his her "
    "their which will would been were said who also after about more than into over its".split())
_WORD = re.compile(r"[A-Za-z']+")


def looks_english(text: str, min_words: int = 8, stopword_ratio: float = 0.22, ascii_ratio: float = 0.95) -> bool:
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20 or sum(c.isascii() for c in letters) / len(letters) < ascii_ratio:
        return False
    words = _WORD.findall(text.lower())
    if len(words) < min_words:
        return False
    return sum(w in _EN_STOPWORDS for w in words) / len(words) >= stopword_ratio


def script_shares(text: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        cp = ord(ch)
        name = "latin" if ch.isascii() or 0x00C0 <= cp <= 0x024F else "other"
        for sname, lo, hi, _ in SCRIPTS:
            if lo <= cp <= hi:
                name = sname
                break
        counts[name] = counts.get(name, 0) + 1
    return {k: v / total for k, v in counts.items()} if total else {}


def detect_language(text: str, hint: str | None = None) -> tuple[str, float]:
    """Returns (iso_code, confidence 0-1). 'und' when it cannot tell."""
    shares = script_shares(text or "")
    if not shares:
        return "und", 0.0
    script, share = max(shares.items(), key=lambda kv: kv[1])
    if script == "latin":
        if looks_english(text):
            return "en", round(min(1.0, 0.7 + 0.3 * share), 3)
        return ("en", 0.4) if hint == "en" else ("und", round(share * 0.3, 3))
    if script == "other":
        return "und", 0.0
    cands = next(c for n, _, _, c in SCRIPTS if n == script)
    code = hint if hint in cands else cands[0]
    return code, round(share, 3)
