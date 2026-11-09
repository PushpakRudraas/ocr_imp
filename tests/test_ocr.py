import pytest
import pymupdf as fitz

from newspaper_pipeline.config import Settings
from newspaper_pipeline.input.fetch import fetch_editions
from newspaper_pipeline.input.splitter import split_edition
from newspaper_pipeline.ocr.backends import ImageOcrBackend, PdfTextBackend
from newspaper_pipeline.ocr.fields import detect_wire, parse_dateline
from newspaper_pipeline.ocr.layout import classify_blocks, reading_order, segment_articles
from newspaper_pipeline.ocr.models import BODY, BYLINE, CAPTION, FURNITURE, HEADLINE, IMAGE, Block, OcrLine
from newspaper_pipeline.ocr.quality import apply_gate, garbage_ratio
from newspaper_pipeline.ocr.run import OcrUnavailable, crop_images, extract_edition, extract_page
from newspaper_pipeline.ocr.stitch import stitch_continuations


@pytest.fixture(scope="module")
def editions(manifest, tmp_path_factory):
    out = tmp_path_factory.mktemp("work")
    res = {}
    for e in fetch_editions(manifest, out):
        res[e.paper.id] = split_edition(e, manifest.run_date, out, dpi=50)
    return res, out


def by_headline(recs, needle):
    return next(r for r in recs if needle.lower() in r["headline"].lower())


# ---------------------------------------------------------------------------------- unit: parsing
def test_parse_dateline_variants():
    i, body = parse_dateline("CHANDIGARH, Nov 2 (ANI): Kunwar Singh resigned.")
    assert (i["city"], i["wire"]) == ("Chandigarh", "ANI") and body.startswith("Kunwar")
    i, _ = parse_dateline("CHANDIGARH: Kunwar Singh resigned.")
    assert i["city"] == "Chandigarh" and "wire" not in i
    i, _ = parse_dateline("NEW DELHI, NOVEMBER 2: Air quality")
    assert i["city"] == "New Delhi"
    i, body = parse_dateline("चंडीगढ़, 2 नवंबर (एएनआई): कुंवर विजय")
    assert (i["city"], i["wire"]) == ("चंडीगढ़", "ANI") and body.startswith("कुंवर")
    # an ordinary sentence is not a dateline
    assert parse_dateline("Mr. Singh said: we will wait")[0] == {}
    assert parse_dateline("The minister said: nothing")[0] == {}


def test_detect_wire():
    assert detect_wire("By PTI") == "PTI"
    assert detect_wire("By Staff Reporter") is None
    assert detect_wire("Reuters report") == "Reuters"


def test_reading_order_is_column_major():
    bl = [Block(1, (200, 100, 380, 160), "c2-top"), Block(1, (20, 100, 180, 160), "c1-top"),
          Block(1, (20, 200, 180, 260), "c1-bot"), Block(1, (200, 200, 380, 260), "c2-bot")]
    assert [b.text for b in reading_order(bl)] == ["c1-top", "c1-bot", "c2-top", "c2-bot"]


def test_garbage_ratio():
    assert garbage_ratio("Normal news text, with punctuation.") < 0.02
    assert garbage_ratio("§¶‡†◊∆≈ç√∫~µ≤≥÷") > 0.8
    assert garbage_ratio("") == 1.0


# ----------------------------------------------------------------------------- digital PDF end to end
def test_pdf_blocks_have_font_sizes_and_images(editions):
    pages, _ = editions
    blocks = PdfTextBackend().blocks(pages["the_hindu"][0])
    assert any(b.kind == IMAGE for b in blocks)
    sizes = sorted({round(b.font_size) for b in blocks if b.text})
    assert max(sizes) >= 20 and 9 in sizes


def test_classify_marks_headline_byline_caption_furniture(editions):
    pages, _ = editions
    page = pages["the_hindu"][0]
    blocks = PdfTextBackend().blocks(page)
    body = classify_blocks(blocks, page)
    assert 8 <= body <= 10
    kinds = {b.kind for b in blocks}
    assert {HEADLINE, BYLINE, CAPTION, FURNITURE, BODY, IMAGE} <= kinds
    assert any(b.kind == FURNITURE and "FRONT PAGE" in b.text for b in blocks)
    assert any(b.kind == HEADLINE and "resigns" in b.text for b in blocks)


def test_segment_front_page_into_two_articles(editions):
    pages, _ = editions
    page = pages["toi"][0]
    blocks = PdfTextBackend().blocks(page)
    classify_blocks(blocks, page)
    arts = segment_articles(blocks, page)
    assert len(arts) == 2
    main = next(a for a in arts if "Vijay" in a.headline_block.text)
    assert sum(1 for b in main.blocks if b.kind == BODY) == 3     # 3 columns
    side = next(a for a in arts if "pollution" in a.headline_block.text.lower())
    assert all(b.x0 > 600 for b in side.blocks)                    # right-hand column only


