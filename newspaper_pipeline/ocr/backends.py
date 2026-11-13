"""Block extraction backends.

* `PdfTextBackend`  - digital e-paper: read text blocks (with font size/bold) and image blocks straight from
  the PDF. Confidence is 1.0. This is the fast path and needs no model.
* `ImageOcrBackend` - scanned e-paper: run an OCR engine over the rendered page image and group the
  recognised lines into blocks. Engines are pluggable (RapidOCR on CPU, PaddleOCR on GPU, Tesseract).

Both return `Block`s in PDF points so everything downstream is backend-agnostic.
"""
from __future__ import annotations

import statistics
from typing import Protocol

import pymupdf as fitz

from ..input.splitter import PageRecord
from .models import BODY, IMAGE, Block, OcrLine
from .textfix import fix_spacing


class OcrEngine(Protocol):
    def read(self, image) -> list[OcrLine]:
        """image: PIL.Image. Returns recognised lines with pixel bboxes and 0-1 confidence."""


# --------------------------------------------------------------------------------------- digital PDF
class PdfTextBackend:
    name = "pdf_text"

    def blocks(self, page: PageRecord) -> list[Block]:
        out: list[Block] = []
        with fitz.open(page.pdf_path) as doc:
            data = doc[page.page_no - 1].get_text("dict")
        for b in data["blocks"]:
            bbox = tuple(b["bbox"])
            if b["type"] == 1:
                out.append(Block(page.page_no, bbox, "", IMAGE))
                continue
            # split the PDF block wherever the font size or face changes between lines: a headline, its
            # byline and a kicker are often emitted as one block by the producer
            groups: list[list[dict]] = []
            prev = None
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"])
                if not txt.strip():
                    continue
                sig = _line_sig(ln)
                if prev is None or abs(sig[0] - prev[0]) > 0.12 * max(prev[0], 1) or sig[1] != prev[1]:
                    groups.append([])
                groups[-1].append(ln)
                prev = sig
            for grp in groups:
                lines, sizes, weights, bold_chars, chars = [], [], [], 0, 0
                for ln in grp:
                    lines.append("".join(s["text"] for s in ln["spans"]).rstrip())
                    for s in ln["spans"]:
                        n = len(s["text"].strip())
                        sizes.append(s["size"]); weights.append(n); chars += n
                        if s["flags"] & 16 or "bold" in s["font"].lower():   # bit 4 = bold
                            bold_chars += n
                x0 = min(l["bbox"][0] for l in grp); y0 = min(l["bbox"][1] for l in grp)
                x1 = max(l["bbox"][2] for l in grp); y1 = max(l["bbox"][3] for l in grp)
                out.append(Block(page.page_no, (x0, y0, x1, y1), "\n".join(lines), BODY, 1.0,
                                 _weighted_median(sizes, weights),
                                 bold=chars > 0 and bold_chars / chars > 0.5, n_lines=len(lines)))
        return out


def _line_sig(line: dict) -> tuple[float, str]:
    spans = [s for s in line["spans"] if s["text"].strip()] or line["spans"]
    big = max(spans, key=lambda s: len(s["text"]))
    return big["size"], big["font"]


def _weighted_median(values: list[float], weights: list[int]) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs) or 1
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return statistics.median(values) if values else 0.0


