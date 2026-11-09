"""Field extraction: turn a segmented Article into a flat record (headline, byline, dateline, city, wire,
section, text, confidence, image refs, continuation markers)."""
from __future__ import annotations

import hashlib
import re

from ..input.splitter import PageRecord
from .layout import BYLINE_RE, CONT_FROM_HI_RE, CONT_FROM_RE, reading_order
from .models import BODY, BYLINE, CAPTION, FURNITURE, IMAGE, KICKER, Article, Block

WIRES = {"PTI": "PTI", "ANI": "ANI", "IANS": "IANS", "UNI": "UNI", "REUTERS": "Reuters", "AFP": "AFP", "AP": "AP",
         "एएनआई": "ANI", "पीटीआई": "PTI", "आईएएनएस": "IANS", "भाषा": "PTI-Bhasha"}
# "CHANDIGARH, Nov 2 (ANI): " / "CHANDIGARH: " / "NEW DELHI, NOVEMBER 2: " / "चंडीगढ़, 2 नवंबर (एएनआई): "
DATELINE_RE = re.compile(
    r"^\s*(?P<city>[A-Z][A-Z .'-]{2,30}|[ऀ-ॿ][ऀ-ॿ ]{1,30})"
    r"(?:\s*,\s*(?P<date>[^:()\n]{3,24}?))?"
    r"(?:\s*\((?P<wire>[^)\n]{2,12})\))?\s*:\s*")
CONT_TO_RE = re.compile(
    r"[\(\[]?\s*(?:continued|contd\.?)\s+(?:on|to)\s+(?:page|p\.?)\s*(\d+)\s*[\)\]]?\s*$|"
    r"[\(\[]?\s*शेष\s*पृष्ठ\s*(\d+)\s*पर\s*[\)\]]?\s*$", re.I)
CONTD_HEAD_RE = re.compile(r"\s*[\(\[]\s*(?:contd\.?|continued)\s*[\)\]]\s*$", re.I)
KNOWN_SECTIONS = {"front page", "nation", "business", "sports", "world", "city", "opinion", "editorial", "national",
                  "international", "markets", "entertainment", "states", "metro", "economy", "politics"}


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def page_section(blocks: list[Block]) -> str | None:
    """Section from the running head, e.g. 'Monday, 2 November 2026 | FRONT PAGE | Page 1'."""
    for b in blocks:
        if b.kind != FURNITURE:
            continue
        parts = [collapse(p) for p in b.text.replace("\n", " ").split("|")]
        for p in parts:
            if p.lower() in KNOWN_SECTIONS:
                return p.title()
    return None


def parse_dateline(text: str) -> tuple[dict, str]:
    """Returns ({'dateline','city','wire'}, body_without_dateline). Latin cities must be upper-case so ordinary
    sentences ('Mr. Singh said: ...') are not mistaken for datelines."""
    m = DATELINE_RE.match(text)
    if not m:
        return {}, text
    city = collapse(m.group("city"))
    if len(city.split()) > 3 or (city.isascii() and not city.isupper()):
        return {}, text
    wire_raw = collapse(m.group("wire") or "")
    wire = WIRES.get(wire_raw.upper()) or WIRES.get(wire_raw)
    info = {"dateline": collapse(m.group(0)).rstrip(":").strip(), "city": city.title() if city.isascii() else city}
    if wire:
        info["wire"] = wire
    return info, text[m.end():]


def detect_wire(*texts: str) -> str | None:
    blob = " ".join(texts)
    for key, name in WIRES.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", blob, re.I if key.isascii() else 0):
            return name
    return None


def _pop_continued_to(text: str) -> tuple[str, int | None]:
    m = CONT_TO_RE.search(text.strip())
    if not m:
        return text, None
    return text.strip()[:m.start()].rstrip(), int(m.group(1) or m.group(2))


def make_article_id(paper_id: str, date: str, page_no: int, headline: str) -> str:
    return hashlib.sha1(f"{paper_id}|{date}|{page_no}|{headline}".encode()).hexdigest()[:16]


def build_record(art: Article, page: PageRecord, section: str | None) -> dict:
    h = art.headline_block
    headline = collapse(h.text) if h else ""
    body_blocks = reading_order([b for b in art.blocks if b.kind == BODY])
    byline_blocks = [b for b in art.blocks if b.kind == BYLINE]
    kickers = [b for b in art.blocks if b.kind == KICKER]
    captions = [b for b in art.blocks if b.kind == CAPTION]
    images = [b for b in art.blocks if b.kind == IMAGE]

    byline = collapse(byline_blocks[0].text) if byline_blocks else None
    if byline:
        byline = re.sub(r"^by\s+", "", byline, flags=re.I)

    raw = "\n".join(b.text for b in body_blocks)
    if not byline:      # OCR can fuse the byline into the first body block
        first, _, rest = raw.partition("\n")
        if BYLINE_RE.match(first) and len(first) <= 60:
            byline, raw = re.sub(r"^by\s+", "", collapse(first), flags=re.I), rest
    raw, continued_to = _pop_continued_to(raw)
    dl, raw = parse_dateline(raw)

    continued_from = None
    for k in kickers:
        m = CONT_FROM_RE.search(collapse(k.text)) or CONT_FROM_HI_RE.search(collapse(k.text))
        if m:
            continued_from = int(m.group(1))
    contd_headline = bool(CONTD_HEAD_RE.search(headline))
    headline = CONTD_HEAD_RE.sub("", headline).strip()

    # image refs, pairing each image with the caption just below it
    image_refs = []
    for im in images:
        cap = next((collapse(c.text) for c in captions if -2 <= c.y0 - im.y1 <= 30 and c.x0 - 4 <= im.x1), None)
        image_refs.append({"page_no": im.page_no, "bbox": [round(v, 1) for v in im.bbox], "caption": cap})

    chars = sum(len(b.text) for b in body_blocks) or 1
    conf = sum(b.conf * len(b.text) for b in body_blocks) / chars if body_blocks else (h.conf if h else 0.0)
    if h and h.conf < conf:
        conf = (conf * chars + h.conf * len(h.text)) / (chars + len(h.text))
    flags = list(art.flags)
    if continued_to:
        flags.append("continued_to")
    if continued_from or contd_headline:
        flags.append("is_continuation")

    return {
        "article_id": make_article_id(page.paper_id, page.date, page.page_no, headline or raw[:40]),
        "paper_id": page.paper_id, "paper_name": page.paper_name, "edition": page.edition,
        "language": page.language, "reputation": page.reputation, "date": page.date,
        "page_no": page.page_no, "pages": [page.page_no], "section": section,
        "headline": headline, "byline": byline,
        "dateline": dl.get("dateline"), "city": dl.get("city"),
        "wire_source": dl.get("wire") or detect_wire(byline or "", headline) ,
        "text": raw.strip(),
        "ocr_confidence": round(conf, 4),
        "headline_bbox": [round(v, 1) for v in h.bbox] if h else None,
        "body_bboxes": [[round(v, 1) for v in b.bbox] for b in body_blocks],
        "image_refs": image_refs,
        "continued_to": continued_to, "continued_from": continued_from, "contd_headline": contd_headline,
        "flags": flags,
    }
