"""Text cleaning for OCR output.

`clean_text` is adapted from ARGUS microservices/etl-pipeline-svc/etl/clean.py (HTML->text, entity decode,
NFC, invisible/control chars, whitespace). `normalize_ocr_text` adds what newspaper OCR needs on top:
ligatures, de-hyphenation across line breaks, page-furniture removal, glued-punctuation repair and
reflowing column-wrapped lines into paragraphs.
"""
from __future__ import annotations

import html
import re
import statistics
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
               "section", "article"}
_SKIP_TAGS = {"script", "style", "noscript", "iframe"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def clean_text(value) -> str:
    """HTML -> text, entities decoded, unicode NFC, invisible/control chars removed, whitespace tidied."""
    if value is None:
        return ""
    text = str(value)
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text).translate(_ZERO_WIDTH)
    text = _CONTROL.sub("", text)
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


# ----------------------------------------------------------------------------------------- OCR specifics
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st",
             "ﬆ": "st"}
_FURNITURE_LINE = re.compile(
    r"^\W*(?:page\s*)?\d{1,3}\W*$|^\W*(?:advertisement|advt\.?|sponsored|विज्ञापन)\W*$|"
    r"^.{0,60}\|\s*page\s*\d+\s*$|^\W*\(?\s*(?:contd\.?|continued)(?:\s+(?:on|from)\s+(?:page|p\.?)\s*\d+)?\s*\)?\W*$",
    re.I)
_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])[-‐‑]\s*\n\s*(?=[a-zऀ-ॿ])")
_GLUED_AFTER_PUNCT = re.compile(r"(?<=[a-z]{2})([.,;:!?])(?=[A-Z][a-z])")
_GLUED_AFTER_CAPS_COLON = re.compile(r"(?<=[A-Z]{3}):(?=[A-Z][a-z])")
_SENTENCE_END = re.compile(r"[.!?\"”।]$")


def _drop_furniture(lines: list[str]) -> list[str]:
    return [ln for ln in lines if not _FURNITURE_LINE.match(ln.strip())]


def _reflow(lines: list[str]) -> str:
    """Join column-wrapped lines into paragraphs. A line that ends a sentence and is clearly shorter than the
    typical line is treated as the end of a paragraph."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return ""
    typical = statistics.median(len(ln) for ln in lines)
    paras, cur = [], [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        if _SENTENCE_END.search(prev) and len(prev) < 0.6 * typical:
            paras.append(" ".join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    paras.append(" ".join(cur))
    return "\n\n".join(paras)


def normalize_ocr_text(raw: str) -> str:
    text = clean_text(raw)
    for lig, repl in LIGATURES.items():
        text = text.replace(lig, repl)
    text = _HYPHEN_BREAK.sub("", text)                       # "govern-\nment" -> "government"
    text = _GLUED_AFTER_CAPS_COLON.sub(": ", text)           # "CHANDIGARH:Kunwar" -> "CHANDIGARH: Kunwar"
    text = _GLUED_AFTER_PUNCT.sub(r"\1 ", text)              # "Assembly.The" -> "Assembly. The"
    paras = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = _drop_furniture(chunk.split("\n"))
        flowed = _reflow(lines)
        if flowed:
            paras.append(flowed)
    return re.sub(r"[ \t]+", " ", "\n\n".join(paras)).strip()


def clean_headline(raw: str) -> str:
    h = clean_text(raw)
    for lig, repl in LIGATURES.items():
        h = h.replace(lig, repl)
    h = _HYPHEN_BREAK.sub("", h)
    return re.sub(r"\s+", " ", h).strip()
