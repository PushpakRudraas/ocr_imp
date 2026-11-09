"""HTTP API for the upload frontend.

    uvicorn newspaper_pipeline.api.server:app --port 8000        (env: NP_WORKDIR, NP_OCR, NP_TRANSLATOR, ...)

POST /api/runs           multipart: run_date, papers (JSON list, one entry per file, same order), files[]  -> 202 {id}
GET  /api/runs           list runs (newest first)
GET  /api/runs/{id}      status / stage / metrics / error
GET  /api/runs/{id}/stories | /review | /articles/{event_id}
DELETE /api/runs/{id}
GET  /api/config         what this server can do (OCR engine, translator, limits)

Each run gets its own folder (uploads, data, out, run.json) so runs never interfere and survive restarts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
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

from ..config import Settings
from ..input.manifest import REPUTATIONS, Manifest, PaperSpec
from ..pipeline import Pipeline

STAGES = ["ingest", "ocr", "clean", "cluster", "enrich", "export"]
MAX_PAPERS = int(os.getenv("NP_MAX_PAPERS", "10"))
MAX_UPLOAD_MB = int(os.getenv("NP_MAX_UPLOAD_MB", "250"))
_LANG = re.compile(r"^[a-z]{2,3}$")
_EVENT_ID = re.compile(r"^[0-9a-f]{16,64}$")
_RUN_ID = re.compile(r"^[0-9a-f]{12}$")
_STORY_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "paper"


def default_components() -> argparse.Namespace:
    """Backends from the environment; OCR defaults to RapidOCR when it is installed (needed only for scanned PDFs)."""
    ocr = os.getenv("NP_OCR", "auto")
    if ocr == "auto":
        try:
            import rapidocr_onnxruntime  # noqa: F401
            ocr = "rapidocr"
        except Exception:
            ocr = "none"
    return argparse.Namespace(ocr=ocr, ocr_lang=os.getenv("NP_OCR_LANG", "eng"),
                              translator=os.getenv("NP_TRANSLATOR", "none"), embedder=os.getenv("NP_EMBEDDER", "hashing"),
                              store=os.getenv("NP_STORE", "sqlite"), index=os.getenv("NP_INDEX", "memory"),
                              graph=os.getenv("NP_GRAPH", "none"), qdrant_path=None, judge=os.getenv("NP_JUDGE", "none"),
                              llm_refine=os.getenv("NP_LLM_REFINE", "") == "1", models=os.getenv("NP_ENRICH_MODELS", "none"))


class RunManager:
    def __init__(self, workdir: Path, make_pipeline: Callable[[Settings, Callable[[str], None]], Pipeline] | None = None,
                 sync: bool = False) -> None:
        self.root = Path(workdir) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sync = sync
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1)         # one pipeline at a time: OCR/models are heavy
        self._args = default_components()
        self._shared: dict = {}
        self._make = make_pipeline or self._default_pipeline
        self._recover_interrupted()

    # ---------------------------------------------------------------------------------------- components
    def _default_pipeline(self, settings: Settings, on_stage) -> Pipeline:
        from ..cli import build_pipeline
        p = build_pipeline(self._args, settings, self._shared)      # engines are cached across runs
        p.on_stage = on_stage
        return p

    def stages(self) -> list[str]:
        return STAGES[:-1] + ["graph", "export"] if self._args.graph == "neo4j" else list(STAGES)

    def capabilities(self) -> dict:
        a = self._args
        return {"ocr": a.ocr, "translator": a.translator, "embedder": a.embedder, "index": a.index, "store": a.store,
                "graph": a.graph, "enrich_models": a.models, "max_upload_mb": MAX_UPLOAD_MB, "max_papers": MAX_PAPERS, "stages": self.stages()}

    # ---------------------------------------------------------------------------------------- persistence
    def _dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise HTTPException(404, "unknown run")
        return self.root / run_id

    def _write(self, run: dict) -> None:
        d = self.root / run["id"]
        tmp = d / "run.json.tmp"
        tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / "run.json")

    def get(self, run_id: str) -> dict:
        f = self._dir(run_id) / "run.json"
        if not f.exists():
            raise HTTPException(404, "unknown run")
        return json.loads(f.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        runs = []
        for f in self.root.glob("*/run.json"):
            try:
                runs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def _recover_interrupted(self) -> None:
        for r in self.list():
            if r["status"] in ("queued", "running"):
                r.update(status="failed", error="server restarted while this run was in progress")
                self._write(r)

    # ---------------------------------------------------------------------------------------- creation
    def create(self, run_date: str, papers_json: str, files: list[UploadFile]) -> dict:
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
               "papers": specs, "metrics": None, "error": None, "finished_at": None}
        self._write(run)
        (self._sync_run if self.sync else self._submit)(run_id)
        return self.get(run_id)

    # ---------------------------------------------------------------------------------------- execution
    def _submit(self, run_id: str) -> None:
        self._pool.submit(self._sync_run, run_id)

    def _sync_run(self, run_id: str) -> None:
        run = self.get(run_id)
        d = self.root / run_id

        def on_stage(name: str) -> None:
            run["stage"] = name
            self._write(run)
        run.update(status="running", stage=STAGES[0])
        self._write(run)
        try:
            settings = Settings(data_dir=str(d / "data"), db_path=str(d / "data" / "pipeline.sqlite"), render_dpi=150)
            manifest = Manifest(dt.date.fromisoformat(run["run_date"]), [
                PaperSpec(id=p["id"], name=p["name"], language=p["language"], edition=p["edition"],
                          reputation=p["reputation"], pdf=str(d / "uploads" / f"{p['id']}.pdf")) for p in run["papers"]])
            pipeline = self._make(settings, on_stage)
            try:
                res = pipeline.run(manifest, d / "out")
            finally:
                pipeline.close()
            run.update(status="done", stage="done", metrics=res.metrics)
        except Exception as e:                                   # the run fails, the server does not
            run.update(status="failed", error=f"{type(e).__name__}: {e}")
        run["finished_at"] = time.time()
        self._write(run)

    # ---------------------------------------------------------------------------------------- results
    def _result(self, run_id: str, name: str):
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        f = self.root / run_id / "out" / name
        if not f.exists():
            raise HTTPException(404, f"{name} not found")
        return json.loads(f.read_text(encoding="utf-8"))

    def article(self, run_id: str, event_id: str) -> dict:
        if not _EVENT_ID.match(event_id):
            raise HTTPException(404, "unknown article")
        self.get(run_id)
        hits = list((self.root / run_id / "out" / "articles").glob(f"*/{event_id[:16]}.json"))
        if not hits:
            raise HTTPException(404, "unknown article")
        return json.loads(hits[0].read_text(encoding="utf-8"))

    def story_graph(self, run_id: str, story_id: str) -> dict:
        if not _STORY_ID.match(story_id):
            raise HTTPException(404, "unknown story")
        run = self.get(run_id)
        if run["status"] != "done":
            raise HTTPException(409, f"run is {run['status']}")
        if self._args.graph != "neo4j":
            raise HTTPException(404, "the relationship graph is not enabled on this server (NP_GRAPH=neo4j)")
        with self._lock:
            if "graph" not in self._shared:
                from ..graph.neo4j_graph import Neo4jGraph
                s = Settings()
                self._shared["graph"] = Neo4jGraph(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
        try:
            return self._shared["graph"].story_graph(story_id)
        except Exception as e:
            raise HTTPException(502, f"graph database error: {type(e).__name__}")

    def delete(self, run_id: str) -> None:
        run = self.get(run_id)
        if run["status"] in ("queued", "running"):
            raise HTTPException(409, "run is still in progress")
        shutil.rmtree(self.root / run_id, ignore_errors=True)


def create_app(workdir: str | Path | None = None, make_pipeline=None, sync: bool = False,
               frontend_dist: str | Path | None = None) -> FastAPI:
    mgr = RunManager(Path(workdir or os.getenv("NP_WORKDIR", "data/web")), make_pipeline, sync)
    app = FastAPI(title="Newspaper pipeline API")
    app.state.runs = mgr
    app.add_middleware(CORSMiddleware, allow_origins=os.getenv("NP_CORS", "http://localhost:5173").split(","),
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/config")
    def config():
        return mgr.capabilities()

    @app.post("/api/runs", status_code=202)
    def create_run(run_date: str = Form(...), papers: str = Form(...), files: list[UploadFile] = File(...)):
        return mgr.create(run_date, papers, files)

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
