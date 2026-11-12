"""The two phases as jobs. Each runs in one of two modes:

* inline (no `bus`): the caller (the API process) writes the result straight to the store and marks the run done.
* kafka (`bus` given): the pipeline's store is a `BusStore`, so every document it produces is already on Kafka; the job
  then publishes an `extract_done` / `enrich_done` marker and leaves the run in stage "store". The data-sync service
  stores the documents in MongoDB and only then flips the run to done (see datasync.py), so "done" always means "stored".

Neither function raises for a bad job: the outcome is recorded on the run so the UI can show it."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Callable

from .bus import KafkaBus
from .config import Settings
from .input.manifest import Manifest, PaperSpec
from .pipeline import Pipeline
from .runs import RunNotFound, RunStore

log = logging.getLogger("np.jobs")

MakePipeline = Callable[[Settings, Callable[[str], None]], Pipeline]


def enrich_state(**kw) -> dict:
    """The `enrichment` block of run.json. state: off | queued | running | done | failed."""
    return {"state": "off", "stage": None, "error": None, "queued_at": None, "started_at": None, "finished_at": None, **kw}


def clean_event_ids(runs: RunStore, run_id: str) -> list[str]:
    f = runs.dir(run_id) / "out" / "clean_articles.json"
    return [a["event_id"] for a in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []


def build_enrich_job(runs: RunStore, run_id: str) -> dict:
    """The "enrich this run" message: pointers only (run id, date, clean event ids). The worker reads the data from MongoDB."""
    return {"run_id": run_id, "run_date": runs.get(run_id)["run_date"], "event_ids": clean_event_ids(runs, run_id)}


def _settings(runs: RunStore, run_id: str) -> Settings:
    d = runs.dir(run_id)
    return Settings(data_dir=str(d / "data"), db_path=str(d / "data" / "pipeline.sqlite"), render_dpi=150)


# ===================================================================================================== phase 1
def run_extraction(job: dict, runs: RunStore, make_pipeline: MakePipeline, bus: KafkaBus | None = None,
                   events_topic: str | None = None) -> bool:
    """PDF -> OCR + layout -> clean + translate. Returns True if the extraction succeeded."""
    run_id = job.get("run_id", "")
    try:
        run = runs.get(run_id)
    except RunNotFound:
        log.error("extraction job for unknown run %r - dropped", run_id)
        return False
    if run["status"] not in ("queued", "running"):               # redelivered after it finished (or failed): nothing to redo
        log.info("run %s is already %s - skipping extraction job", run_id, run["status"])
        return False

    d = runs.dir(run_id)
    runs.patch(run_id, status="running", stage="ingest", error=None)
    pipeline = None
    try:
        manifest = Manifest(dt.date.fromisoformat(run["run_date"]), [
            PaperSpec(id=p["id"], name=p["name"], language=p["language"], edition=p["edition"],
                      reputation=p["reputation"], pdf=str(d / "uploads" / f"{p['id']}.pdf")) for p in run["papers"]])
        pipeline = make_pipeline(_settings(runs, run_id), lambda name: runs.patch(run_id, stage=name))
        res = pipeline.run(manifest, d / "out", enrich=False)
        if bus is not None:
            runs.patch(run_id, metrics=res.metrics, stage="store")
            bus.publish(events_topic, {"type": "extract_done", "run_id": run_id, "event_ids": clean_event_ids(runs, run_id),
                                       "enrich_after": bool(run.get("enrich_after"))}, run_id)
        else:
            runs.patch(run_id, status="done", stage="done", metrics=res.metrics, finished_at=time.time())
        return True
    except Exception as e:                                       # the run fails, the worker does not
        log.exception("run %s: extraction failed", run_id)
        runs.patch(run_id, status="failed", error=f"{type(e).__name__}: {e}", finished_at=time.time())
        return False
    finally:
        if pipeline is not None:
            pipeline.close()


# ===================================================================================================== phase 2
def run_enrichment(job: dict, runs: RunStore, make_pipeline: MakePipeline, bus: KafkaBus | None = None,
                   events_topic: str | None = None) -> None:
    """Enrich one run: its clean events are read back from the store, enriched, and written as `enriched` events."""
    run_id = job.get("run_id", "")
    try:
        run = runs.get(run_id)
    except RunNotFound:
        log.error("enrichment job for unknown run %r - dropped", run_id)
        return
    if run["status"] != "done":
        log.error("run %s: extraction is %s, not enriching", run_id, run["status"])
        return
    cur = run.get("enrichment") or enrich_state()
    if cur["state"] == "done":
        log.info("run %s is already enriched - skipping redelivered job", run_id)
        return

    def on_stage(name: str) -> None:
        runs.patch(run_id, enrichment={**runs.get(run_id)["enrichment"], "state": "running", "stage": name})

    runs.patch(run_id, enrichment={**cur, "state": "running", "stage": "cluster", "error": None, "started_at": time.time()})
    pipeline = None
    try:
        pipeline = make_pipeline(_settings(runs, run_id), on_stage)
        res = pipeline.enrich_stored(job["run_date"], job.get("event_ids"), runs.dir(run_id) / "out", run.get("metrics"))
        if bus is not None:
            runs.patch(run_id, enrichment={**runs.get(run_id)["enrichment"], "state": "running", "stage": "store"})
            bus.publish(events_topic, {"type": "enrich_done", "run_id": run_id, "event_ids": [d["event_id"] for d in res.docs],
                                       "metrics": res.metrics}, run_id)
        else:
            runs.patch(run_id, metrics=res.metrics,
                       enrichment={**runs.get(run_id)["enrichment"], "state": "done", "stage": "done", "finished_at": time.time()})
    except Exception as e:                                       # the job fails, the worker does not
        log.exception("run %s: enrichment failed", run_id)
        runs.patch(run_id, enrichment={**runs.get(run_id)["enrichment"], "state": "failed",
                                       "error": f"{type(e).__name__}: {e}", "finished_at": time.time()})
    finally:
        if pipeline is not None:
            pipeline.close()
