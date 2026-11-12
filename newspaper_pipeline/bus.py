"""Kafka wiring. With NP_BUS=kafka every hop of the pipeline goes through it:

    api ──newspaper.jobs.extract──▶ extract-worker (OCR + layout + clean + translate)
    extract-worker ──newspaper.events.clean──▶ datasync ──▶ MongoDB (pages, raw_events, clean_events)
    datasync ──newspaper.jobs.enrich──▶ enrich-worker (reads clean_events back from MongoDB, cluster + enrich)
    enrich-worker ──newspaper.events.enriched──▶ datasync ──▶ MongoDB (enriched_events)

Messages are JSON. Two kinds travel on the *events* topics: `{"type": "doc", stage, key, doc}` (one document to upsert) and
a marker (`extract_done` / `enrich_done`) sent after the last document of a run. A run's messages share one key and the
topics have one partition, so the marker is always consumed after the documents it covers.

Delivery is at-least-once (offsets are committed after a batch is handled) and every write is an upsert, so a redelivered
message is harmless.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from .config import Settings

log = logging.getLogger("np.bus")

Message = tuple[str, dict]                       # (topic, payload)
Handler = Callable[[list[Message]], None]
MAX_MESSAGE_BYTES = 10 * 1024 * 1024             # a long article with its enrichment is far bigger than Kafka's 1 MB default


def all_topics(s: Settings) -> list[str]:
    return [s.topic_extract_jobs, s.topic_clean_events, s.topic_enrich_jobs, s.topic_enriched_events]


class KafkaBus:
    def __init__(self, bootstrap: str, group: str = "newspaper", producer=None, consumer_factory=None) -> None:
        self.bootstrap, self.group = bootstrap, group
        self._producer = producer
        self._consumer_factory = consumer_factory

    # ------------------------------------------------------------------------------------------ producer
    def _prod(self):
        if self._producer is None:
            from confluent_kafka import Producer
            self._producer = Producer({"bootstrap.servers": self.bootstrap, "enable.idempotence": True,
                                       "message.max.bytes": MAX_MESSAGE_BYTES, "socket.timeout.ms": 10000,
                                       "message.timeout.ms": 30000, "linger.ms": 20})
        return self._producer

    def publish(self, topic: str, payload: dict, key: str | None = None) -> None:
        self.publish_many(topic, [payload], key)

    def publish_many(self, topic: str, payloads: list[dict], key: str | None = None) -> None:
        """Send all payloads in order; raises if the broker did not acknowledge every one (so callers can fail the run)."""
        p = self._prod()
        errors: list = []

        def done(err, _msg):
            if err:
                errors.append(err)
        for payload in payloads:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode()
            while True:
                try:
                    p.produce(topic, key=key, value=data, on_delivery=done)
                    break
                except BufferError:                     # local queue full: let delivery reports drain it
                    p.poll(0.5)
            p.poll(0)
        if p.flush(60) > 0:
            raise RuntimeError(f"kafka: messages not delivered to {self.bootstrap} within 60s")
        if errors:
            raise RuntimeError(f"kafka: {errors[0]}")

    # ------------------------------------------------------------------------------------------ consumer
    def _consumer(self):
        if self._consumer_factory:
            return self._consumer_factory()
        from confluent_kafka import Consumer
        return Consumer({"bootstrap.servers": self.bootstrap, "group.id": self.group, "enable.auto.commit": False,
                         "auto.offset.reset": "earliest", "fetch.message.max.bytes": MAX_MESSAGE_BYTES,
                         "max.poll.interval.ms": 6 * 3600 * 1000})   # one job can run for hours: do not get kicked out of the group

    def consume(self, topics: list[str], handler: Handler, stop: threading.Event | None = None,
                max_batch: int = 1, retry_on_error: bool = False) -> None:
        """Blocking loop. Up to `max_batch` messages per call to `handler([(topic, payload), ...])`; offsets are committed
        after the handler returns. Undecodable messages are skipped. If the handler raises:
          retry_on_error=False  the batch is logged and skipped (the handler records the failure on the run itself);
          retry_on_error=True   the exception propagates *without committing*, so after the caller restarts the consumer
                                the batch is redelivered (used by the data-sync service: never lose a document)."""
        stop = stop or threading.Event()
        c = self._consumer()
        c.subscribe(topics)
        log.info("consuming %s on %s as group %s", topics, self.bootstrap, self.group)
        try:
            while not stop.is_set():
                raw = c.consume(max_batch, 1.0) if max_batch > 1 else [m for m in [c.poll(1.0)] if m is not None]
                batch: list[Message] = []
                last = None
                for msg in raw:
                    if msg.error():
                        log.warning("kafka: %s", msg.error())
                        continue
                    last = msg
                    try:
                        batch.append((msg.topic(), json.loads(msg.value())))
                    except Exception:
                        log.error("skipping undecodable message at %s[%s]@%s", msg.topic(), msg.partition(), msg.offset())
                if batch:
                    try:
                        handler(batch)
                    except Exception:
                        log.exception("handler failed for a batch of %d message(s)", len(batch))
                        if retry_on_error:
                            raise
                if last is not None:
                    c.commit(message=last, asynchronous=False)
        finally:
            c.close()

    def ensure_topics(self, topics: list[str]) -> None:
        """Create the topics (one partition: ordering between a run's documents and its marker) if missing."""
        from confluent_kafka.admin import AdminClient, NewTopic
        admin = AdminClient({"bootstrap.servers": self.bootstrap})
        new = [NewTopic(t, num_partitions=1, replication_factor=1, config={"max.message.bytes": str(MAX_MESSAGE_BYTES)}) for t in topics]
        for fut in admin.create_topics(new).values():
            try:
                fut.result(15)
            except Exception as e:                                  # already exists is fine
                if "TOPIC_ALREADY_EXISTS" not in str(e):
                    raise

    def close(self) -> None:
        if self._producer is not None:
            self._producer.flush(5)


def make_bus(settings: Settings, role: str = "api") -> KafkaBus | None:
    """None = inline mode (no broker): the API process runs both phases itself. `role` picks the consumer group."""
    if settings.bus_backend == "kafka":
        return KafkaBus(settings.kafka_bootstrap, f"{settings.kafka_group}-{role}")
    return None
