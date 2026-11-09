"""End-to-end orchestration. Stages hand off through the Store (so each can be re-run or inspected on its own):

    ingest  -> pages            (fetch PDFs, split into pages + images)
    ocr     -> ocr              (layout, OCR, stitching, quality gate)
    clean   -> clean            (cleanup, language, translation, canonical record)
    cluster -> clean (+story)   (cross-paper dedup / story clustering)
    enrich  -> enriched         (per-article + per-story enrichment, sample.json schema)
    export  -> JSON files, stories.json, review.json, metrics.json
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
from .output.export import export_all
from .store import make_store

log = logging.getLogger("np.pipeline")


@dataclass
class RunResult:
    docs: list[dict]
    metrics: dict
    export: dict = field(default_factory=dict)


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
    def run(self, manifest: Manifest, out_dir: str | Path | None = None) -> RunResult:
        t0 = time.time()
        self.on_stage("ingest"); pages = self.ingest(manifest)
        self.on_stage("ocr"); recs = self.ocr(pages)
        self.on_stage("clean"); arts = self.clean(recs)
        self.on_stage("cluster"); arts = self.cluster(arts)
        self.on_stage("enrich"); docs = self.enrich(arts, manifest.run_date.isoformat())
        graph_summary = None
        if self.graph:
            self.on_stage("graph")
            with self._timed("graph"):
                graph_summary = self.graph.write(docs)
        self.on_stage("export")
        metrics = self.metrics(manifest, pages, recs, docs)
        metrics["graph"] = graph_summary
        metrics["stage_seconds"] = {**self.timings, "total": round(time.time() - t0, 3)}
        exp = export_all(docs, out_dir, metrics) if out_dir else {}
        return RunResult(docs, metrics, exp)

    def close(self) -> None:
        """Release connections (Postgres, Neo4j, Mongo). Safe to call twice."""
        for obj in (getattr(self.clusterer, "index", None), self.graph, self.store):
            if obj is not None and hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    log.exception("error while closing %s", type(obj).__name__)

    def metrics(self, manifest: Manifest, pages: list[PageRecord], recs: list[dict], docs: list[dict]) -> dict:
        stories: dict[str, set] = {}
        for d in docs:
            stories.setdefault(d["story"]["story_id"], set()).add(d["newspaper"]["paper_id"])
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
            "translated": sum(d["newspaper"]["translation_status"] == "translated" for d in docs),
            "stories": len(stories),
            "multi_paper_stories": sum(len(p) > 1 for p in stories.values()),
            "largest_story_papers": max((len(p) for p in stories.values()), default=0),
            "borderline_matches": sum(bool(d["dedup_clustering"].get("review_candidates")) for d in docs),
        }
