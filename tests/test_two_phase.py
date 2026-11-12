"""The two-phase flow, and the guarantee that with Kafka *everything* passes through it:

    api -[jobs.extract]-> extract worker (OCR + clean) -[events.clean]-> datasync -> store
        datasync -[jobs.enrich]-> enrich worker (reads the store, enriches) -[events.enriched]-> datasync -> store

`FakeBroker` carries those messages in-process, so the whole chain runs deterministically without a broker; the shared store
refuses any document write that does not come from the data-sync service."""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from helpers import PhraseTableBackend
from newspaper_pipeline.api.server import create_app
from newspaper_pipeline.bus import KafkaBus, all_topics, make_bus
from newspaper_pipeline.busstore import BusStore
from newspaper_pipeline.config import Settings
from newspaper_pipeline.datasync import DataSync
from newspaper_pipeline.jobs import run_enrichment, run_extraction
from newspaper_pipeline.pipeline import Pipeline
from newspaper_pipeline.runs import RunStore
from newspaper_pipeline.store import Store

ENGLISH = [("the_hindu", "The Hindu", "en"), ("toi", "The Times of India", "en"), ("indian_express", "The Indian Express", "en")]
S = Settings(bus_backend="kafka")
EXTRACT, CLEAN, ENRICH, ENRICHED = S.topic_extract_jobs, S.topic_clean_events, S.topic_enrich_jobs, S.topic_enriched_events


def inline_factory(settings, on_stage):
    return Pipeline(settings, Store(settings.db_path), translator=PhraseTableBackend(), on_stage=on_stage, render_pages=False)


def upload(client, papers_dir, items=ENGLISH, enrich=False):
    meta = [{"name": n, "language": lang, "edition": "Delhi", "reputation": "high"} for _, n, lang in items]
    files = [("files", (f"{pid}.pdf", (papers_dir / f"{pid}.pdf").read_bytes(), "application/pdf")) for pid, _, _ in items]
    data = {"run_date": "2026-11-02", "papers": json.dumps(meta), **({"enrich": "true"} if enrich else {})}
    return client.post("/api/runs", data=data, files=files)


def inline_app(tmp_path, make=inline_factory):
    return TestClient(create_app(tmp_path / "w", make, sync=True, frontend_dist=tmp_path / "none"))


# ======================================================================================================== fake Kafka
class FakeBroker:
    """Stands in for KafkaBus. Messages are JSON round-tripped like the real thing; `drain` plays every consumer."""
    def __init__(self, fail_topics=()):
        self.queue, self.log, self.handlers, self.fail_topics = [], [], {}, set(fail_topics)

    def publish(self, topic, payload, key=None):
        self.publish_many(topic, [payload], key)

    def publish_many(self, topic, payloads, key=None):
        if topic in self.fail_topics:
            raise RuntimeError("broker down")
        for p in payloads:
            msg = (topic, json.loads(json.dumps(p, default=str)))
            self.queue.append(msg)
            self.log.append((topic, msg[1].get("type", "job")))

    def drain(self, topics=None):
        """Deliver queued messages (optionally only those of `topics`) as per-topic batches until nothing is left."""
        while True:
            ready = [m for m in self.queue if topics is None or m[0] in topics]
            if not ready:
                return
            topic = ready[0][0]
            batch = [m for m in ready if m[0] == topic]
            for m in batch:
                self.queue.remove(m)
            for h in self.handlers.get(topic, []):
                h(batch)


class WriteGuardStore(Store):
    """Plays MongoDB. Only the data-sync service may write documents; a worker writing directly would raise."""
    def __init__(self, path):
        super().__init__(path)
        self.syncing = False

    def put(self, stage, key, doc):
        assert self.syncing, f"direct write to {stage!r} outside the data-sync service"
        super().put(stage, key, doc)

    def put_keyed_many(self, stage, items):
        self.syncing = True
        try:
            super().put_keyed_many(stage, items)
        finally:
            self.syncing = False

    def close(self):                          # the pipelines close their store after every job; this one is shared
        pass


