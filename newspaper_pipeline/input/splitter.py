"""Page splitter: edition PDF -> one record per page (+ rendered page image).

`has_text_layer` tells the OCR stage whether it can read the embedded text (digital e-paper) or must
run real OCR on the image (scanned e-paper).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from pathlib import Path

import pymupdf as fitz

from .fetch import FetchedEdition


@dataclass
class PageRecord:
    paper_id: str
    paper_name: str
    edition: str
    language: str
    date: str               # ISO date
    page_no: int            # 1-based
    pdf_path: str
    image_path: str | None
    width: float            # PDF points
    height: float
    has_text_layer: bool
    reputation: str = "not_rated"

    @property
    def page_id(self) -> str:
        return f"{self.paper_id}:{self.date}:p{self.page_no}"

    def to_dict(self) -> dict:
        return asdict(self)


def split_edition(edition: FetchedEdition, run_date: dt.date, out_dir: str | Path, dpi: int = 300,
                  render: bool = True) -> list[PageRecord]:
    paper = edition.paper
    pages_dir = Path(out_dir) / "pages" / run_date.isoformat() / paper.id
    pages_dir.mkdir(parents=True, exist_ok=True)
    records: list[PageRecord] = []
    with fitz.open(edition.path) as doc:
        for page_no in paper.page_numbers(doc.page_count):
            page = doc[page_no - 1]
            text = page.get_text("text").strip()
            image_path = None
            if render:
                image_path = str(pages_dir / f"page-{page_no:03d}.png")
                page.get_pixmap(dpi=dpi).save(image_path)
            records.append(PageRecord(
                paper_id=paper.id, paper_name=paper.name, edition=paper.edition, language=paper.language,
                date=run_date.isoformat(), page_no=page_no, pdf_path=str(edition.path), image_path=image_path,
                width=page.rect.width, height=page.rect.height, has_text_layer=len(text) >= 20,
                reputation=paper.reputation,
            ))
    return records
