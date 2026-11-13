"""Page-level ad detection: DocLayout-YOLO layout blocks + a YOLOv8 newspaper ad model.

* The ad model (models/ad_yolo.pt, classes `ad` / `article`) finds advertisement regions on the rendered page.
* DocLayout-YOLO (models/doclayout.pt) cuts the page into layout blocks (title, plain text, figure, ...). It has no "ad" class,
  so a block counts as part of an ad when >= 50 % of it sits inside an ad region.
Everything is saved per page (see `analyse_page`) so the UI can show - and let a person adjust - what is being ignored.
Each OCR record is also compared with the ad regions: a record inside an ad region is an ad whatever its text says.

Both models are optional: with no weights or no packages the pipeline uses the text rules only.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable

log = logging.getLogger("np.adyolo")
_lock = threading.Lock()
_infer_lock = threading.Lock()                         # the models are not thread-safe
_cache: dict = {}

AD_MIN_CONF = 0.05          # every candidate is saved; the UI filters further
AD_IOU = 0.1                # same as the source repo's lean_proc.py
LAYOUT_MIN_CONF = 0.10
LAYOUT_IMGSZ = 1024
RENDER_DPI = 110
DECISION_CONF = float(os.environ.get("NP_AD_CONF", "0.25"))      # ads below this are NOT used to drop records


def _load(env: str, default: str, make):
    path = os.environ.get(env, default)
    if path.lower() in ("", "off", "none") or not os.path.isfile(path):
        return None
    with _lock:
        if path not in _cache:
            try:
                _cache[path] = make(path)
                log.info("%s loaded: %s", env, path)
            except Exception as e:                      # package missing, corrupt file, ...: degrade
                log.warning("%s unavailable (%s): %s", env, type(e).__name__, e)
                _cache[path] = None
        return _cache[path]


class AdDetector:
    def __init__(self, weights: str) -> None:
        from ultralytics import YOLO
        self.model = YOLO(weights)
        self.names = self.model.names

    def detect(self, img, scale: tuple[float, float]) -> list[dict]:
        with _infer_lock:
            res = self.model(img, conf=AD_MIN_CONF, iou=AD_IOU, verbose=False)[0]
        return _boxes(res, self.names, scale)


class LayoutDetector:
    def __init__(self, weights: str) -> None:
        from doclayout_yolo import YOLOv10
        self.model = YOLOv10(weights)
        self.names = self.model.names

    def detect(self, img, scale: tuple[float, float]) -> list[dict]:
        with _infer_lock:
            res = self.model.predict(img, imgsz=LAYOUT_IMGSZ, conf=LAYOUT_MIN_CONF, device="cpu", verbose=False)[0]
        return _boxes(res, self.names, scale)


def _boxes(res, names, scale) -> list[dict]:
    sx, sy = scale
    out = []
    for b in res.boxes:
        x0, y0, x1, y1 = (float(v) for v in b.xyxy[0])
        out.append({"label": str(names[int(b.cls[0])]), "conf": round(float(b.conf[0]), 4),
                    "bbox": [round(max(0, x0) * sx, 1), round(max(0, y0) * sy, 1), round(x1 * sx, 1), round(y1 * sy, 1)]})
    out.sort(key=lambda d: -d["conf"])
    return out


def get_ad_detector() -> AdDetector | None:
    return _load("NP_AD_MODEL", "models/ad_yolo.pt", AdDetector)


def get_layout_detector() -> LayoutDetector | None:
    return _load("NP_LAYOUT_MODEL", "models/doclayout.pt", LayoutDetector)


def analyse_page(page) -> dict | None:
    """Ad regions + layout blocks of one PageRecord, in PDF points. None when neither model is available.
    {"regions": [{"id", "label": "ad"|"article", "conf", "bbox"}], "blocks": [{"id", "cls", "conf", "bbox"}]}"""
    ads, lay = get_ad_detector(), get_layout_detector()
    if ads is None and lay is None:
        return None
    import pymupdf as fitz
    from PIL import Image
    with fitz.open(page.pdf_path) as doc:
        pix = doc[page.page_no - 1].get_pixmap(dpi=RENDER_DPI)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    scale = (page.width / pix.width, page.height / pix.height)
    regions = ads.detect(img, scale) if ads else []
    blocks = lay.detect(img, scale) if lay else []
    for i, r in enumerate(regions):
        r["id"] = i
    for i, b in enumerate(blocks):
        b["id"] = i
        b["cls"] = b.pop("label")
    return {"regions": regions, "blocks": blocks, "ads": ads is not None, "layout": lay is not None}


def _area(b: Iterable[float]) -> float:
    x0, y0, x1, y1 = b
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _inter(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def record_boxes(rec: dict) -> list[list[float]]:
    boxes = ([rec["headline_bbox"]] if rec.get("headline_bbox") else []) + list(rec.get("body_bboxes") or [])
    return [b for b in boxes if _area(b) > 0]


def overlap_fraction(rec: dict, regions: list[dict], label: str, min_conf: float = 0.0) -> float:
    """Share of the record's own area (headline + body boxes) that lies inside `label` regions of the page."""
    boxes = record_boxes(rec)
    total = sum(_area(b) for b in boxes)
    if not total:
        return 0.0
    keep = [r for r in regions if r["label"] == label and r["conf"] >= min_conf]
    inside = 0.0
    for b in boxes:
        inside += min(_area(b), sum(_inter(b, r["bbox"]) for r in keep))
    return round(min(1.0, inside / total), 3)


def annotate_records(recs: list[dict], regions: list[dict]) -> None:
    """Store how much of each record sits in confident ad / article regions; `ocr.adscan` turns that into the label."""
    for r in recs:
        r["yolo_ad_frac"] = overlap_fraction(r, regions, "ad", DECISION_CONF)
        r["yolo_article_frac"] = overlap_fraction(r, regions, "article", 0.5)
