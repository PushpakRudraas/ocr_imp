"""Layout analysis for one page: classify blocks, then group them into articles.

Heuristic and model-free (works from block geometry + font size), which is what digital e-papers need.
For scanned pages with hard layouts, swap `classify_blocks` for a detector such as DocLayout-YOLO that
sets `Block.kind`; `segment_articles` only relies on kinds + geometry.
"""
from __future__ import annotations

import re

from ..input.splitter import PageRecord
from .backends import _weighted_median
from .models import BODY, BYLINE, CAPTION, FURNITURE, HEADLINE, IMAGE, KICKER, Article, Block

BYLINE_RE = re.compile(
    r"^\s*(by\s+\S|(?-i:By[A-Z])|from our\b|special correspondent|staff (reporter|writer|correspondent)|express news service|"
    r"\btnn\b|\bpti\b|\bani\b|\bians\b|संवाददाता|ब्यूरो|विशेष संवाददाता|भाषा)", re.I)
CONT_FROM_RE = re.compile(r"(?:continued|contd\.?)\s+from\s+(?:page|p\.?)\s*(\d+)", re.I)
CONT_FROM_HI_RE = re.compile(r"पृष्ठ\s*(\d+)\s*का\s*शेष")
PAGE_NUM_RE = re.compile(r"^\W*(page\s*)?\d{1,3}\W*$", re.I)


def body_font_size(blocks: list[Block]) -> float:
    sizes, weights = [], []
    for b in blocks:
        if b.kind == IMAGE or b.font_size <= 0:
            continue
        n = len(b.text.replace("\n", ""))
        if n >= 40:
            sizes.append(b.font_size)
            weights.append(n)
    return _weighted_median(sizes, weights) if sizes else 9.0


def _x_overlap(a: Block, b: Block, tol: float = 4.0) -> bool:
    return a.x0 - tol <= b.x1 and b.x0 - tol <= a.x1


def classify_blocks(blocks: list[Block], page: PageRecord, top_frac: float = 0.07, bottom_frac: float = 0.04,
                    headline_ratio: float = 1.35) -> float:
    """Sets Block.kind in place; returns the body font size used."""
    body = body_font_size(blocks)
    top_y, bottom_y = page.height * top_frac, page.height * (1 - bottom_frac)

    # pass 1: furniture, kickers, headlines
    for b in blocks:
        if b.kind == IMAGE:
            continue
        flat = " ".join(b.text.split())
        if (b.y1 <= top_y or b.y0 >= bottom_y) and b.n_lines <= 2:
            b.kind = FURNITURE
        elif PAGE_NUM_RE.match(flat):
            b.kind = FURNITURE
        elif b.n_lines <= 2 and (CONT_FROM_RE.search(flat) or CONT_FROM_HI_RE.search(flat)) and len(flat) < 80:
            b.kind = KICKER
        elif b.font_size >= body * headline_ratio or (
                b.bold and b.font_size >= body * 1.15 and b.n_lines <= 4 and len(flat) < 200):
            b.kind = HEADLINE
        else:
            b.kind = BODY

    headlines = [b for b in blocks if b.kind == HEADLINE]
    images = [b for b in blocks if b.kind == IMAGE]

    # pass 2: bylines and captions need context
    for b in blocks:
        if b.kind != BODY:
            continue
        flat = " ".join(b.text.split())
        short = len(flat) <= 80 and b.n_lines <= 2
        under_headline = any(0 <= b.y0 - h.y1 <= 30 and _x_overlap(b, h) for h in headlines)
        if short and b.font_size <= body * 1.1 and (BYLINE_RE.match(flat) or under_headline):
            b.kind = BYLINE
            continue
        near_image = any(-2 <= b.y0 - im.y1 <= 30 and _x_overlap(b, im) for im in images)
        if b.n_lines <= 4 and ((b.font_size < body * 0.88 and near_image) or b.font_size < body * 0.80):
            b.kind = CAPTION          # caption under a photo, or tiny type (a scan with no image detector)
    return body


def segment_articles(blocks: list[Block], page: PageRecord, tol: float = 8.0) -> list[Article]:
    headlines = sorted((b for b in blocks if b.kind == HEADLINE), key=lambda b: (b.y0, b.x0))
    arts = [Article(page.paper_id, page.page_no, headline_block=h, pages=[page.page_no]) for h in headlines]
    orphans: list[Block] = []

    def owner(b: Block) -> Article | None:
        best = None
        for a in arts:
            h = a.headline_block
            if h.y0 <= b.y0 + tol and h.x0 - tol <= b.cx <= h.x1 + tol:
                if best is None or h.y1 > best.headline_block.y1:
                    best = a
        return best

    for b in blocks:
        if b.kind in (HEADLINE, FURNITURE):
            continue
        if b.kind == KICKER:
            below = [a for a in arts if a.headline_block.y0 >= b.y0 - tol and _x_overlap(a.headline_block, b)]
            if below:
                a = min(below, key=lambda a: a.headline_block.y0)
                a.blocks.append(b)
                continue
        a = owner(b)
        if a is None:
            orphans.append(b)
        else:
            a.blocks.append(b)

    # a headline can be narrower than its article (short headline over several columns): attach orphans that sit
    # in the column right next to an already-assigned block and start at the same height
    changed = True
    while orphans and changed:
        changed = False
        for b in list(orphans):
            for a in arts:
                if any(0 <= b.x0 - c.x1 <= 90 and abs(b.y0 - c.y0) <= 25 for c in a.blocks if c.kind == BODY):
                    a.blocks.append(b)
                    orphans.remove(b)
                    changed = True
                    break

    chars = sum(len(b.text) for b in orphans if b.kind == BODY)
    if orphans and chars >= 300:
        arts.append(Article(page.paper_id, page.page_no, headline_block=None, blocks=orphans, pages=[page.page_no],
                            flags=["no_headline"]))
    return arts


def reading_order(blocks: list[Block], col_tol: float = 20.0) -> list[Block]:
    """Column-major order: left column top-to-bottom, then the next column ..."""
    if not blocks:
        return []
    xs = sorted({round(b.x0, 1) for b in blocks})
    cols, start = [], None
    for x in xs:
        if start is None or x - start > col_tol:
            cols.append(x)
            start = x
    def col_idx(b: Block) -> int:
        idx = 0
        for i, c in enumerate(cols):
            if b.x0 >= c - col_tol / 2:
                idx = i
        return idx
    return sorted(blocks, key=lambda b: (col_idx(b), b.y0))