class Chain:
    """api + extract worker + datasync + enrich worker, wired through one FakeBroker."""
    def __init__(self, tmp_path, fail_topics=(), extract_make=None):
        self.tmp = tmp_path
        self.broker = FakeBroker(fail_topics)
        self.mongo = WriteGuardStore(tmp_path / "mongo.sqlite")
        self.runs = RunStore(tmp_path / "w")
        self.stages = {"extract": [], "enrich": []}
        self.client = TestClient(create_app(tmp_path / "w", None, sync=True, frontend_dist=tmp_path / "none", bus=self.broker))
        self.sync = DataSync(self.mongo, self.runs, self.broker, S)

        def make(role, topic):
            def for_run(run_id):
                def build(settings, on_stage):
                    if role == "extract" and extract_make:
                        return extract_make(settings, on_stage)
                    p = Pipeline(settings, BusStore(self.mongo, self.broker, topic, run_id), translator=PhraseTableBackend(),
                                 on_stage=lambda n: (self.stages[role].append(n), on_stage(n)), render_pages=False)
                    return p
                return build
            return for_run
        self.make_extract, self.make_enrich = make("extract", CLEAN), make("enrich", ENRICHED)
        self.broker.handlers = {
            EXTRACT: [lambda msgs: [run_extraction(j, self.runs, self.make_extract(j["run_id"]), self.broker, CLEAN) for _t, j in msgs]],
            ENRICH: [lambda msgs: [run_enrichment(j, self.runs, self.make_enrich(j["run_id"]), self.broker, ENRICHED) for _t, j in msgs]],
            CLEAN: [self.sync.handle], ENRICHED: [self.sync.handle]}

    def run(self, rid):
        return self.client.get(f"/api/runs/{rid}").json()


# ============================================================================================ everything through Kafka
def test_everything_passes_through_kafka_in_order(tmp_path, papers_dir):
    c = Chain(tmp_path)
    run = upload(c.client, papers_dir, enrich=True).json()
    rid = run["id"]
    assert run["status"] == "queued" and run["stage"] == "queued"
    assert c.broker.log == [(EXTRACT, "job")]                           # the API only queued the work
    assert c.mongo.count("clean") == 0 and c.stages == {"extract": [], "enrich": []}

    c.broker.drain()
    done = c.run(rid)
    assert done["status"] == "done" and done["enrichment"]["state"] == "done" and done["metrics"]["stories"] >= 1
    # the first message of each hop, in order: job -> clean docs -> marker -> enrich job -> enriched docs -> marker
    firsts = list(dict.fromkeys(c.broker.log))
    assert firsts == [(EXTRACT, "job"), (CLEAN, "doc"), (CLEAN, "extract_done"), (ENRICH, "job"), (ENRICHED, "doc"), (ENRICHED, "enrich_done")]
    assert c.stages == {"extract": ["ingest", "ocr", "clean"], "enrich": ["cluster", "enrich", "export"]}
    n = done["metrics"]["articles"]
    assert c.mongo.count("pages") > 0 and c.mongo.count("ocr") == n and c.mongo.count("clean") == n and c.mongo.count("enriched") == n
    assert any(s["n_papers"] == 3 for s in c.client.get(f"/api/runs/{rid}/stories").json())      # (and no direct write ever tripped the guard)


def test_without_enrichment_the_chain_stops_after_the_sync(tmp_path, papers_dir):
    c = Chain(tmp_path)
    rid = upload(c.client, papers_dir).json()["id"]
    c.broker.drain()
    run = c.run(rid)
    assert run["status"] == "done" and run["enrichment"]["state"] == "off" and run["enrich_after"] is False
    assert (ENRICH, "job") not in c.broker.log and c.mongo.count("enriched") == 0 and c.mongo.count("clean") >= 8
    assert "stories" not in run["metrics"]
    assert len(c.client.get(f"/api/runs/{rid}/clean").json()) == c.mongo.count("clean")
    assert c.client.get(f"/api/runs/{rid}/stories").status_code == 409


def test_a_run_is_only_done_once_datasync_has_stored_it(tmp_path, papers_dir):
    c = Chain(tmp_path)
    rid = upload(c.client, papers_dir).json()["id"]
    c.broker.drain([EXTRACT])                                           # only the extract worker has run so far
    run = c.run(rid)
    assert run["status"] == "running" and run["stage"] == "store" and run["metrics"]["articles"] >= 8
    assert c.mongo.count("clean") == 0                                  # documents are still in Kafka
    assert c.client.get(f"/api/runs/{rid}/clean").status_code == 409
    c.broker.drain([CLEAN])                                             # now datasync consumes them
    assert c.run(rid)["status"] == "done" and c.mongo.count("clean") >= 8


