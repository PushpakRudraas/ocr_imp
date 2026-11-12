"""A Store whose *writes of documents* go to Kafka instead of the database.

The extraction and enrichment workers use it, so they never write MongoDB themselves: every document they produce
(`pages`, `raw_events`, `clean_events`, `enriched_events`) is published and the data-sync service stores it. Reads (the
enrichment worker reads the clean events back) and the daily counters used for burst/novelty still go straight to the
wrapped store; they are not documents of the pipeline's output."""
from __future__ import annotations

from .bus import KafkaBus

DOC = "doc"


class BusStore:
    def __init__(self, inner, bus: KafkaBus, topic: str, run_id: str = "") -> None:
        self.inner, self.bus, self.topic, self.run_id = inner, bus, topic, run_id

    def put(self, stage: str, key: str, doc: dict) -> None:
        self.bus.publish(self.topic, {"type": DOC, "run_id": self.run_id, "stage": stage, "key": key, "doc": doc}, self.run_id or key)

    def put_many(self, stage: str, docs: list[dict], key_field: str = "event_id") -> None:
        if docs:
            self.bus.publish_many(self.topic, [{"type": DOC, "run_id": self.run_id, "stage": stage, "key": d[key_field], "doc": d}
                                               for d in docs], self.run_id or None)

    def __getattr__(self, name):                 # get / all / count / counters / history ... are plain reads on the wrapped store
        return getattr(self.inner, name)

    def close(self) -> None:
        self.inner.close()
