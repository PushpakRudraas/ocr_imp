"""HTTP API for the upload frontend.

    uvicorn newspaper_pipeline.api.server:app --port 8000        (env: NP_WORKDIR, NP_OCR, NP_TRANSLATOR, ...)

POST /api/runs           multipart: run_date, papers (JSON list, one entry per file, same order), files[],
                         enrich (optional, "true" = also run the enrichment phase after extraction)  -> 202 {id}
POST /api/runs/{id}/enrich   queue the enrichment phase for a run whose extraction is done  -> 202
GET  /api/runs           list runs (newest first)
GET  /api/runs/{id}      status / stage / metrics / error, plus `enrichment` {state, stage, error}
GET  /api/runs/{id}/clean                       phase-1 result: the cleaned + translated articles (available after extraction)
GET  /api/runs/{id}/stories | /review | /articles/{event_id}     phase-2 results (409 until enrichment is done)
DELETE /api/runs/{id}
GET  /api/config         what this server can do (OCR engine, translator, limits, enrichment bus)

Two phases per run, and with NP_BUS=kafka every hop goes through Kafka (this process only serves HTTP and publishes jobs):
  upload -> [jobs.extract] -> extract-worker: PDF -> OCR + layout -> clean + translate -> [events.clean] -> datasync -> MongoDB
  (optional, slow) datasync -> [jobs.enrich] -> enrich-worker: reads clean_events back, cluster + enrich -> [events.enriched]
  -> datasync -> MongoDB enriched_events.
With NP_BUS=inline (default, no broker) this process runs both phases itself, writing to the store directly.

Each run gets its own folder (uploads, data, out, run.json) so runs never interfere and survive restarts.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ..bus import KafkaBus, make_bus
from ..components import default_components
from ..config import Settings
from ..input.manifest import REPUTATIONS, Manifest, PaperSpec
from ..jobs import ad_marks, build_enrich_job, enrich_state, run_enrichment, run_extraction
from ..pipeline import Pipeline
from ..runs import RunNotFound, RunStore

EXTRACT_STAGES = ["ingest", "ocr", "clean"]              # phase 1: ends with clean events stored (MongoDB clean_events)
ENRICH_STAGES = ["cluster", "enrich", "export"]          # phase 2: "graph" is added after "enrich" when Neo4j is on
# With Kafka a run additionally waits in "queued" for a worker and ends in "store" (the data-sync service writing MongoDB).
MAX_PAPERS = int(os.getenv("NP_MAX_PAPERS", "10"))
MAX_UPLOAD_MB = int(os.getenv("NP_MAX_UPLOAD_MB", "250"))
_LANG = re.compile(r"^[a-z]{2,3}$")
_EVENT_ID = re.compile(r"^[0-9a-f]{16,64}$")
_RUN_ID = re.compile(r"^[0-9a-f]{12}$")
_STORY_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")

log = logging.getLogger("np.api")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "paper"


class RunManager:
    def __init__(self, workdir: Path, make_pipeline: Callable[[Settings, Callable[[str], None]], Pipeline] | None = None,
                 sync: bool = False, bus: KafkaBus | None = None) -> None:
        self.store = RunStore(workdir)
        self.root = self.store.root
        self.sync = sync
        self.s = Settings()
        self.bus = bus if bus is not None else make_bus(self.s)         # None = inline: this process runs both phases
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1)         # one pipeline at a time: OCR/models are heavy
        self._args = default_components()
        self._shared: dict = {}
        self._make = make_pipeline or self._default_pipeline                # phase 1 (inline mode only; Kafka -> extract-worker)
        self._make_enrich = make_pipeline or self._enrich_pipeline          # phase 2 (inline mode only; Kafka -> enrich-worker)
        self._recover_interrupted()

    # ---------------------------------------------------------------------------------------- components
    def _default_pipeline(self, settings: Settings, on_stage, phase: str = "extract") -> Pipeline:
        from ..cli import build_pipeline
        p = build_pipeline(self._args, settings, self._shared, phase)      # engines are cached across runs
        p.on_stage = on_stage
        return p

    def _enrich_pipeline(self, settings: Settings, on_stage) -> Pipeline:
        return self._default_pipeline(settings, on_stage, "enrich")

    def stages(self) -> list[str]:
        return ["queued"] + EXTRACT_STAGES + ["store"] if self.bus else list(EXTRACT_STAGES)

    def enrich_stages(self) -> list[str]:
        st = ENRICH_STAGES[:-1] + ["graph", "export"] if self._args.graph == "neo4j" else list(ENRICH_STAGES)
        return st + ["store"] if self.bus else st

    def capabilities(self) -> dict:
        a = self._args
        return {"ocr": a.ocr, "translator": a.translator, "embedder": a.embedder, "index": a.index, "store": a.store,
                "graph": a.graph, "enrich_models": a.models, "max_upload_mb": MAX_UPLOAD_MB, "max_papers": MAX_PAPERS, "stages": self.stages(),
                "enrich_stages": self.enrich_stages(), "enrichment_bus": "kafka" if self.bus else "inline"}

    # ---------------------------------------------------------------------------------------- persistence
    def _write(self, run: dict) -> None:
        self.store.write(run)

    def get(self, run_id: str) -> dict:
        try:
            run = self.store.get(run_id)
        except RunNotFound:
            raise HTTPException(404, "unknown run")
        if "enrichment" not in run:           # runs made before the two-phase flow ran everything in one go
            run["enrichment"] = enrich_state(state="done" if run["status"] == "done" else "off")
        return run

    def list(self) -> list[dict]:
        return [self.get(r["id"]) for r in self.store.list()]

    def _recover_interrupted(self) -> None:
        if self.bus is not None:              # Kafka: the queued message / the worker outlive an API restart
            return
        for r in self.list():
            if r["status"] in ("queued", "running"):
                r.update(status="failed", error="server restarted while this run was in progress")
                self._write(r)
            elif r["enrichment"]["state"] in ("queued", "running"):          # inline mode: the job died with this process
                r["enrichment"].update(state="failed", error="server restarted while enrichment was in progress")
                self._write(r)

    # ---------------------------------------------------------------------------------------- creation
    def create(self, run_date: str, papers_json: str, files: list[UploadFile], enrich: bool = False) -> dict:
        try:
            date = dt.date.fromisoformat(run_date)
        except ValueError:
            raise HTTPException(422, "run_date must be YYYY-MM-DD")
        try:
            meta = json.loads(papers_json)
        except json.JSONDecodeError:
            raise HTTPException(422, "papers must be a JSON list")
        if not isinstance(meta, list) or not meta:
            raise HTTPException(422, "papers must be a non-empty JSON list")
        if len(meta) != len(files):
            raise HTTPException(422, f"{len(meta)} paper entries but {len(files)} files")
        if len(files) > MAX_PAPERS:
            raise HTTPException(422, f"at most {MAX_PAPERS} papers per run")

        used: set[str] = set()
        specs: list[dict] = []
        for m, f in zip(meta, files):
            name = str((m or {}).get("name", "")).strip()
            if not name or len(name) > 80:
                raise HTTPException(422, f"{f.filename}: paper name is required (max 80 chars)")
            lang = str(m.get("language", "en")).lower()
            if not _LANG.match(lang):
                raise HTTPException(422, f"{name}: language must be an ISO code like 'en' or 'hi'")
            rep = m.get("reputation", "not_rated")
            if rep not in REPUTATIONS:
                raise HTTPException(422, f"{name}: reputation must be one of {sorted(REPUTATIONS)}")
            pid, n = slugify(name), 2
            base = pid
            while pid in used:
                pid, n = f"{base}_{n}", n + 1
            used.add(pid)
            specs.append({"id": pid, "name": name, "language": lang, "edition": str(m.get("edition", "main"))[:40] or "main",
                          "reputation": rep, "filename": f.filename or f"{pid}.pdf"})

        run_id = uuid.uuid4().hex[:12]
        d = self.root / run_id
        (d / "uploads").mkdir(parents=True)
        limit = MAX_UPLOAD_MB * 1024 * 1024
        try:
            for spec, f in zip(specs, files):
                dest = d / "uploads" / f"{spec['id']}.pdf"
                size = 0
                with open(dest, "wb") as out:
                    while chunk := f.file.read(1 << 20):
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, f"{spec['filename']} exceeds {MAX_UPLOAD_MB} MB")
                        out.write(chunk)
                with open(dest, "rb") as chk:
                    if chk.read(5) != b"%PDF-":
                        raise HTTPException(400, f"{spec['filename']} is not a PDF")
                spec["size"] = size
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise

        run = {"id": run_id, "created_at": time.time(), "status": "queued", "stage": None, "run_date": date.isoformat(),
               "papers": specs, "metrics": None, "error": None, "finished_at": None,
               "enrich_after": enrich, "enrichment": enrich_state()}
        if self.bus is not None:
            run["stage"] = "queued"
        self._write(run)
        if self.bus is not None:              # Kafka: hand the run to the extract-worker; this process does no pipeline work
            try:
                self.bus.publish(self.s.topic_extract_jobs, {"run_id": run_id, "run_date": run["run_date"], "enrich_after": enrich}, run_id)
            except Exception as e:
                self.store.patch(run_id, status="failed", error=f"could not queue extraction on Kafka: {e}", finished_at=time.time())
        else:
            (self._sync_run if self.sync else self._submit)(run_id)
        return self.get(run_id)

    # ---------------------------------------------------------------------------------------- execution
    def _submit(self, run_id: str) -> None:
        self._pool.submit(self._sync_run, run_id)

    def _sync_run(self, run_id: str) -> None:
        """Inline mode, phase 1: the shared extraction job, then phase 2 if the run asked for it."""
        if run_extraction({"run_id": run_id}, self.store, self._make) and self.get(run_id).get("enrich_after"):
            try:
                self.request_enrichment(run_id)
            except HTTPException as e:
                log.error("run %s: could not start enrichment: %s", run_id, e.detail)

    def request_enrichment(self, run_id: str) -> dict:
        """Phase 2 trigger (the UI toggle, or the button on a finished run): publish an "enrich this run" job.
        The job holds only the run id, date and clean event ids; the worker reads the events back from MongoDB."""
        with self._lock:
            run = self.get(run_id)
            if run["status"] != "done":
                raise HTTPException(409, f"extraction is {run['status']}; enrich after it has finished")
            if run["enrichment"]["state"] in ("queued", "running", "done"):
                raise HTTPException(409, f"enrichment is already {run['enrichment']['state']}")
            clean = self.root / run_id / "out" / "clean_articles.json"
            if not clean.exists():
                raise HTTPException(409, "no cleaned articles stored for this run")
            job = build_enrich_job(self.store, run_id)
            if not job["event_ids"]:
                raise HTTPException(409, "this run has no articles to enrich")
            self._write({**run, "enrichment": enrich_state(state="queued", stage="queued", queued_at=time.time())})
        if self.bus is not None:
            try:
                self.bus.publish(self.s.topic_enrich_jobs, job, run_id)
            except Exception as e:
                self.store.patch(run_id, enrichment=enrich_state(state="failed", error=f"could not queue on Kafka: {e}"))
                raise HTTPException(502, f"could not queue enrichment on Kafka: {e}")
        elif self.sync:
            run_enrichment(job, self.store, self._make_enrich)
        else:
            self._pool.submit(run_enrichment, job, self.store, self._make_enrich)
        return self.get(run_id)

    # ---------------------------------------------------------------------------------------- results
    def _result(self, run_id: str, name: str):
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        if run["enrichment"]["state"] != "done":
            raise HTTPException(409, f"enrichment is {run['enrichment']['state']}")
        f = self.root / run_id / "out" / name
        if not f.exists():
            raise HTTPException(404, f"{name} not found")
        return json.loads(f.read_text(encoding="utf-8"))

    def clean_articles(self, run_id: str) -> list[dict]:
        """Phase-1 result, available as soon as extraction is done (no enrichment needed)."""
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        f = self.root / run_id / "out" / "clean_articles.json"
        if not f.exists():
            raise HTTPException(404, "clean_articles.json not found")
        return json.loads(f.read_text(encoding="utf-8"))

    def ocr_articles(self, run_id: str) -> list[dict]:
        """Every raw OCR result of the run in reading order, kept or filtered out, with the ad label (reviewer marks win)."""
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        f = self.root / run_id / "out" / "ocr_articles.json"
        if not f.exists():
            raise HTTPException(404, "this run has no saved OCR results (it was extracted before they were kept) - upload it again")
        items = json.loads(f.read_text(encoding="utf-8"))
        marks = ad_marks(self.store, run_id)
        for it in items:
            m = marks.get(it["article_id"])
            it["marked"] = m
            if m:
                it["ad_label"] = m                       # a reviewer's call overrides the rules
        return items

    def page_regions(self, run_id: str) -> list[dict]:
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        f = self.root / run_id / "out" / "pages.json"
        if not f.exists():
            raise HTTPException(404, "this run has no saved page layout - upload it again")
        return json.loads(f.read_text(encoding="utf-8"))

    def page_image(self, run_id: str, paper_id: str, page_no: int) -> Path:
        """A small JPEG of one page for the page view, rendered from the uploaded PDF and cached next to the run."""
        run = self.get(run_id)
        if paper_id not in {p["id"] for p in run["papers"]} or not 1 <= page_no <= 500:
            raise HTTPException(404, "unknown page")
        pdf = self.root / run_id / "uploads" / f"{paper_id}.pdf"
        if not pdf.exists():
            raise HTTPException(404, "the uploaded PDF is no longer available")
        out = self.root / run_id / "out" / "page_images" / paper_id / f"{page_no}.jpg"
        if not out.exists():
            import pymupdf as fitz
            with fitz.open(pdf) as doc:
                if page_no > doc.page_count:
                    raise HTTPException(404, "unknown page")
                pix = doc[page_no - 1].get_pixmap(dpi=100)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(pix.tobytes("jpg", jpg_quality=80))
        return out

    def mark_ocr(self, run_id: str, article_id: str, label: str) -> dict:
        """Reviewer override: call an OCR result an ad (kept out of enrichment) or news. Stored per run; raw_events is untouched."""
        if label not in ("ad", "news") or not _EVENT_ID.match(article_id):
            raise HTTPException(400, "label must be 'ad' or 'news'")
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        if run["enrichment"]["state"] in ("queued", "running", "done") and label == "ad":
            raise HTTPException(409, "this run is already enriched - the mark would not change the stored data")
        marks = ad_marks(self.store, run_id)
        marks[article_id] = label
        (self.root / run_id / "out" / "ad_marks.json").write_text(json.dumps(marks), encoding="utf-8")
        return {"article_id": article_id, "label": label}

    def article(self, run_id: str, event_id: str) -> dict:
        if not _EVENT_ID.match(event_id):
            raise HTTPException(404, "unknown article")
        self.get(run_id)
        hits = list((self.root / run_id / "out" / "articles").glob(f"*/{event_id[:16]}.json"))
        if not hits:
            raise HTTPException(404, "unknown article")
        return json.loads(hits[0].read_text(encoding="utf-8"))

    # ---------------------------------------------------------------------------------------- relationship graph (Neo4j)
    def _graph_for(self, run_id: str):
        """The shared Neo4j handle, after checking that the run is finished and the graph backend is on."""
        run = self.get(run_id)
        if run["status"] != "done" or run["enrichment"]["state"] != "done":
            raise HTTPException(409, f"run is {run['status']}, enrichment {run['enrichment']['state']}")
        if self._args.graph != "neo4j":
            raise HTTPException(404, "the relationship graph is not enabled on this server (NP_GRAPH=neo4j)")
        with self._lock:
            if "graph" not in self._shared:
                from ..graph.neo4j_graph import Neo4jGraph
                s = Settings()
                self._shared["graph"] = Neo4jGraph(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
        return self._shared["graph"]

    def _graph_call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"graph database error: {type(e).__name__}")

    def story_graph(self, run_id: str, story_id: str) -> dict:
        if not _STORY_ID.match(story_id):
            raise HTTPException(404, "unknown story")
        g = self._graph_for(run_id)
        return self._graph_call(g.story_graph, story_id)

    def _article_ids(self, run_id: str, scope: str, g) -> list[str]:
        """Which articles the graph view covers: this run's (the default) or the most recent of every run."""
        if scope == "all":
            return self._graph_call(g.run_ids)
        f = self.root / run_id / "out" / "stories.json"
        if not f.exists():
            raise HTTPException(404, "stories.json not found")
        stories = json.loads(f.read_text(encoding="utf-8"))
        return [c["event_id"] for st in stories for c in st["coverage"]]

    @staticmethod
    def _graph_types(types: str | None) -> set[str]:
        from ..graph.neo4j_graph import NODE_SPECS
        if not types:
            return {"Article", "Paper", "Story"}                        # the default view; everything else is switched on by the user
        asked = {t.strip() for t in types.split(",") if t.strip()}
        bad = asked - set(NODE_SPECS)
        if bad:
            raise HTTPException(400, f"unknown node type(s): {', '.join(sorted(bad))}; allowed: {', '.join(NODE_SPECS)}")
        return asked | {"Article"}

    @staticmethod
    def _graph_scope(scope: str) -> str:
        if scope not in ("run", "all"):
            raise HTTPException(400, "scope must be 'run' or 'all'")
        return scope

    def graph_summary(self, run_id: str, scope: str = "run") -> dict:
        scope = self._graph_scope(scope)
        g = self._graph_for(run_id)
        ids = self._article_ids(run_id, scope, g)
        return {"run_id": run_id, "scope": scope, **self._graph_call(g.run_summary, ids)}

    def graph(self, run_id: str, types: str | None, min_score: float, max_nodes: int, scope: str = "run") -> dict:
        scope = self._graph_scope(scope)
        if not 0.0 <= min_score <= 1.0 or not 20 <= max_nodes <= 2000:
            raise HTTPException(400, "min_score must be 0..1 and max_nodes 20..2000")
        wanted = self._graph_types(types)
        g = self._graph_for(run_id)
        ids = self._article_ids(run_id, scope, g)
        return {"run_id": run_id, "scope": scope, "max_nodes": max_nodes, **self._graph_call(g.run_graph, ids, wanted, min_score, max_nodes)}

    def graph_neighbors(self, run_id: str, node: str, types: str | None, limit: int, scope: str = "run") -> dict:
        from ..graph.neo4j_graph import parse_node_id
        scope = self._graph_scope(scope)
        if not parse_node_id(node) or not 1 <= limit <= 200:
            raise HTTPException(400, "node must look like 'Article:<id>' with a known type, and limit must be 1..200")
        wanted = self._graph_types(types) | {"Paper", "Story"}
        g = self._graph_for(run_id)
        ids = None if scope == "all" else self._article_ids(run_id, scope, g)
        return {"run_id": run_id, "scope": scope, **self._graph_call(g.neighbors, node, wanted, ids, limit)}

    def delete(self, run_id: str) -> None:
        run = self.get(run_id)
        if run["status"] in ("queued", "running") or run["enrichment"]["state"] in ("queued", "running"):
            raise HTTPException(409, "run is still in progress")
        shutil.rmtree(self.root / run_id, ignore_errors=True)


