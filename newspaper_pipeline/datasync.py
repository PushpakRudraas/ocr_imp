"""Data sync: the only service that writes pipeline documents to MongoDB.

    python -m newspaper_pipeline.datasync          (env: NP_KAFKA_*, NP_MONGO_URI, NP_WORKDIR)

It consumes `newspaper.events.clean` and `newspaper.events.enriched`:

* `doc` messages (pages, raw_events, clean_events, enriched_events) are upserted into the store, in batches;
* an `extract_done` marker means the run's clean events are all in the store: after checking they really are, the run is
  marked done (extraction finished AND stored) and, if the run asked for enrichment, the enrichment job is published;
* an `enrich_done` marker does the same for the enriched events and marks the enrichment done.

If the database is unreachable the batch is not committed: the consumer restarts and Kafka redelivers it, so no document is lost."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

from .bus import KafkaBus, Message, all_topics, make_bus
from .busstore import DOC
from .config import Settings
from .jobs import build_enrich_job, enrich_state
from .runs import RunNotFound, RunStore

log = logging.getLogger("np.datasync")


class DataSync:
    def __init__(self, store, runs: RunStore, bus: KafkaBus, settings: Settings | None = None) -> None:
        self.store, self.runs, self.bus = store, runs, bus
        self.s = settings or Settings()

    def handle(self, msgs: list[Message]) -> None:
        pending: dict[str, list[tuple[str, dict]]] = {}

        def flush() -> None:
            for stage, items in pending.items():
                self.store.put_keyed_many(stage, items)
            pending.clear()
        for _topic, m in msgs:
            kind = m.get("type")
            if kind == DOC:
                pending.setdefault(m["stage"], []).append((m["key"], m["doc"]))
            elif kind in ("extract_done", "enrich_done"):
                flush()                                   # the marker is only meaningful once the documents before it are stored
                try:
                    (self._extract_done if kind == "extract_done" else self._enrich_done)(m)
                except RunNotFound:
                    log.error("%s for unknown run %r - dropped", kind, m.get("run_id"))
            else:
                log.warning("unknown message type %r - dropped", kind)
        flush()

    # --------------------------------------------------------------------------------------------- markers
    def _extract_done(self, m: dict) -> None:
        run_id = m["run_id"]
        run = self.runs.get(run_id)
        if run["status"] == "running":
            missing = self.store.missing("clean", m["event_ids"])
            if missing:
                self.runs.patch(run_id, status="failed", finished_at=time.time(),
                                error=f"{len(missing)} of {len(m['event_ids'])} cleaned articles are missing from the store after sync")
                return
            self.runs.patch(run_id, status="done", stage="done", finished_at=time.time())
            log.info("run %s: %d clean events stored", run_id, len(m["event_ids"]))
        if m.get("enrich_after"):
            self._queue_enrichment(run_id)

    def _queue_enrichment(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run["status"] != "done" or (run.get("enrichment") or enrich_state())["state"] != "off":
            return                                         # already queued / running / done (redelivered marker)
        job = build_enrich_job(self.runs, run_id)
        self.runs.patch(run_id, enrichment=enrich_state(state="queued", stage="queued", queued_at=time.time()))
        try:
            self.bus.publish(self.s.topic_enrich_jobs, job, run_id)
        except Exception as e:
            log.exception("run %s: could not queue enrichment", run_id)
            self.runs.patch(run_id, enrichment=enrich_state(state="failed", error=f"could not queue on Kafka: {e}"))

    def _enrich_done(self, m: dict) -> None:
        run_id = m["run_id"]
        run = self.runs.get(run_id)
        if (run.get("enrichment") or {}).get("state") == "done":
            return
        missing = self.store.missing("enriched", m["event_ids"])
        enr = {**run["enrichment"]}
        if missing:
            enr.update(state="failed", finished_at=time.time(),
                       error=f"{len(missing)} of {len(m['event_ids'])} enriched articles are missing from the store after sync")
            self.runs.patch(run_id, enrichment=enr)
            return
        enr.update(state="done", stage="done", finished_at=time.time())
        self.runs.patch(run_id, metrics=m.get("metrics") or run.get("metrics"), enrichment=enr)
        log.info("run %s: %d enriched events stored", run_id, len(m["event_ids"]))


def main() -> int:
    from .store import make_store
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings(bus_backend="kafka")
    bus = make_bus(settings, "sync")
    assert isinstance(bus, KafkaBus)
    runs = RunStore(Path(os.getenv("NP_WORKDIR", "data/web")))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():                              # broker or database may still be starting: retry, never drop a batch
        store = None
        try:
            store = make_store(settings)
            bus.ensure_topics(all_topics(settings))
            sync = DataSync(store, runs, bus, settings)
            bus.consume([settings.topic_clean_events, settings.topic_enriched_events], sync.handle, stop,
                        max_batch=200, retry_on_error=True)
        except Exception:
            log.exception("data sync interrupted, restarting in 5s (uncommitted messages will be redelivered)")
            stop.wait(5)
        finally:
            if store is not None:
                store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
