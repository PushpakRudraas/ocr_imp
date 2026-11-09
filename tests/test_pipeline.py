"""Integration tests: the whole pipeline on synthetic newspapers, plus export, CLI, LLM refiner and a real-OCR run."""
import datetime as dt
import json
from pathlib import Path

import pytest

from helpers import PhraseTableBackend
from newspaper_pipeline.cli import main as cli_main
from newspaper_pipeline.config import Settings
from newspaper_pipeline.enrich.llm import LlmRefiner
from newspaper_pipeline.enrich.pipeline import enrich_article
from newspaper_pipeline.input.manifest import Manifest, PaperSpec
from newspaper_pipeline.output.export import review_items, story_views
from newspaper_pipeline.pipeline import Pipeline
from newspaper_pipeline.store import Store

SAMPLE = json.loads((Path(__file__).parent.parent.parent / "sample.json").read_text())


def make_pipeline(tmp_path, **kw):
    s = Settings(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "p.sqlite"), render_dpi=40)
    return Pipeline(s, Store(s.db_path), translator=PhraseTableBackend(), **kw)


@pytest.fixture(scope="module")
def run(manifest, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("run")
    p = make_pipeline(tmp)
    res = p.run(manifest, tmp / "out")
    return p, res, tmp


def _story_with(res, needle):
    return next(s for s in story_views(res.docs) if needle in s["coverage"][0]["headline"].lower() or
                any(needle in c["headline"].lower() for c in s["coverage"]))


# ----------------------------------------------------------------------------------------------- the headline requirement
def test_same_news_from_different_papers_is_identified_as_one_story(run):
    _, res, _ = run
    resign = next(s for s in story_views(res.docs) if s["event_type"] == "Politics.Resignation")
    papers = {c["paper_id"] for c in resign["coverage"]}
    assert {"the_hindu", "toi", "indian_express"} <= papers
    assert len(resign["coverage"]) == len(papers)                        # exactly one article per paper
    assert resign["location"] == "Punjab" and resign["n_papers"] >= 3
    rbi = next(s for s in story_views(res.docs) if s["event_type"] == "Economy.MonetaryPolicy")
    assert {c["paper_id"] for c in rbi["coverage"]} == {"the_hindu", "toi", "indian_express"}
    assert resign["story_id"] != rbi["story_id"]


def test_hindi_paper_joins_the_english_story_after_translation(run):
    _, res, _ = run
    jag = [d for d in res.docs if d["newspaper"]["paper_id"] == "dainik_jagran"]
    if not jag:
        pytest.skip("no Devanagari font")
    d = next(x for x in jag if "इस्तीफा" in x["newspaper"]["headline"])
    assert d["newspaper"]["translation_status"] == "translated"
    assert d["newspaper"]["original_headline"] == d["newspaper"]["headline"]
    assert d["story"]["n_papers"] == 4 and "Dainik Jagran" in d["summary"]["multi_doc_summary"]
    assert d["event"]["type"] == "Politics.Resignation"                  # enrichment runs on the English text


def test_every_doc_carries_all_sample_fields(run):
    _, res, _ = run
    for d in res.docs:
        assert set(SAMPLE) <= set(d), set(SAMPLE) - set(d)
        assert d["input_quality"]["text_level"] in ("full_body", "headline_only")
    full = [d for d in res.docs if d["input_quality"]["text_level"] == "full_body"]
    assert len(full) >= 10


def test_metrics(run):
    _, res, _ = run
    m = res.metrics
    assert m["papers"] and m["pages"] == 3 * len(m["papers"]) and m["pages_scanned"] == 0
    assert m["articles"] == len(res.docs) and m["mean_ocr_confidence"] == 1.0
    assert m["multi_paper_stories"] >= 3 and m["largest_story_papers"] >= 3
    assert set(m["stage_seconds"]) >= {"ingest", "ocr", "clean", "cluster", "enrich", "total"}


# ----------------------------------------------------------------------------------------------- persistence + export
def test_stage_outputs_persisted(run):
    p, res, _ = run
    assert p.store.count("pages") == res.metrics["pages"]
    assert p.store.count("ocr") == res.metrics["articles"]
    assert p.store.count("enriched") == len(res.docs)
    e = res.docs[0]
    assert p.store.get("enriched", e["event_id"])["event"] == e["event"]
    assert p.store.get("clean", e["event_id"])["story_id"] == e["story"]["story_id"]


def test_export_files(run):
    _, res, tmp = run
    out = tmp / "out"
    files = list((out / "articles").rglob("*.json"))
    assert len(files) == len(res.docs) == res.export["articles"]
    one = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(SAMPLE) <= set(one)
    stories = json.loads((out / "stories.json").read_text(encoding="utf-8"))
    assert stories[0]["n_papers"] >= stories[-1]["n_papers"]               # multi-paper stories first
    assert json.loads((out / "metrics.json").read_text())["articles"] == len(res.docs)
    assert isinstance(json.loads((out / "review.json").read_text()), list)


def test_review_queue_only_lists_real_problems(run):
    _, res, _ = run
    items = review_items(res.docs)
    assert not any(i["reasons"] == ["short_text"] for i in items)
    assert all(i["reasons"] for i in items)


def test_story_view_shape(run):
    _, res, _ = run
    v = story_views(res.docs)[0]
    assert {"story_id", "suggested_story_key", "n_papers", "event_type", "summary", "coverage", "bias_label"} <= set(v)
    c = v["coverage"][0]
    assert {"paper", "page_no", "headline", "ocr_confidence", "event_id", "sentiment"} <= set(c)


# ----------------------------------------------------------------------------------------------- robustness
def test_duplicate_edition_is_skipped(manifest, tmp_path):
    first = manifest.papers[0]
    clone = PaperSpec(id="the_hindu_copy", name="The Hindu (copy)", pdf=first.pdf, reputation="high")
    m = Manifest(manifest.run_date, [first, clone])
    res = make_pipeline(tmp_path).run(m)
    assert res.metrics["pages"] == 3 and {d["newspaper"]["paper_id"] for d in res.docs} == {first.id}


def test_scanned_edition_without_ocr_engine_is_reported_not_fatal(manifest, tmp_path, monkeypatch):
    from newspaper_pipeline.input import splitter
    real = splitter.split_edition

    def fake_split(ed, run_date, out_dir, dpi=300, render=True):
        recs = real(ed, run_date, out_dir, dpi, render)
        if ed.paper.id == "toi":
            for r in recs:
                r.has_text_layer = False
        return recs
    monkeypatch.setattr("newspaper_pipeline.pipeline.split_edition", fake_split)
    res = make_pipeline(tmp_path).run(manifest)
    assert res.metrics["papers_skipped_no_ocr"] == ["toi"] and res.metrics["pages_scanned"] == 3
    assert "toi" not in {d["newspaper"]["paper_id"] for d in res.docs} and res.docs        # others unaffected


def test_scanned_edition_with_engine(manifest, tmp_path, monkeypatch):
    from newspaper_pipeline.input import splitter
    from test_ocr import GeometryEngine
    real = splitter.split_edition

    def fake_split(ed, run_date, out_dir, dpi=300, render=True):
        recs = real(ed, run_date, out_dir, dpi, render)
        for r in recs:
            r.has_text_layer = False
        return recs
    monkeypatch.setattr("newspaper_pipeline.pipeline.split_edition", fake_split)

    class Router:
        """Routes each page to a GeometryEngine for that page (the engine only gets the image)."""
        def read(self, image):
            return self.cur.read(image)

    router = Router()
    from newspaper_pipeline.ocr import run as ocr_run
    real_extract = ocr_run.extract_page

    def extract(page, engine=None, force_ocr=False):
        router.cur = GeometryEngine(page, 40 / 72, conf=0.97)
        return real_extract(page, router, force_ocr)
    monkeypatch.setattr("newspaper_pipeline.ocr.run.extract_page", extract)
    small = Manifest(manifest.run_date, [p for p in manifest.papers if p.id in ("toi", "the_hindu")])
    res = make_pipeline(tmp_path, ocr_engine=router).run(small)
    assert res.metrics["pages_scanned"] == 6 and 0.9 < res.metrics["mean_ocr_confidence"] < 1.0
    assert all(d["newspaper"]["ocr_backend"] == "image_ocr" for d in res.docs)
    assert any(d["story"]["n_papers"] == 2 for d in res.docs)          # still clustered across the two scanned papers
    assert next(d for d in res.docs if d["newspaper"]["paper_id"] == "the_hindu" and "Punjab MLA" in d["newspaper"]["headline"])["newspaper"]["pages"] == [1, 3]


def test_rerun_is_idempotent_in_store(manifest, tmp_path):
    p = make_pipeline(tmp_path)
    a = p.run(manifest)
    b = p.run(manifest)
    assert p.store.count("enriched") == len(a.docs) == len(b.docs)
    assert {d["event_id"] for d in a.docs} == {d["event_id"] for d in b.docs}
    assert {d["story"]["story_id"] for d in a.docs}


def test_trends_accumulate_across_days(manifest, tmp_path):
    p = make_pipeline(tmp_path)
    for day in (1, 2, 3):
        m = Manifest(dt.date(2026, 11, day), manifest.papers[:3])
        # reuse same PDFs; only the run date (and so the counters' day) changes
        p.clusterer = None
        p.run(m)
    last = p.run(Manifest(dt.date(2026, 11, 4), manifest.papers[:3]))
    states = {d["trend_novelty"]["burst_status"] for d in last.docs}
    assert states <= {"normal", "burst"} and "baseline_building" not in states     # 3 prior days of history now


# ----------------------------------------------------------------------------------------------- LLM refiner
class FakeClaude:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("R", (), {"content": [type("C", (), {"text": self.reply})]})


def _one_doc(run):
    return json.loads(json.dumps(next(d for d in run[1].docs if d["event"]["type"] == "Politics.Resignation")))


def test_llm_refiner_merges_valid_fields_only(run):
    d = _one_doc(run)
    before = d["entities"]
    reply = json.dumps({"event": {"type": "Politics.Defection", "trigger": "quits", "actor": "K. V. P. Singh", "bogus": 1},
                        "keyphrases": ["Punjab politics", "AAP exodus"], "one_line": "x" * 400,
                        "tone": "factual / hard news", "entity_stances": [{"entity": "AAP", "stance": "angry"}],
                        "relations": [["a", "b"]]})
    out = LlmRefiner(FakeClaude("```json\n" + reply + "\n```")).refine(d)
    assert out["event"]["type"] == "Politics.Defection" and out["event"]["actor"] == "K. V. P. Singh"
    assert "bogus" not in out["event"] and out["event"]["date"] == d["event"]["date"]       # unlisted keys never merged
    assert out["keyphrases"] == ["Punjab politics", "AAP exodus"] and out["emotion_tone"]["tone"] == "factual / hard news"
    assert len(out["summary"]["one_line"]) < 300                      # 400-char one_line rejected
    assert out["sentiment"]["entity_level"] != [{"entity": "AAP", "stance": "angry"}]       # invalid stance rejected
    assert out["relations"] != [["a", "b"]] and out["entities"] == before
    assert "refined: event, keyphrases, tone" in out["provenance"]["llm"]


@pytest.mark.parametrize("reply", ["not json at all", "[1,2,3]", RuntimeError("api down")])
def test_llm_refiner_failure_keeps_draft(run, reply):
    d = _one_doc(run)
    snapshot = json.dumps({k: v for k, v in d.items() if k != "provenance"}, sort_keys=True)
    out = LlmRefiner(FakeClaude(reply)).refine(d)
    assert json.dumps({k: v for k, v in out.items() if k != "provenance"}, sort_keys=True) == snapshot
    assert "refine_failed" in out["provenance"]["llm"]


def test_llm_refiner_in_pipeline(manifest, tmp_path):
    fake = FakeClaude(json.dumps({"keyphrases": ["Refined phrase"]}))
    res = make_pipeline(tmp_path, refiner=LlmRefiner(fake)).run(Manifest(manifest.run_date, manifest.papers[:2]))
    assert len(fake.prompts) == len(res.docs) and all(d["keyphrases"] == ["Refined phrase"] for d in res.docs)
    assert "HEADLINE:" in fake.prompts[0]


# ----------------------------------------------------------------------------------------------- CLI
def test_cli_demo_and_run(tmp_path, capsys, manifest):
    rc = cli_main(["demo", "--out", str(tmp_path / "o"), "--data-dir", str(tmp_path / "d")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "multi_paper_stories" in out and "wrote" in out
    assert (tmp_path / "o" / "stories.json").exists() and (tmp_path / "d" / "pipeline.sqlite").exists()
    # `run` with a YAML manifest
    import yaml
    mf = tmp_path / "m.yaml"
    mf.write_text(yaml.safe_dump({"run_date": "2026-11-02", "papers": [
        {"id": p.id, "name": p.name, "language": p.language, "pdf": p.pdf} for p in manifest.papers[:2]]}))
    assert cli_main(["run", "--manifest", str(mf), "--out", str(tmp_path / "o2"), "--data-dir", str(tmp_path / "d2")]) == 0
    assert len(list((tmp_path / "o2" / "articles").rglob("*.json"))) > 0


def test_cluster_with_qdrant_index_matches_memory(manifest, tmp_path):
    pytest.importorskip("qdrant_client")
    from newspaper_pipeline.cluster.clusterer import StoryClusterer
    from newspaper_pipeline.cluster.embed import HashingEmbedder
    from newspaper_pipeline.cluster.index import QdrantIndex
    e = HashingEmbedder(dim=1024)
    mem = make_pipeline(tmp_path / "a", clusterer=StoryClusterer(HashingEmbedder(dim=1024))).run(manifest)
    qd = make_pipeline(tmp_path / "b", clusterer=StoryClusterer(e, QdrantIndex(e.dim))).run(manifest)
    groups = lambda r: sorted(sorted(d["event_id"] for d in r.docs if d["story"]["story_id"] == s)
                              for s in {d["story"]["story_id"] for d in r.docs})
    assert groups(mem) == groups(qd)


# ----------------------------------------------------------------------------------------------- real OCR, whole pipeline
def test_real_ocr_whole_pipeline(manifest, tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    from newspaper_pipeline.ocr.backends import RapidOcrEngine
    small = Manifest(manifest.run_date, [p for p in manifest.papers if p.id in ("toi", "indian_express")])
    s = Settings(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "p.sqlite"), render_dpi=150)
    p = Pipeline(s, Store(s.db_path), ocr_engine=RapidOcrEngine())
    from newspaper_pipeline.input import splitter
    real = splitter.split_edition

    def scan(ed, run_date, out_dir, dpi=300, render=True):
        recs = real(ed, run_date, out_dir, dpi, render)
        for r in recs:
            r.has_text_layer = False                                 # force real OCR on the rendered images
        return recs
    import newspaper_pipeline.pipeline as pl
    orig, pl.split_edition = pl.split_edition, scan
    try:
        res = p.run(small)
    finally:
        pl.split_edition = orig
    assert res.metrics["pages_scanned"] == 6 and res.metrics["mean_ocr_confidence"] > 0.85
    resign = [d for d in res.docs if d["event"]["type"] == "Politics.Resignation"]
    assert len(resign) == 2 and len({d["story"]["story_id"] for d in resign}) == 1       # matched from OCR'd pixels alone
    assert all(d["geo"]["primary_location"] == "Punjab" for d in resign)