def test_enrich_on_demand_goes_through_kafka_too(tmp_path, papers_dir):
    c = Chain(tmp_path)
    rid = upload(c.client, papers_dir).json()["id"]
    c.broker.drain()
    r = c.client.post(f"/api/runs/{rid}/enrich")
    assert r.status_code == 202 and r.json()["enrichment"]["state"] == "queued"
    assert c.broker.queue[0][0] == ENRICH and c.broker.queue[0][1]["event_ids"] and c.mongo.count("enriched") == 0
    assert c.client.post(f"/api/runs/{rid}/enrich").status_code == 409
    c.broker.drain([ENRICH])
    run = c.run(rid)
    assert run["enrichment"]["state"] == "running" and run["enrichment"]["stage"] == "store"     # waiting for the sync
    c.broker.drain()
    assert c.run(rid)["enrichment"]["state"] == "done" and c.mongo.count("enriched") == c.mongo.count("clean")


def test_the_api_does_no_pipeline_work_in_kafka_mode(tmp_path, papers_dir):
    c = Chain(tmp_path)
    cfg = c.client.get("/api/config").json()
    assert cfg["enrichment_bus"] == "kafka"
    assert cfg["stages"] == ["queued", "ingest", "ocr", "clean", "store"]
    assert cfg["enrich_stages"] == ["cluster", "enrich", "export", "store"]
    upload(c.client, papers_dir)
    assert not list((tmp_path / "w" / "runs").glob("*/data/pipeline.sqlite"))      # no pipeline was ever built in the api
    assert c.mongo.count("pages") == 0


def test_queued_runs_survive_an_api_restart_in_kafka_mode(tmp_path, papers_dir):
    c = Chain(tmp_path)
    rid = upload(c.client, papers_dir).json()["id"]
    again = TestClient(create_app(tmp_path / "w", None, sync=True, frontend_dist=tmp_path / "none", bus=c.broker))
    assert again.get(f"/api/runs/{rid}").json()["status"] == "queued"      # not failed: the message is still on Kafka


# ============================================================================================================ failures
def test_failing_extraction_fails_the_run_and_publishes_no_marker(tmp_path, papers_dir):
    def boom(settings, on_stage):
        raise RuntimeError("ocr exploded")
    c = Chain(tmp_path, extract_make=boom)
    rid = upload(c.client, papers_dir, enrich=True).json()["id"]
    c.broker.drain()
    run = c.run(rid)
    assert run["status"] == "failed" and "ocr exploded" in run["error"] and run["enrichment"]["state"] == "off"
    assert (CLEAN, "extract_done") not in c.broker.log and (ENRICH, "job") not in c.broker.log


def test_cannot_queue_extraction_marks_the_run_failed(tmp_path, papers_dir):
    c = Chain(tmp_path, fail_topics=[EXTRACT])
    run = upload(c.client, papers_dir).json()
    assert run["status"] == "failed" and "could not queue extraction on Kafka" in run["error"]


def test_cannot_queue_enrichment_after_sync_marks_enrichment_failed(tmp_path, papers_dir):
    c = Chain(tmp_path, fail_topics=[ENRICH])
    rid = upload(c.client, papers_dir, enrich=True).json()["id"]
    c.broker.drain()
    run = c.run(rid)
    assert run["status"] == "done" and run["enrichment"]["state"] == "failed" and "broker down" in run["enrichment"]["error"]
    c.broker.fail_topics.clear()
    assert c.client.post(f"/api/runs/{rid}/enrich").status_code == 202           # retry from the UI
    c.broker.drain()
    assert c.run(rid)["enrichment"]["state"] == "done"


def test_failing_enrichment_is_recorded_and_retryable(tmp_path, papers_dir):
    c = Chain(tmp_path)
    rid = upload(c.client, papers_dir, enrich=True).json()["id"]
    good = c.make_enrich

    def exploding(settings, on_stage):
        raise RuntimeError("model exploded")
    c.make_enrich = lambda run_id: exploding
    c.broker.drain()
    run = c.run(rid)
    assert run["status"] == "done" and run["enrichment"]["state"] == "failed" and "model exploded" in run["enrichment"]["error"]
    c.make_enrich = good
    assert c.client.post(f"/api/runs/{rid}/enrich").status_code == 202
    c.broker.drain()
    assert c.run(rid)["enrichment"]["state"] == "done"


