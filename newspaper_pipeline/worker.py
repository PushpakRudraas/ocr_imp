"""Pipeline workers (one container each, same image):

    python -m newspaper_pipeline.worker extract    OCR + layout + clean + translate   (jobs: newspaper.jobs.extract)
    python -m newspaper_pipeline.worker enrich     cluster + enrich + graph           (jobs: newspaper.jobs.enrich)

Both consume jobs from Kafka and share the `runs/` volume with the API (progress and errors are written to run.json).
Neither writes documents to MongoDB: their store is a `BusStore`, so every page / raw / clean / enriched event is published to
Kafka and the data-sync service stores it. The enrich worker reads the clean events back from MongoDB.
Models and OCR engines load once and are reused by every job."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .bus import KafkaBus, all_topics, make_bus
from .busstore import BusStore
from .components import default_components
from .config import Settings
from .jobs import run_enrichment, run_extraction
from .runs import RunStore

log = logging.getLogger("np.worker")


def main(argv: list[str] | None = None) -> int:
    role = (argv if argv is not None else sys.argv[1:] or ["enrich"])[0]
    if role not in ("extract", "enrich"):
        raise SystemExit("usage: python -m newspaper_pipeline.worker extract|enrich")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings(bus_backend="kafka")
    bus = make_bus(settings, role)
    assert isinstance(bus, KafkaBus)
    runs = RunStore(Path(os.getenv("NP_WORKDIR", "data/web")))
    args, shared = default_components(), {}
    events_topic = settings.topic_clean_events if role == "extract" else settings.topic_enriched_events

    def make(run_id: str):
        def build(s: Settings, on_stage):
            from .cli import build_pipeline
            p = build_pipeline(args, s, shared, role)                    # engines / models cached across jobs
            p.store = BusStore(p.store, bus, events_topic, run_id)       # documents go to Kafka, not to the database
            p.on_stage = on_stage
            return p
        return build

    if role == "extract":
        topic = settings.topic_extract_jobs
        handle = lambda job: run_extraction(job, runs, make(job.get("run_id", "")), bus, events_topic)
    else:
        topic = settings.topic_enrich_jobs
        handle = lambda job: run_enrichment(job, runs, make(job.get("run_id", "")), bus, events_topic)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():                                     # broker may still be starting: retry, do not crash-loop
        try:
            bus.ensure_topics(all_topics(settings))
            bus.consume([topic], lambda msgs: [handle(job) for _t, job in msgs], stop)
        except Exception:
            log.exception("kafka unavailable, retrying in 5s")
            stop.wait(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