def create_app(workdir: str | Path | None = None, make_pipeline=None, sync: bool = False,
               frontend_dist: str | Path | None = None, bus: KafkaBus | None = None) -> FastAPI:
    mgr = RunManager(Path(workdir or os.getenv("NP_WORKDIR", "data/web")), make_pipeline, sync, bus)
    app = FastAPI(title="Newspaper pipeline API")
    app.state.runs = mgr
    app.add_middleware(CORSMiddleware, allow_origins=os.getenv("NP_CORS", "http://localhost:5173").split(","),
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/config")
    def config():
        return mgr.capabilities()

    @app.post("/api/runs", status_code=202)
    def create_run(run_date: str = Form(...), papers: str = Form(...), files: list[UploadFile] = File(...),
                   enrich: bool = Form(False)):
        return mgr.create(run_date, papers, files, enrich)

    @app.post("/api/runs/{run_id}/enrich", status_code=202)
    def enrich_run(run_id: str):
        return mgr.request_enrichment(run_id)

    @app.get("/api/runs/{run_id}/clean")
    def clean(run_id: str):
        return mgr.clean_articles(run_id)

    @app.post("/api/runs/{run_id}/ocr/{article_id}/mark")
    def mark_ocr(run_id: str, article_id: str, body: dict):
        return mgr.mark_ocr(run_id, article_id, str(body.get("label", "")))

    @app.get("/api/runs/{run_id}/pages")
    def pages(run_id: str):
        return mgr.page_regions(run_id)

    @app.get("/api/runs/{run_id}/pages/{paper_id}/{page_no}/image")
    def page_image(run_id: str, paper_id: str, page_no: int):
        return FileResponse(mgr.page_image(run_id, paper_id, page_no), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/runs/{run_id}/ocr")
    def ocr(run_id: str):
        return mgr.ocr_articles(run_id)

    @app.get("/api/runs")
    def list_runs():
        return mgr.list()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return mgr.get(run_id)

    @app.get("/api/runs/{run_id}/stories")
    def stories(run_id: str):
        return mgr._result(run_id, "stories.json")

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: str):
        return mgr._result(run_id, "review.json")

    @app.get("/api/runs/{run_id}/articles/{event_id}")
    def article(run_id: str, event_id: str):
        return mgr.article(run_id, event_id)

    @app.get("/api/runs/{run_id}/graph/summary")
    def graph_summary(run_id: str, scope: str = "run"):
        return mgr.graph_summary(run_id, scope)

    @app.get("/api/runs/{run_id}/graph/neighbors")
    def graph_neighbors(run_id: str, node: str, types: str | None = None, limit: int = 50, scope: str = "run"):
        return mgr.graph_neighbors(run_id, node, types, limit, scope)

    @app.get("/api/runs/{run_id}/graph")
    def run_graph(run_id: str, types: str | None = None, min_score: float = 0.0, max_nodes: int = 300, scope: str = "run"):
        return mgr.graph(run_id, types, min_score, max_nodes, scope)

    @app.get("/api/runs/{run_id}/stories/{story_id}/graph")
    def story_graph(run_id: str, story_id: str):
        return mgr.story_graph(run_id, story_id)

    @app.delete("/api/runs/{run_id}", status_code=204)
    def delete_run(run_id: str):
        mgr.delete(run_id)

    dist = Path(frontend_dist or os.getenv("NP_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
    if dist.exists():                                            # serve the built UI from the same origin
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


app = create_app()