# ================================================================================================== data sync service
def doc(stage, key, **body):
    return (CLEAN, {"type": "doc", "run_id": "r", "stage": stage, "key": key, "doc": {"event_id": key, "date": "2026-11-02", **body}})


def seeded_run(tmp_path, **over):
    runs = RunStore(tmp_path / "w")
    (runs.root / "abcdefabcdef").mkdir(parents=True)
    runs.write({"id": "abcdefabcdef", "created_at": 1, "status": "running", "stage": "store", "run_date": "2026-11-02", "papers": [], "metrics": {},
                "enrich_after": False, "enrichment": {"state": "off", "stage": None, "error": None, "queued_at": None, "started_at": None,
                                                       "finished_at": None}, **over})
    return runs


def test_datasync_upserts_documents_by_stage_then_completes_the_run(tmp_path):
    runs, store, broker = seeded_run(tmp_path), WriteGuardStore(tmp_path / "m.sqlite"), FakeBroker()
    sync = DataSync(store, runs, broker, S)
    marker = (CLEAN, {"type": "extract_done", "run_id": "abcdefabcdef", "event_ids": ["a", "b"], "enrich_after": False})
    sync.handle([doc("clean", "a"), doc("ocr", "x"), doc("clean", "b"), marker])        # the marker follows its documents in the same batch
    assert store.count("clean") == 2 and store.count("ocr") == 1
    assert runs.get("abcdefabcdef")["status"] == "done" and runs.get("abcdefabcdef")["stage"] == "done"
    sync.handle([doc("clean", "a", headline="changed")])                                  # upsert, not duplicate
    assert store.count("clean") == 2 and store.get("clean", "a")["headline"] == "changed"


def test_datasync_fails_the_run_when_documents_are_missing(tmp_path):
    runs, store = seeded_run(tmp_path), WriteGuardStore(tmp_path / "m.sqlite")
    DataSync(store, runs, FakeBroker(), S).handle([doc("clean", "a"), (CLEAN, {"type": "extract_done", "run_id": "abcdefabcdef", "event_ids": ["a", "b"]})])
    run = runs.get("abcdefabcdef")
    assert run["status"] == "failed" and "1 of 2 cleaned articles are missing" in run["error"]


def test_datasync_redelivery_is_idempotent_and_queues_enrichment_once(tmp_path):
    runs, store, broker = seeded_run(tmp_path, enrich_after=True), WriteGuardStore(tmp_path / "m.sqlite"), FakeBroker()
    d = runs.dir("abcdefabcdef") / "out"
    d.mkdir(parents=True)
    (d / "clean_articles.json").write_text(json.dumps([{"event_id": "a"}]))
    batch = [doc("clean", "a"), (CLEAN, {"type": "extract_done", "run_id": "abcdefabcdef", "event_ids": ["a"], "enrich_after": True})]
    sync = DataSync(store, runs, broker, S)
    sync.handle(batch)
    sync.handle(batch)                                                                     # redelivered after a crash
    assert [t for t, _ in broker.queue] == [ENRICH]
    assert broker.queue[0][1] == {"run_id": "abcdefabcdef", "run_date": "2026-11-02", "event_ids": ["a"]}
    assert runs.get("abcdefabcdef")["enrichment"]["state"] == "queued"


def test_datasync_ignores_unknown_runs_and_message_types(tmp_path):
    runs, store = seeded_run(tmp_path), WriteGuardStore(tmp_path / "m.sqlite")
    DataSync(store, runs, FakeBroker(), S).handle([(CLEAN, {"type": "extract_done", "run_id": "000000000000", "event_ids": []}),
                                                   (CLEAN, {"type": "mystery"}), (ENRICHED, {"type": "enrich_done", "run_id": "../x", "event_ids": []})])


def test_datasync_enrich_done_stores_metrics_and_finishes(tmp_path):
    runs = seeded_run(tmp_path, status="done", stage="done",
                      enrichment={"state": "running", "stage": "store", "error": None, "queued_at": 1, "started_at": 2, "finished_at": None})
    store = WriteGuardStore(tmp_path / "m.sqlite")
    DataSync(store, runs, FakeBroker(), S).handle([(ENRICHED, {"type": "doc", "run_id": "r", "stage": "enriched", "key": "a", "doc": {"event_id": "a"}}),
        (ENRICHED, {"type": "enrich_done", "run_id": "abcdefabcdef", "event_ids": ["a"], "metrics": {"stories": 4}})])
    run = runs.get("abcdefabcdef")
    assert run["enrichment"]["state"] == "done" and run["enrichment"]["finished_at"] and run["metrics"] == {"stories": 4}


