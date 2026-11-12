import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from helpers import PhraseTableBackend
from newspaper_pipeline.api import server
from newspaper_pipeline.api.server import create_app, slugify
from newspaper_pipeline.config import Settings
from newspaper_pipeline.pipeline import Pipeline
from newspaper_pipeline.store import Store


def factory(settings, on_stage):
    return Pipeline(settings, Store(settings.db_path), translator=PhraseTableBackend(), on_stage=on_stage, render_pages=False)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "web", factory, sync=True, frontend_dist=tmp_path / "none"))


def pdf(papers_dir, pid):
    return (papers_dir / f"{pid}.pdf").read_bytes()


def upload(client, papers_dir, items, run_date="2026-11-02", enrich=True):
    meta, files = [], []
    for pid, name, lang in items:
        meta.append({"name": name, "language": lang, "edition": "Delhi", "reputation": "high"})
        files.append(("files", (f"{pid}.pdf", pdf(papers_dir, pid), "application/pdf")))
    data = {"run_date": run_date, "papers": json.dumps(meta), **({"enrich": "true"} if enrich else {})}
    return client.post("/api/runs", data=data, files=files)


ENGLISH = [("the_hindu", "The Hindu", "en"), ("toi", "The Times of India", "en"), ("indian_express", "The Indian Express", "en")]


def test_slugify():
    assert slugify("The Times of India") == "the_times_of_india" and slugify("!!!") == "paper"


def test_config_endpoint(client):
    c = client.get("/api/config").json()
    assert c["stages"] == ["ingest", "ocr", "clean"] and c["enrich_stages"] == ["cluster", "enrich", "export"]
    assert c["enrichment_bus"] == "inline" and c["max_papers"] >= 3


def test_upload_runs_pipeline_and_serves_results(client, papers_dir):
    r = upload(client, papers_dir, ENGLISH)
    assert r.status_code == 202
    run = r.json()
    assert run["status"] == "done" and run["metrics"]["articles"] >= 8 and run["error"] is None
    assert [p["id"] for p in run["papers"]] == ["the_hindu", "the_times_of_india", "the_indian_express"]
    rid = run["id"]
    assert client.get(f"/api/runs/{rid}").json()["status"] == "done"
    stories = client.get(f"/api/runs/{rid}/stories").json()
    resign = next(s for s in stories if s["event_type"] == "Politics.Resignation")
    assert resign["n_papers"] == 3 and stories[0]["n_papers"] == 3
    art = client.get(f"/api/runs/{rid}/articles/{resign['coverage'][0]['event_id']}")
    assert art.status_code == 200 and art.json()["event"]["type"] == "Politics.Resignation"
    assert isinstance(client.get(f"/api/runs/{rid}/review").json(), list)
    assert [r["id"] for r in client.get("/api/runs").json()] == [rid]


def test_hindi_upload_translated_and_joined(client, papers_dir):
    if not (papers_dir / "dainik_jagran.pdf").exists():
        pytest.skip("no Devanagari font")
    run = upload(client, papers_dir, ENGLISH + [("dainik_jagran", "Dainik Jagran", "hi")]).json()
    stories = client.get(f"/api/runs/{run['id']}/stories").json()
    assert next(s for s in stories if s["event_type"] == "Politics.Resignation")["n_papers"] == 4