def test_extract_full_article_fields(editions):
    pages, _ = editions
    recs = extract_edition(pages["toi"])
    a = by_headline(recs, "AAP loses another MLA")
    assert a["byline"] == "TNN" and a["city"] == "Chandigarh" and a["wire_source"] is None
    assert a["section"] == "Front Page" and a["page_no"] == 1
    flat = " ".join(a["text"].split())
    assert flat.startswith("Kunwar Vijay Pratap Singh on Monday resigned")
    assert "Opposition parties demanded a by-election" in flat
    assert "CHANDIGARH:" not in a["text"]                          # dateline removed from body
    assert a["ocr_confidence"] == 1.0 and a["status"] == "ok"
    assert a["image_refs"][0]["caption"] == "Kunwar Vijay Pratap Singh outside the Assembly."
    assert a["ocr_backend"] == "pdf_text"


def test_multi_column_text_reads_in_order(editions):
    pages, _ = editions
    a = by_headline(extract_edition(pages["the_hindu"]), "Punjab MLA Kunwar")
    t = " ".join(a["text"].split())
    assert t.index("becoming the third") < t.index("He submitted") < t.index("Opposition parties demanded")


def test_wire_from_dateline(editions):
    pages, _ = editions
    a = by_headline(extract_edition(pages["the_hindu"]), "Punjab MLA Kunwar")
    assert a["wire_source"] == "ANI" and a["byline"] == "Staff Reporter"


def test_continued_story_is_stitched(editions):
    pages, _ = editions
    recs = extract_edition(pages["the_hindu"])
    a = by_headline(recs, "Punjab MLA Kunwar")
    assert a["pages"] == [1, 3] and "stitched" in a["flags"]
    t = " ".join(a["text"].split())
    assert "(Continued on page 3)" not in t
    assert t.index("Opposition parties demanded") < t.index("Mr. Singh said he was quitting")
    assert t.endswith("Assembly elections.")
    # the Contd. fragment is no longer a standalone article
    assert not any("Contd" in r["headline"] for r in recs)
    assert not any("is_continuation" in r["flags"] and "stitched" not in r["flags"] and r["page_no"] == 3 for r in recs)


def test_orphan_continuation_flagged():
    src = {"article_id": "s", "paper_id": "p", "page_no": 1, "headline": "Budget", "text": "alpha beta gamma",
           "continued_to": 5, "flags": ["continued_to"], "pages": [1], "ocr_confidence": 1.0, "body_bboxes": [],
           "image_refs": [], "headline_bbox": [0, 0, 1, 1]}
    frag = {"article_id": "f", "paper_id": "p", "page_no": 7, "headline": "Other", "text": "zzz", "continued_from": 1,
            "continued_to": None, "flags": ["is_continuation"], "pages": [7], "ocr_confidence": 1.0, "body_bboxes": [],
            "image_refs": [], "headline_bbox": [0, 0, 1, 1]}
    out = stitch_continuations([src, frag])
    flags = {r["article_id"]: r["flags"] for r in out}
    assert "orphan_continuation" in flags["f"] and "missing_continuation" in flags["s"]


def test_stitch_picks_best_of_two_candidates():
    def rec(i, page, head, text, to=None, frm=None, cont=False):
        return {"article_id": i, "paper_id": "p", "page_no": page, "headline": head, "text": text, "continued_to": to,
                "continued_from": frm, "flags": (["continued_to"] if to else []) + (["is_continuation"] if cont else []),
                "pages": [page], "ocr_confidence": 1.0, "body_bboxes": [], "image_refs": [], "headline_bbox": [0, page, 1, 1]}
    a = rec("a", 1, "Metro extension approved", "metro airport corridor delhi government", to=4)
    b = rec("b", 1, "Cricket auction", "mumbai indians coach auction player", to=4)
    f = rec("f", 4, "Mumbai Indians coach", "coach auction squad preparation", cont=True)
    out = stitch_continuations([a, b, f])
    byid = {r["article_id"]: r for r in out}
    assert byid["b"]["pages"] == [1, 4] and byid["a"]["pages"] == [1] and "f" not in byid


def test_hindi_paper_extraction(editions):
    pages, _ = editions
    if "dainik_jagran" not in pages:
        pytest.skip("no Devanagari font")
    recs = extract_edition(pages["dainik_jagran"])
    a = by_headline(recs, "इस्तीफा")
    assert a["language"] == "hi" and a["wire_source"] == "ANI" and a["city"] == "चंडीगढ़"
    assert a["byline"] == "संवाददाता" and "कुंवर विजय प्रताप सिंह" in " ".join(a["text"].split())


def test_all_papers_yield_expected_counts(editions):
    pages, _ = editions
    counts = {p: len(extract_edition(v)) for p, v in pages.items()}
    # the_hindu: resign(+local) front, rbi, (contd merged) ; toi: 2 + rbi + weather
    assert counts["the_hindu"] == 3 and counts["toi"] == 4


# ------------------------------------------------------------------------------------ quality gate
def _rec(conf=1.0, text="x" * 300, headline="H", flags=None):
    return {"ocr_confidence": conf, "text": text, "headline": headline, "flags": flags or []}