# ======================================================================================================= BusStore
def test_busstore_publishes_documents_and_reads_through(tmp_path):
    broker, inner = FakeBroker(), Store(":memory:")
    inner.put("clean", "k", {"event_id": "k", "date": "2026-11-02"})
    bs = BusStore(inner, broker, CLEAN, "r1")
    bs.put_many("clean", [{"event_id": "a", "x": 1}, {"event_id": "b"}])
    bs.put("pages", "p1", {"key": "p1"})
    assert [(t, m["stage"], m["key"], m["run_id"]) for t, m in broker.queue] == [(CLEAN, "clean", "a", "r1"), (CLEAN, "clean", "b", "r1"), (CLEAN, "pages", "p1", "r1")]
    assert inner.count("clean") == 1                                      # nothing was written to the wrapped store
    assert bs.get("clean", "k")["event_id"] == "k" and bs.count("clean") == 1   # reads pass through
    bs.increment("c", "2026-11-02"); assert bs.counter("c", "2026-11-02") == 1  # counters too


# ============================================================================================= inline mode (no Kafka)
def test_inline_mode_still_runs_everything_in_the_api(tmp_path, papers_dir):
    seen = []
    c = inline_app(tmp_path, lambda st, on: inline_factory(st, lambda n: (seen.append(n), on(n))))
    assert c.get("/api/config").json()["stages"] == ["ingest", "ocr", "clean"]
    rid = upload(c, papers_dir).json()["id"]
    run = c.get(f"/api/runs/{rid}").json()
    assert seen == ["ingest", "ocr", "clean"] and run["status"] == "done" and run["enrichment"]["state"] == "off"
    r = c.post(f"/api/runs/{rid}/enrich")
    assert r.status_code == 202 and r.json()["enrichment"]["state"] == "done" and seen[3:] == ["cluster", "enrich", "export"]
    assert any(s["n_papers"] == 3 for s in c.get(f"/api/runs/{rid}/stories").json())


def test_inline_enrichment_failure_is_recorded_and_retryable(tmp_path, papers_dir):
    calls = []

    def make(settings, on_stage):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("model exploded")
        return inline_factory(settings, on_stage)
    c = inline_app(tmp_path, make)
    run = upload(c, papers_dir, ENGLISH[:2], enrich=True).json()
    assert run["status"] == "done" and run["enrichment"]["state"] == "failed" and "model exploded" in run["enrichment"]["error"]
    assert c.post(f"/api/runs/{run['id']}/enrich").status_code == 202
    assert c.get(f"/api/runs/{run['id']}").json()["enrichment"]["state"] == "done"


def test_old_runs_read_as_enriched(tmp_path):
    root = tmp_path / "w" / "runs" / "abcdefabcdef"
    root.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"id": "abcdefabcdef", "created_at": 1, "status": "done", "papers": [], "metrics": {}}))
    assert inline_app(tmp_path).get("/api/runs/abcdefabcdef").json()["enrichment"]["state"] == "done"


def test_jobs_for_missing_or_finished_runs_are_ignored(tmp_path):
    runs = RunStore(tmp_path / "w")
    boom = lambda st, on: 1 / 0
    assert run_extraction({"run_id": "000000000000"}, runs, boom) is False
    assert run_extraction({"run_id": "../../etc"}, runs, boom) is False
    run_enrichment({"run_id": "000000000000", "run_date": "2026-11-02"}, runs, boom)
    done = seeded_run(tmp_path, status="done")
    assert run_extraction({"run_id": "abcdefabcdef"}, done, boom) is False         # redelivered extract job of a finished run


# =========================================================================================================== KafkaBus
class FakeProducer:
    def __init__(self, undelivered=0, error=None, buffer_full=0):
        self.sent, self.undelivered, self.error, self.buffer_full, self.polls = [], undelivered, error, buffer_full, 0

    def produce(self, topic, key, value, on_delivery):
        if self.buffer_full:
            self.buffer_full -= 1
            raise BufferError("queue full")
        self.sent.append((topic, key, json.loads(value)))
        on_delivery(self.error, None)

    def poll(self, t): self.polls += 1
    def flush(self, timeout): return self.undelivered


