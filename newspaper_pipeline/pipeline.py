"""End-to-end orchestration. Stages hand off through the Store (so each can be re-run or inspected on its own):

    ingest  -> pages            (fetch PDFs, split into pages + images)
    ocr     -> ocr              (layout, OCR, stitching, quality gate)
    clean   -> clean            (cleanup, language, translation, canonical record)
    cluster -> clean (+story)   (cross-paper dedup / story clustering)
    enrich  -> enriched         (per-article + per-story enrichment, sample.json schema)
    export  -> JSON files, stories.json, review.json, metrics.json

The stages form two phases. `extract` (ingest -> ocr -> clean) ends with the cleaned, translated events stored in the
Store (MongoDB `clean_events`); it is cheap enough to run immediately. `enrich_stored` (cluster -> enrich -> graph ->
export) is the slow ML phase: it reads those stored clean events back and writes `enriched_events`, so it can run later
and elsewhere (see jobs.py / worker.py, driven over Kafka). `run` chains both, as before, unless `enrich=False`.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .clean.filter import filter_raw_events
from .clean.normalize import clean_article, mark_exact_duplicates
from .clean.translate import TranslationBackend
from .cluster.clusterer import StoryClusterer
from .config import Settings
from .enrich.llm import LlmRefiner
from .enrich.pipeline import enrich_article, enrich_stories
from .input.fetch import fetch_editions
from .input.manifest import Manifest
from .input.splitter import PageRecord, split_edition
from .ocr.backends import OcrEngine
from .ocr.run import OcrUnavailable, extract_edition
from .output.export import export_all, export_clean
from .store import make_store

log = logging.getLogger("np.pipeline")


@dataclass
class RunResult:
    docs: list[dict]
    metrics: dict
    export: dict = field(default_factory=dict)


@dataclass
class Extraction:
    """What phase 1 produced: pages, raw OCR events and the cleaned events now sitting in the store."""
    pages: list
    recs: list[dict]
    arts: list[dict]


class Pipeline:
    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 translator: TranslationBackend | None = None, ocr_engine: OcrEngine | None = None,
                 clusterer: StoryClusterer | None = None, fallback=None, refiner: LlmRefiner | None = None,
                 render_pages: bool = True, on_stage=None, graph=None, models=None) -> None:
        self.s = settings or Settings()
        self.store = store or make_store(self.s)
        self.graph = graph                                   # optional Neo4jGraph (relationship graph)
        self.translator, self.ocr_engine, self.fallback, self.refiner = translator, ocr_engine, fallback, refiner
        self.clusterer = clusterer
        self.models = models                                 # enrich.models.HfModels or None (rule-based enrichment)
        self.render_pages = render_pages
        self.on_stage = on_stage or (lambda name: None)      # progress hook: called with the stage about to run
        self.timings: dict[str, float] = {}
        self.device: str | None = None                       # "cuda" | "cpu": where the enrichment models run (set by build_pipeline)

    def _timed(self, name: str):
        class _T:
            def __enter__(_s): _s.t = time.time()
            def __exit__(_s, *a): self.timings[name] = round(time.time() - _s.t, 3)
        return _T()

    # ------------------------------------------------------------------------------------------- stages
    def ingest(self, manifest: Manifest) -> list[PageRecord]:
        pages: list[PageRecord] = []
        with self._timed("ingest"):
            for ed in fetch_editions(manifest, self.s.data_dir):
                if ed.duplicate_of:
                    log.warning("%s is byte-identical to %s - skipping", ed.paper.id, ed.duplicate_of)
                    continue
                pages += split_edition(ed, manifest.run_date, self.s.data_dir, self.s.render_dpi, self.render_pages)
        self.store.put_many("pages", [{**p.to_dict(), "date": p.date, "key": p.page_id} for p in pages], "key")
        return pages

    def ocr(self, pages: list[PageRecord]) -> list[dict]:
        recs: list[dict] = []
        skipped = []
        with self._timed("ocr"):
            by_paper: dict[str, list[PageRecord]] = {}
            for p in pages:
                by_paper.setdefault(p.paper_id, []).append(p)
            for paper_id, ps in by_paper.items():
                try:
                    recs += extract_edition(ps, self.s, self.ocr_engine, self.fallback)
                except OcrUnavailable as e:
                    log.error("%s: %s", paper_id, e)
                    skipped.append(paper_id)
        self.skipped_papers = skipped
        self.store.put_many("ocr", recs, "article_id")
        return recs

    def clean(self, recs: list[dict]) -> list[dict]:
        with self._timed("clean"):
            ingested_ms = time.time() * 1000
            filtered = filter_raw_events(recs)
            self.clean_filter = {**filtered.stats(), "removed_items": filtered.removed}
            if filtered.removed:
                log.info("clean: removed %d of %d raw events %s", len(filtered.removed), len(recs),
                         self.clean_filter["by_reason"])
            out = [clean_article(r, self.translator, self.s, ingested_ms) for r in filtered.kept]
            mark_exact_duplicates(out)
        self.store.put_many("clean", out)
        return out

    def cluster(self, arts: list[dict]) -> list[dict]:
        with self._timed("cluster"):
            cl = self.clusterer or StoryClusterer(settings=self.s)
            cl.cluster_all(arts)
            self.clusterer = cl
        self.store.put_many("clean", arts)
        return arts

    def enrich(self, arts: list[dict], day: str | None = None) -> list[dict]:
        with self._timed("enrich"):
            docs = [enrich_article(a, self.models) for a in arts]
            docs = enrich_stories(docs, self.store, day)
            if self.refiner:
                docs = [self.refiner.refine(d) for d in docs]
        self.store.put_many("enriched", docs)
        return docs

    # ----------------------------------------------------------------------------------------------- run
    def extract(self, manifest: Manifest) -> Extraction:
        """Phase 1: PDFs -> OCR + layout -> clean + translate -> stored `clean` events. No models beyond OCR/translation."""
        self.on_stage("ingest"); pages = self.ingest(manifest)
        self.on_stage("ocr"); recs = self.ocr(pages)
        self.on_stage("clean"); arts = self.clean(recs)
        return Extraction(pages, recs, arts)

    def enrich_stored(self, day: str, event_ids: list[str] | None = None, out_dir: str | Path | None = None,
                      base_metrics: dict | None = None) -> RunResult:
        """Phase 2: read the stored clean events (those in `event_ids`, else every one of `day`) back from the store,
        then cluster -> enrich -> graph -> export. Writes `enriched` events. `base_metrics` = phase-1 metrics to extend."""
        t0 = time.time()
        if event_ids is not None:
            arts = [a for a in (self.store.get("clean", e) for e in event_ids) if a is not None]
            if len(arts) != len(event_ids):
                log.warning("enrich: %d of %d clean events are missing from the store", len(event_ids) - len(arts), len(event_ids))
        else:
            arts = self.store.all("clean", day)
        if not arts:
            raise ValueError("no clean events to enrich - run the extraction phase first")
        self.on_stage("cluster"); arts = self.cluster(arts)
        self.on_stage("enrich"); docs = self.enrich(arts, day)
        graph_summary = None
        if self.graph:
            self.on_stage("graph")
            with self._timed("graph"):
                graph_summary = self.graph.write(docs)
        self.on_stage("export")
        base = dict(base_metrics or {})
        metrics = {**base, **self.enrichment_metrics(docs), "graph": graph_summary, "enrich_device": self.device,
                   "stage_seconds": {**(base.get("stage_seconds") or {}), **self.timings}}
        metrics["stage_seconds"]["total"] = round(metrics["stage_seconds"].get("total", 0) + time.time() - t0, 3)
        exp = export_all(docs, out_dir, metrics) if out_dir else {}
        return RunResult(docs, metrics, exp)

    def run(self, manifest: Manifest, out_dir: str | Path | None = None, enrich: bool = True) -> RunResult:
        """Both phases back to back (CLI/demo). `enrich=False` stops after phase 1 and returns phase-1 metrics only."""
        t0 = time.time()
        ex = self.extract(manifest)
        metrics = self.extraction_metrics(manifest, ex.pages, ex.recs, ex.arts)
        metrics["stage_seconds"] = {**self.timings, "total": round(time.time() - t0, 3)}
        if out_dir:
            export_clean(ex.arts, out_dir)
        if not enrich:
            if out_dir:
                export_all([], out_dir, metrics)
            return RunResult([], metrics, {"clean": len(ex.arts)})
        return self.enrich_stored(manifest.run_date.isoformat(), [a["event_id"] for a in ex.arts], out_dir, metrics)

    def close(self) -> None:
        """Release connections (Postgres, Neo4j, Mongo). Safe to call twice."""
        for obj in (getattr(self.clusterer, "index", None), self.graph, self.store):
            if obj is not None and hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    log.exception("error while closing %s", type(obj).__name__)

    def extraction_metrics(self, manifest: Manifest, pages: list[PageRecord], recs: list[dict], arts: list[dict]) -> dict:
        confs = [r["ocr_confidence"] for r in recs]
        return {
            "run_date": manifest.run_date.isoformat(),
            "papers": [p.id for p in manifest.papers],
            "papers_skipped_no_ocr": getattr(self, "skipped_papers", []),
            "clean_filter": getattr(self, "clean_filter", None),
            "pages": len(pages),
            "pages_scanned": sum(not p.has_text_layer for p in pages),
            "articles": len(recs),
            "mean_ocr_confidence": round(statistics.mean(confs), 4) if confs else None,
            "articles_flagged_review": sum(r["status"] == "review" for r in recs),
            "translated": sum(a.get("translation_status") == "translated" for a in arts),
        }

    @staticmethod
    def enrichment_metrics(docs: list[dict]) -> dict:
        stories: dict[str, set] = {}
        for d in docs:
            stories.setdefault(d["story"]["story_id"], set()).add(d["newspaper"]["paper_id"])
        return {
            "stories": len(stories),
            "multi_paper_stories": sum(len(p) > 1 for p in stories.values()),
            "largest_story_papers": max((len(p) for p in stories.values()), default=0),
            "borderline_matches": sum(bool(d["dedup_clustering"].get("review_candidates")) for d in docs),
        }