def test_gate_ok_and_review():
    s = Settings()
    assert apply_gate(_rec(), s)["status"] == "ok"
    r = apply_gate(_rec(conf=0.6), s)
    assert r["status"] == "review" and "low_confidence" in r["quality_issues"]
    assert apply_gate(_rec(text="short"), s)["status"] == "ok"               # short is not an OCR failure
    assert apply_gate(_rec(flags=["missing_continuation"]), s)["status"] == "review"


def test_gate_fallback_rescues_low_confidence():
    s = Settings()
    calls = []

    def fallback(rec):
        calls.append(rec["headline"])
        return ("clean text " * 40, 0.93)

    r = apply_gate(_rec(conf=0.5), s, fallback)
    assert calls == ["H"] and r["status"] == "ok" and "fallback_used" in r["flags"] and r["ocr_confidence"] == 0.93


def test_gate_fallback_that_fails_stays_review():
    r = apply_gate(_rec(conf=0.5), Settings(), lambda rec: ("still bad", 0.4))
    assert r["status"] == "review"


# ----------------------------------------------------------- scanned-page path (engine is faked, geometry is real)
class GeometryEngine:
    """Pretends to be an OCR engine by replaying the PDF's own text lines in image pixels, with a set confidence."""

    def __init__(self, page_rec, scale, conf=0.92):
        self.page, self.scale, self.conf = page_rec, scale, conf

    def read(self, image):
        with fitz.open(self.page.pdf_path) as doc:
            d = doc[self.page.page_no - 1].get_text("dict")
        out = []
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b["lines"]:
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    x0, y0, x1, y1 = [v * self.scale for v in ln["bbox"]]
                    out.append(OcrLine(t, (x0, y0, x1, y1), self.conf))
        return out


def test_image_ocr_backend_groups_lines_and_scales(editions):
    pages, _ = editions
    page = pages["toi"][0]
    scale = 50 / 72
    backend = ImageOcrBackend(GeometryEngine(page, scale))
    blocks = backend.blocks(page)
    assert all(abs(b.conf - 0.92) < 1e-6 for b in blocks)
    head = max(blocks, key=lambda b: b.font_size)
    assert "AAP loses another MLA" in head.text and head.font_size > 15
    # coordinates are back in PDF points
    assert head.x0 == pytest.approx(36, abs=3)


def test_scanned_page_end_to_end_with_engine(editions):
    pages, _ = editions
    page = pages["toi"][0]
    page_scanned = type(page)(**{**page.to_dict(), "has_text_layer": False})
    eng = GeometryEngine(page_scanned, 50 / 72, conf=0.95)
    recs = extract_page(page_scanned, eng)
    a = by_headline(recs, "AAP loses another MLA")
    assert a["ocr_backend"] == "image_ocr" and a["ocr_confidence"] == pytest.approx(0.95, abs=0.01)
    assert a["city"] == "Chandigarh" and "by-election" in " ".join(a["text"].split()) and a["byline"] == "TNN"


def test_scanned_page_without_engine_raises(editions):
    pages, _ = editions
    page = pages["toi"][0]
    scanned = type(page)(**{**page.to_dict(), "has_text_layer": False})
    with pytest.raises(OcrUnavailable):
        extract_page(scanned, None)


def test_low_confidence_scan_goes_to_review(editions):
    pages, _ = editions
    page = pages["toi"][1]
    scanned = type(page)(**{**page.to_dict(), "has_text_layer": False})
    recs = extract_edition([scanned], engine=GeometryEngine(scanned, 50 / 72, conf=0.6))
    assert recs and all(r["status"] == "review" for r in recs)


def test_crop_images(editions, tmp_path):
    pages, _ = editions
    recs = extract_edition(pages["toi"])
    a = by_headline(recs, "AAP loses another MLA")
    paths = crop_images(a, pages["toi"][0].pdf_path, tmp_path)
    assert len(paths) == 1
    from PIL import Image
    assert Image.open(paths[0]).size[0] > 50


# --------------------------------------------------------------------------- real OCR (skipped if not installed)
def test_real_rapidocr_on_rendered_scan(manifest, tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    from newspaper_pipeline.ocr.backends import RapidOcrEngine
    eds = {e.paper.id: e for e in fetch_editions(manifest, tmp_path)}
    pages = split_edition(eds["toi"], manifest.run_date, tmp_path, dpi=200)
    page = type(pages[0])(**{**pages[0].to_dict(), "has_text_layer": False})
    truth = extract_page(pages[0])                                   # embedded-text ground truth
    recs = extract_page(page, RapidOcrEngine())
    a = by_headline(recs, "another MLA")
    assert a["ocr_backend"] == "image_ocr" and 0.7 < a["ocr_confidence"] <= 1.0
    flat = " ".join(a["text"].split()).lower()
    for phrase in ("resigned", "punjab assembly", "by-election"):
        assert phrase in flat
    assert "breathing problems" not in flat          # the sidebar story must not leak in
    assert a["byline"] and "TNN" in a["byline"]
    # character-level similarity against the embedded text
    import difflib
    ref = " ".join(by_headline(truth, "another MLA")["text"].split()).lower()
    assert difflib.SequenceMatcher(None, ref, flat).ratio() > 0.85