class FakeMsg:
    def __init__(self, value, topic="t", err=None, offset=0):
        self._v, self._t, self._e, self._o = value, topic, err, offset

    def value(self): return self._v
    def topic(self): return self._t
    def error(self): return self._e
    def offset(self): return self._o
    def partition(self): return 0


class FakeConsumer:
    def __init__(self, msgs, stop):
        self.msgs, self.stop, self.committed, self.closed, self.topics = list(msgs), stop, [], False, None

    def subscribe(self, topics): self.topics = topics

    def _take(self, n):
        out, self.msgs = self.msgs[:n], self.msgs[n:]
        if not out:
            self.stop.set()
        return out

    def poll(self, t): return (self._take(1) or [None])[0]
    def consume(self, n, t): return self._take(n)
    def commit(self, message, asynchronous): self.committed.append(message.offset())
    def close(self): self.closed = True


def bus(producer=None, consumer=None):
    return KafkaBus("k:9092", "g", producer=producer, consumer_factory=(lambda: consumer) if consumer else None)


def test_publish_many_keeps_order_retries_a_full_queue_and_raises_when_undelivered():
    p = FakeProducer(buffer_full=2)
    bus(p).publish_many("t", [{"n": 1}, {"n": 2}, {"n": 3}], key="r1")
    assert [(t, k, v["n"]) for t, k, v in p.sent] == [("t", "r1", 1), ("t", "r1", 2), ("t", "r1", 3)] and p.polls >= 2
    with pytest.raises(RuntimeError, match="not delivered"):
        bus(FakeProducer(undelivered=1)).publish("t", {"a": 1})
    with pytest.raises(RuntimeError, match="boom"):
        bus(FakeProducer(error="boom")).publish("t", {"a": 1})


def test_consume_batches_commits_the_last_offset_and_skips_undecodable_messages():
    stop, got = threading.Event(), []
    cons = FakeConsumer([FakeMsg(b'{"n": 1}', "a", offset=1), FakeMsg(b"not json", "a", offset=2), FakeMsg(b'{"n": 3}', "b", offset=3)], stop)
    bus(consumer=cons).consume(["a", "b"], lambda batch: got.append(batch), stop, max_batch=10)
    assert got == [[("a", {"n": 1}), ("b", {"n": 3})]] and cons.committed == [3] and cons.closed and cons.topics == ["a", "b"]


def test_consume_one_job_at_a_time_and_a_failing_handler_is_skipped_by_default():
    stop, got = threading.Event(), []
    cons = FakeConsumer([FakeMsg(b'{"n": 1}', offset=1), FakeMsg(b'{"n": 2}', offset=2), FakeMsg(b'{"n": 3}', offset=3)], stop)

    def handler(batch):
        if batch[0][1]["n"] == 2:
            raise RuntimeError("bug")
        got.append(batch[0][1]["n"])
    bus(consumer=cons).consume(["t"], handler, stop)
    assert got == [1, 3] and cons.committed == [1, 2, 3]


def test_consume_with_retry_on_error_does_not_commit_the_failed_batch():
    stop = threading.Event()
    cons = FakeConsumer([FakeMsg(b'{"n": 1}', offset=1), FakeMsg(b'{"n": 2}', offset=2)], stop)

    def handler(batch):
        if batch[0][1]["n"] == 2:
            raise RuntimeError("mongo down")
    with pytest.raises(RuntimeError, match="mongo down"):
        bus(consumer=cons).consume(["t"], handler, stop, retry_on_error=True)
    assert cons.committed == [1] and cons.closed                           # offset 2 stays uncommitted -> redelivered on restart


def test_make_bus_follows_settings_and_gives_each_role_its_own_group():
    assert make_bus(Settings(bus_backend="inline")) is None
    b = make_bus(Settings(bus_backend="kafka", kafka_bootstrap="kafka:9092", kafka_group="np"), "sync")
    assert (b.bootstrap, b.group) == ("kafka:9092", "np-sync")
    assert all_topics(S) == [EXTRACT, CLEAN, ENRICH, ENRICHED] and len(set(all_topics(S))) == 4
