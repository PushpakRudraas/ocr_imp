"""Post-OCR text repair for Latin-script lines.

The OCR engines (RapidOCR especially) read the letters of a scanned e-paper line correctly but drop the spaces:
"mustrespectstatutoryappellateroutes", "Design cities keepii mindchildren'sneed". `fix_spacing` puts them back with a word
segmenter, keeping the original capitalisation and leaving anything it is not sure about (names, acronyms) alone.
"""
from __future__ import annotations

import re

try:                                    # optional: without it the text is returned unchanged
    import wordninja
    _COST = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost
except Exception:                       # pragma: no cover
    wordninja, _COST = None, {}

_PUNCT_GLUE = re.compile(r"(?<=[a-z0-9])([.,;:!?])(?=[A-Za-z])")      # "backlog.The" -> "backlog. The"
_CAMEL = re.compile(r"(?<=[a-z]{3})(?=[A-Z][a-z]{2})")                 # "SaveCities" -> "Save Cities"
_RUN = re.compile(r"[A-Za-z']{7,}")                                     # a glued run of letters (stops at dashes, digits, ...)
_MIN_SPLIT = 7                                                          # shorter tokens are assumed to be one word


def is_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) >= 0.9


def _split_token(tok: str) -> str:
    """Split one long glued token into words, preserving case; unchanged if any piece is not a known word."""
    if wordninja is None:
        return tok
    pre = post = ""
    core = tok
    if len(core) < _MIN_SPLIT or core.isupper() or (core[0].isupper() and len(core) < 16):
        return tok                        # short capitalised tokens are names ("Visakhapatnam"), leave them
    letters = core.replace("'", "")
    pieces = wordninja.split(letters.lower())
    merged: list[str] = []
    for p in pieces:                      # a stray letter ("n" of a clipped "India") belongs to its neighbour
        if len(p) == 1 and p not in ("a", "i") and merged:
            merged[-1] += p
        else:
            merged.append(p)
    if len(merged) > 1 and len(merged[0]) == 1 and merged[0] not in ("a", "i"):
        merged[1:2] = [merged[0] + merged[1]]; merged.pop(0)
    pieces = merged
    if len(pieces) < 2:
        return tok
    # a short token must split into real words only (protects names like "Chandigarh"); a very long one is certainly
    # several words, and an OCR slip in one of them ("ndia's") must not stop the rest being separated
    if len(core) < 16 and any(p not in _COST for p in pieces):
        return tok
    if len(core) >= 16 and sum(p not in _COST for p in pieces) > max(1, len(pieces) // 4):
        return tok
    out, i = [], 0
    for p in pieces:                      # re-cut the ORIGINAL string (keeps capitals and apostrophes) on the same lengths
        j, seen = i, 0
        while j < len(core) and seen < len(p):
            if core[j] != "'":
                seen += 1
            j += 1
        while j < len(core) and core[j] == "'" and (j + 1 >= len(core) or core[j + 1].lower() == "s" or seen == len(p)):
            j += 1
        out.append(core[i:j]); i = j
    if i < len(core):
        out[-1] += core[i:]
    return pre + " ".join(out) + post


def fix_spacing(text: str) -> str:
    if not text or wordninja is None or not is_latin(text):
        return text
    t = _PUNCT_GLUE.sub(r"\1 ", text)
    t = " ".join(_CAMEL.sub(" ", w) if len(w) >= 9 else w for w in t.split(" "))
    return " ".join(_RUN.sub(lambda m: _split_token(m.group(0)), w) for w in t.split(" "))