def test_progress_reports_stage_and_background_mode(tmp_path, papers_dir):
    seen = []

    def make(settings, on_stage):
        p = factory(settings, lambda s: (seen.append(s), on_stage(s)))
        return p
    c = TestClient(create_app(tmp_path / "w", make, sync=False, frontend_dist=tmp_path / "none"))
    rid = upload(c, papers_dir, ENGLISH[:2]).json()["id"]
    for _ in range(100):
        st = c.get(f"/api/runs/{rid}").json()
        if st["status"] in ("done", "failed") and st["enrichment"]["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert st["status"] == "done" and st["stage"] == "done"
    assert seen == ["ingest", "ocr", "clean", "cluster", "enrich", "export"]      # extraction, then the inline enrichment job
    assert st["enrichment"]["state"] == "done"


def test_results_not_ready_is_409(tmp_path, papers_dir):
    mgr_app = create_app(tmp_path / "w", factory, sync=True, frontend_dist=tmp_path / "none")
    c = TestClient(mgr_app)
    rid = upload(c, papers_dir, ENGLISH[:2]).json()["id"]
    run_file = tmp_path / "w" / "runs" / rid / "run.json"
    data = json.loads(run_file.read_text()); data["status"] = "running"; run_file.write_text(json.dumps(data))
    assert c.get(f"/api/runs/{rid}/stories").status_code == 409
    assert c.delete(f"/api/runs/{rid}").status_code == 409


def test_validation_errors(client, papers_dir):
    good = [("files", ("a.pdf", pdf(papers_dir, "toi"), "application/pdf"))]
    meta = json.dumps([{"name": "TOI", "language": "en", "reputation": "high"}])
    post = lambda **kw: client.post("/api/runs", **kw)
    assert post(data={"run_date": "nope", "papers": meta}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": "{"}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": "[]"}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": json.dumps([{"name": "A"}, {"name": "B"}])}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": json.dumps([{"name": ""}])}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": json.dumps([{"name": "A", "language": "english"}])}, files=good).status_code == 422
    assert post(data={"run_date": "2026-11-02", "papers": json.dumps([{"name": "A", "reputation": "meh"}])}, files=good).status_code == 422
    bad = post(data={"run_date": "2026-11-02", "papers": meta}, files=[("files", ("x.pdf", b"<html>nope", "application/pdf"))])
    assert bad.status_code == 400 and "not a PDF" in bad.json()["detail"]
    assert post(data={"run_date": "2026-11-02"}).status_code == 422


def test_failed_validation_leaves_no_run_behind(client, tmp_path):
    client.post("/api/runs", data={"run_date": "2026-11-02", "papers": json.dumps([{"name": "A"}])},
                files=[("files", ("x.pdf", b"nope", "application/pdf"))])
    assert client.get("/api/runs").json() == []


def test_size_limit(client, papers_dir, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD_MB", 0)
    r = upload(client, papers_dir, ENGLISH[:1])
    assert r.status_code == 413 and client.get("/api/runs").json() == []


def test_duplicate_names_get_distinct_ids(client, papers_dir):
    run = upload(client, papers_dir, [("toi", "Same Paper", "en"), ("the_hindu", "Same Paper", "en")]).json()
    assert [p["id"] for p in run["papers"]] == ["same_paper", "same_paper_2"]


def test_failing_pipeline_marks_run_failed(tmp_path, papers_dir):
    def broken(settings, on_stage):
        raise RuntimeError("model exploded")
    c = TestClient(create_app(tmp_path / "w", broken, sync=True, frontend_dist=tmp_path / "none"))
    run = upload(c, papers_dir, ENGLISH[:1]).json()
    assert run["status"] == "failed" and "model exploded" in run["error"] and run["finished_at"]
    assert c.get(f"/api/runs/{run['id']}/stories").status_code == 409


def test_unknown_and_malformed_ids(client):
    assert client.get("/api/runs/000000000000").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/runs/not-a-run-id").status_code == 404
    assert client.get("/api/runs/abcdefabcdef/articles/zzz").status_code == 404


def test_article_id_cannot_traverse(client, papers_dir):
    rid = upload(client, papers_dir, ENGLISH[:2]).json()["id"]
    assert client.get(f"/api/runs/{rid}/articles/..%2F..%2Frun.json").status_code == 404
    assert client.get(f"/api/runs/{rid}/articles/{'a' * 20}").status_code == 404


def test_delete_run(client, papers_dir, tmp_path):
    rid = upload(client, papers_dir, ENGLISH[:2]).json()["id"]
    assert client.delete(f"/api/runs/{rid}").status_code == 204
    assert client.get(f"/api/runs/{rid}").status_code == 404 and client.get("/api/runs").json() == []


def test_interrupted_runs_are_marked_failed_on_restart(tmp_path):
    root = tmp_path / "w" / "runs" / "abcdefabcdef"
    root.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"id": "abcdefabcdef", "created_at": 1, "status": "running", "papers": []}))
    c = TestClient(create_app(tmp_path / "w", factory, sync=True, frontend_dist=tmp_path / "none"))
    run = c.get("/api/runs/abcdefabcdef").json()
    assert run["status"] == "failed" and "restarted" in run["error"]


def test_serves_built_frontend_when_present(tmp_path):
    dist = tmp_path / "dist"; dist.mkdir(); (dist / "index.html").write_text("<h1>ui</h1>")
    c = TestClient(create_app(tmp_path / "w", factory, sync=True, frontend_dist=dist))
    assert "<h1>ui</h1>" in c.get("/").text and c.get("/api/config").status_code == 200


def test_graph_endpoint_404_when_graph_backend_disabled(client, papers_dir):
    rid = upload(client, papers_dir, ENGLISH[:2]).json()["id"]
    sid = client.get(f"/api/runs/{rid}/stories").json()[0]["story_id"]
    r = client.get(f"/api/runs/{rid}/stories/{sid}/graph")
    assert r.status_code == 404 and "NP_GRAPH=neo4j" in r.json()["detail"]
    assert client.get(f"/api/runs/{rid}/stories/bad%20id!/graph").status_code == 404
    assert client.get("/api/runs/000000000000/stories/story-abc123/graph").status_code == 404


def test_config_reports_backends_and_graph_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("NP_STORE", "mongo"); monkeypatch.setenv("NP_INDEX", "pgvector"); monkeypatch.setenv("NP_GRAPH", "neo4j")
    c = TestClient(create_app(tmp_path / "w", factory, sync=True, frontend_dist=tmp_path / "none")).get("/api/config").json()
    assert (c["store"], c["index"], c["graph"]) == ("mongo", "pgvector", "neo4j")
    assert c["stages"] == ["ingest", "ocr", "clean"] and c["enrich_stages"] == ["cluster", "enrich", "graph", "export"]


def test_pipeline_is_closed_after_every_run(tmp_path, papers_dir):
    closed = []

    class Tracking(Pipeline):
        def close(self):
            closed.append(True)
            super().close()

    def make(settings, on_stage):
        return Tracking(settings, Store(settings.db_path), translator=PhraseTableBackend(), on_stage=on_stage, render_pages=False)
    c = TestClient(create_app(tmp_path / "w", make, sync=True, frontend_dist=tmp_path / "none"))
    upload(c, papers_dir, ENGLISH[:2])
    assert closed == [True, True]                      # the extraction pipeline and the (inline) enrichment pipeline