# ---------------------------------------------------------------------------------- scanned page OCR
class ImageOcrBackend:
    """OCR on the page image. Lines are merged into blocks when vertically adjacent and column-aligned."""
    name = "image_ocr"

    def __init__(self, engine: OcrEngine, line_gap_factor: float = 1.0, col_tol_pt: float = 14.0) -> None:
        self.engine = engine
        self.gap_factor = line_gap_factor
        self.col_tol = col_tol_pt

    def blocks(self, page: PageRecord) -> list[Block]:
        from PIL import Image
        img = Image.open(page.image_path)
        sx, sy = page.width / img.width, page.height / img.height
        lines = [ln for ln in self._read_strips(img) if ln.text.strip()]
        scaled = [(fix_spacing(ln.text.strip()), (ln.bbox[0] * sx, ln.bbox[1] * sy, ln.bbox[2] * sx, ln.bbox[3] * sy), ln.conf)
                  for ln in lines]
        return self._group(scaled, page.page_no)

    def _read_strips(self, img, strip: int = 1000, margin: int = 160) -> list[OcrLine]:
        """OCR engines shrink big images (RapidOCR caps the long side at ~2000 px), which blurs small newspaper type and
        costs letters and spaces. Read a tall page as overlapping full-width strips instead: each line is kept from the
        one strip that owns its centre, so nothing is read twice or cut in half."""
        if img.height <= strip + 2 * margin:
            return self.engine.read(img)
        out: list[OcrLine] = []
        for top in range(0, img.height, strip):
            y0, y1 = max(0, top - margin), min(img.height, top + strip + margin)
            for ln in self.engine.read(img.crop((0, y0, img.width, y1))):
                cy = y0 + (ln.bbox[1] + ln.bbox[3]) / 2
                if top <= cy < top + strip:
                    out.append(OcrLine(ln.text, (ln.bbox[0], ln.bbox[1] + y0, ln.bbox[2], ln.bbox[3] + y0), ln.conf))
        return out

    @staticmethod
    def _size(text: str, bb) -> float:
        """Relative type size of an OCR line. Box height alone is skewed by descenders, so for lines long enough
        to average over, blend it with the per-character width (geometric mean)."""
        h = (bb[3] - bb[1]) / 1.2
        n = len(text)
        if n < 12:
            return h
        return (h * ((bb[2] - bb[0]) / n / 0.5)) ** 0.5

    def _columns(self, lines) -> dict[int, int]:
        """index of line -> column number, clustering left edges by gaps."""
        order = sorted(range(len(lines)), key=lambda i: lines[i][1][0])
        col, prev, out = -1, None, {}
        for i in order:
            x = lines[i][1][0]
            if prev is None or x - prev > self.col_tol:
                col += 1
            out[i] = col
            prev = x
        return out

    def _group(self, lines, page_no: int) -> list[Block]:
        cols = self._columns(lines)
        idx = sorted(range(len(lines)), key=lambda i: (cols[i], lines[i][1][1]))
        sizes = {i: self._size(lines[i][0], lines[i][1]) for i in idx}
        groups: list[list[int]] = []
        for i in idx:
            bb = lines[i][1]
            placed = False
            if groups:
                j = groups[-1][-1]
                lb = lines[j][1]
                lh = lb[3] - lb[1]
                gap = bb[1] - lb[3]
                if (cols[i] == cols[j] and 0.8 <= sizes[i] / max(sizes[j], 1e-6) <= 1.25
                        and -2 <= gap <= lh * 0.9 * self.gap_factor):
                    groups[-1].append(i)
                    placed = True
            if not placed:
                groups.append([i])
        out = []
        for g in groups:
            grp = [lines[i] for i in g]
            x0 = min(t[1][0] for t in grp); y0 = min(t[1][1] for t in grp)
            x1 = max(t[1][2] for t in grp); y1 = max(t[1][3] for t in grp)
            chars = [max(1, len(t[0])) for t in grp]
            conf = sum(t[2] * c for t, c in zip(grp, chars)) / sum(chars)
            font = statistics.median(sizes[i] for i in g)
            out.append(Block(page_no, (x0, y0, x1, y1), "\n".join(t[0] for t in grp), BODY, round(conf, 4), font,
                             bold=False, n_lines=len(grp)))
        return out


# ------------------------------------------------------------------------------------- real engines
class RapidOcrEngine:
    """CPU OCR via rapidocr-onnxruntime (pip install rapidocr-onnxruntime). Latin + Chinese models by default."""

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()

    def read(self, image) -> list[OcrLine]:
        import numpy as np
        result, _ = self._ocr(np.array(image.convert("RGB")))
        out = []
        for box, text, score in (result or []):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            out.append(OcrLine(text, (min(xs), min(ys), max(xs), max(ys)), float(score)))
        return out


class TesseractEngine:
    """pip install pytesseract + the tesseract binary (brew install tesseract tesseract-lang)."""

    def __init__(self, lang: str = "eng") -> None:
        import pytesseract
        self._t, self.lang = pytesseract, lang

    def read(self, image) -> list[OcrLine]:
        d = self._t.image_to_data(image, lang=self.lang, output_type=self._t.Output.DICT)
        lines: dict[tuple, list] = {}
        for i, txt in enumerate(d["text"]):
            if not txt.strip() or float(d["conf"][i]) < 0:
                continue
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append(i)
        out = []
        for idxs in lines.values():
            x0 = min(d["left"][i] for i in idxs); y0 = min(d["top"][i] for i in idxs)
            x1 = max(d["left"][i] + d["width"][i] for i in idxs); y1 = max(d["top"][i] + d["height"][i] for i in idxs)
            conf = sum(float(d["conf"][i]) for i in idxs) / len(idxs) / 100.0
            out.append(OcrLine(" ".join(d["text"][i] for i in idxs), (x0, y0, x1, y1), conf))
        return out


class PaddleOcrEngine:
    """GPU/CPU OCR via PaddleOCR; pass lang='hi', 'ta', 'en', ... (pip install paddleocr)."""

    def __init__(self, lang: str = "en", use_gpu: bool = True) -> None:
        from paddleocr import PaddleOCR
        self._ocr = PaddleOCR(lang=lang, use_gpu=use_gpu, show_log=False)

    def read(self, image) -> list[OcrLine]:
        import numpy as np
        res = self._ocr.ocr(np.array(image.convert("RGB")), cls=True)
        out = []
        for line in (res[0] or []):
            box, (text, score) = line
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            out.append(OcrLine(text, (min(xs), min(ys), max(xs), max(ys)), float(score)))
        return out
