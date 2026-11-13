"""OCR stage driver: pages -> article records."""
from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz

from ..config import Settings
from ..input.splitter import PageRecord
from .backends import ImageOcrBackend, OcrEngine, PdfTextBackend
from .adyolo import DECISION_CONF, analyse_page, annotate_records
from .fields import build_record, page_section
from .layout import attach_drop_caps, classify_blocks, segment_articles
from .models import IMAGE
from .quality import apply_gate
from .stitch import stitch_continuations


log = logging.getLogger("np.ocr")


class OcrUnavailable(RuntimeError):
    pass


def extract_page(page: PageRecord, engine: OcrEngine | None = None, force_ocr: bool = False,
                 regions_out: list | None = None) -> list[dict]:
    if page.has_text_layer and not force_ocr:
        backend = PdfTextBackend()
    else:
        if engine is None:
            raise OcrUnavailable(f"{page.page_id} has no text layer and no OCR engine was configured")
        backend = ImageOcrBackend(engine)
    blocks = attach_drop_caps(backend.blocks(page))
    # OCR line heights are noisier than embedded font sizes, so headlines need a lower size ratio there
    classify_blocks(blocks, page, headline_ratio=1.35 if backend.name == "pdf_text" else 1.25)
    section = page_section(blocks)
    recs = [build_record(a, page, section) for a in segment_articles(blocks, page)]
    for r in recs:
        r["ocr_backend"] = backend.name
    found = None
    try:                                          # YOLO: which parts of the page are ads, plus its layout blocks
        found = analyse_page(page)
        if found:
            annotate_records(recs, found["regions"])
    except Exception:
        log.exception("ad detection failed on %s - using the text rules only", page.page_id)
    if regions_out is not None:                   # saved for the UI's page view (also for pages with no regions: it needs the size)
        regions_out.append({"paper_id": page.paper_id, "page_no": page.page_no, "width": page.width, "height": page.height,
                            "yolo": bool(found and found["ads"]), "layout": bool(found and found["layout"]), "ad_conf": DECISION_CONF,
                            "regions": found["regions"] if found else [], "blocks": found["blocks"] if found else []})
    return recs


def extract_edition(pages: list[PageRecord], settings: Settings | None = None, engine: OcrEngine | None = None,
                    fallback=None, force_ocr: bool = False, regions_out: list | None = None) -> list[dict]:
    s = settings or Settings()
    records: list[dict] = []
    for page in pages:
        records.extend(extract_page(page, engine, force_ocr, regions_out))
    records = stitch_continuations(records)
    return [apply_gate(r, s, fallback) for r in records]


def crop_images(record: dict, pdf_path: str, out_dir: str | Path, dpi: int = 150) -> list[str]:
    """Save each photo of an article as a PNG (input for the optional photo captioner). Returns paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    with fitz.open(pdf_path) as doc:
        for i, ref in enumerate(record["image_refs"]):
            page = doc[ref["page_no"] - 1]
            p = out / f"{record['article_id']}-{i}.png"
            page.get_pixmap(dpi=dpi, clip=fitz.Rect(*ref["bbox"])).save(str(p))
            paths.append(str(p))
    return paths
