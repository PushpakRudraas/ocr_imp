"""Mongo / pgvector / Neo4j backends and the docker stack definition.

Pure tests (no marker) run anywhere. Tests marked `docker` start throwaway containers (see conftest.py)."""
import copy
import datetime as dt
import json
import uuid
from pathlib import Path

import numpy as np
import pytest
import yaml

from helpers import PhraseTableBackend
from newspaper_pipeline.clean.normalize import clean_article, mark_exact_duplicates
from newspaper_pipeline.cluster.clusterer import StoryClusterer
from newspaper_pipeline.cluster.embed import HashingEmbedder
from newspaper_pipeline.cluster.index import DateWindow, InMemoryIndex, PgVectorIndex
from newspaper_pipeline.config import Settings
from newspaper_pipeline.enrich.pipeline import enrich_article, enrich_stories
from newspaper_pipeline.graph.neo4j_graph import ENTITY_LABELS, Neo4jGraph, build_graph_rows
from newspaper_pipeline.input.fetch import fetch_editions
from newspaper_pipeline.input.splitter import split_edition
from newspaper_pipeline.ocr.run import extract_edition
from newspaper_pipeline.pipeline import Pipeline
from newspaper_pipeline.store import MongoStore, Store, doc_meta, make_store

ROOT = Path(__file__).resolve().parent.parent
docker = pytest.mark.docker


# ================================================================================================= shared fixtures
@pytest.fixture(scope="module")
def docs(manifest, tmp_path_factory):
    """Enriched docs for the fixture papers (no services involved)."""
    out = tmp_path_factory.mktemp("svc")
    arts = []
    for e in fetch_editions(manifest, out):
        for r in extract_edition(split_edition(e, manifest.run_date, out, dpi=40, render=False)):
            arts.append(clean_article(r, PhraseTableBackend()))
    mark_exact_duplicates(arts)
    StoryClusterer(HashingEmbedder(dim=1024)).cluster_all(arts)
    return enrich_stories([enrich_article(a) for a in arts])


def groups(docs_):
    by = {}
    for d in docs_:
        by.setdefault(d["story"]["story_id"], set()).add(d["event_id"])
    return sorted(sorted(v) for v in by.values())


# ================================================================================================= docker stack definition
def test_compose_defines_the_whole_stack():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    svc = c["services"]
    assert set(svc) == {"mongo", "postgres", "neo4j", "kafka", "api", "extract-worker", "datasync", "enrich-worker", "web", "kafka-ui"}
    assert svc["postgres"]["image"].startswith("pgvector/pgvector")
    assert svc["mongo"]["image"].startswith("mongo:") and svc["neo4j"]["image"].startswith("neo4j:")
    for name in ("mongo", "postgres", "neo4j", "kafka", "api"):
        assert "healthcheck" in svc[name], f"{name} needs a healthcheck so depends_on can wait for it"
    deps = svc["api"]["depends_on"]
    assert all(deps[d]["condition"] == "service_healthy" for d in ("mongo", "postgres", "neo4j", "kafka"))
    assert svc["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    env = svc["api"]["environment"]
    assert (env["NP_STORE"], env["NP_INDEX"], env["NP_GRAPH"]) == ("mongo", "pgvector", "neo4j")
    assert "@mongo:27017" in env["NP_MONGO_URI"] and "@postgres:5432" in env["NP_PG_DSN"] and env["NP_NEO4J_URI"] == "bolt://neo4j:7687"
    assert set(c["volumes"]) >= {"mongo-data", "pg-data", "neo4j-data", "api-data"}
    for name in ("mongo", "postgres", "neo4j"):                       # data stores are not exposed beyond localhost
        assert all(p.startswith("127.0.0.1:") for p in svc[name]["ports"]), name
    assert "ports" in svc["web"]
    # everything passes through Kafka: api -> extract-worker -> datasync -> enrich-worker -> datasync
    assert "ports" not in svc["kafka"] and svc["kafka"]["image"].startswith("apache/kafka")
    cmds = {n: svc[n]["command"] for n in ("extract-worker", "datasync", "enrich-worker")}
    assert cmds == {"extract-worker": ["python", "-m", "newspaper_pipeline.worker", "extract"],
                    "datasync": ["python", "-m", "newspaper_pipeline.datasync"],
                    "enrich-worker": ["python", "-m", "newspaper_pipeline.worker", "enrich"]}
    for n in ("api", "extract-worker", "datasync", "enrich-worker"):
        e = svc[n]["environment"]
        assert e["NP_BUS"] == "kafka" and e["NP_KAFKA_BOOTSTRAP"] == "kafka:9092" and e["NP_KAFKA_TOPIC"] == env["NP_KAFKA_TOPIC"], n
        assert svc[n]["depends_on"]["kafka"]["condition"] == "service_healthy", n
        if n != "api":
            assert svc[n]["build"] == svc["api"]["build"] and "api-data:/data" in svc[n]["volumes"], n   # same image; run.json is shared
    sync, ex, en = svc["datasync"]["environment"], svc["extract-worker"]["environment"], svc["enrich-worker"]["environment"]
    assert sync["NP_STORE"] == en["NP_STORE"] == "mongo" and sync["NP_MONGO_URI"] == env["NP_MONGO_URI"]
    assert ex["NP_STORE"] == "sqlite"                                    # extract never writes MongoDB itself (it publishes to Kafka)
    assert svc["datasync"]["depends_on"]["mongo"]["condition"] == "service_healthy"
    assert all(en["NP_" + k] == env["NP_" + k] for k in ("INDEX", "GRAPH", "PG_DSN", "NEO4J_URI"))
    assert "kafka-data" in c["volumes"]


def test_dockerfiles_and_env_example_are_consistent():
    api = (ROOT / "Dockerfile.api").read_text()
    assert "uvicorn" in api and "USER app" in api
    nginx = (ROOT / "frontend" / "nginx.conf").read_text()
    assert "proxy_pass http://api:8000" in nginx and "client_max_body_size" in nginx and "try_files" in nginx
    assert "nginx" in (ROOT / "frontend" / "Dockerfile").read_text()
    reqs = (ROOT / "requirements-services.txt").read_text()
    for pkg in ("pymongo", "psycopg", "neo4j", "fastapi", "pymupdf", "confluent-kafka"):
        assert pkg in reqs
    assert "pytest" not in reqs
    example = dict(l.split("=", 1) for l in (ROOT / ".env.example").read_text().splitlines()
                   if "=" in l and not l.startswith("#"))
    compose = (ROOT / "docker-compose.yml").read_text()
    for var in ("MONGO_PASSWORD", "POSTGRES_PASSWORD", "NEO4J_PASSWORD", "WEB_PORT"):
        assert var in example and "${" + var in compose
    assert "CREATE EXTENSION IF NOT EXISTS vector" in (ROOT / "infra" / "postgres" / "init.sql").read_text()
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


# ================================================================================================= store: sqlite + mongo contract
@pytest.fixture(params=["sqlite", "mongo"])
def store(request, tmp_path):
    if request.param == "sqlite":
        yield Store(tmp_path / "s.sqlite")
        return
    uri = request.getfixturevalue("mongo_uri")
    s = MongoStore(uri, f"t_{uuid.uuid4().hex[:8]}")
    yield s
    s.db.client.drop_database(s.db.name)
    s.close()


def test_doc_meta_handles_every_stage_shape():
    assert doc_meta({"date": "2026-11-02", "paper_id": "p"}) == ("2026-11-02", "p")
    assert doc_meta({"ingestion_metadata": {"published_at": "2026-11-02T00:00:00Z"}, "newspaper": {"paper_id": "q"}}) == ("2026-11-02", "q")
    assert doc_meta({}) == (None, None)


@docker
def test_store_documents_contract(store):
    store.put("ocr", "a1", {"article_id": "a1", "date": "2026-11-02", "paper_id": "toi", "n": 1})
    store.put("ocr", "a1", {"article_id": "a1", "date": "2026-11-02", "paper_id": "toi", "n": 2})       # upsert
    store.put_many("ocr", [{"article_id": "a2", "date": "2026-11-03", "paper_id": "hindu"}], "article_id")
    store.put_many("ocr", [], "article_id")                                                           # no-op
    assert store.count("ocr") == 2 and store.get("ocr", "a1")["n"] == 2 and store.get("ocr", "zz") is None
    assert [d["article_id"] for d in store.all("ocr")] == ["a1", "a2"]
    assert [d["article_id"] for d in store.all("ocr", "2026-11-03")] == ["a2"] and store.all("ocr", "2030-01-01") == []
    assert not any(k.startswith("_") for k in store.get("ocr", "a1"))
    # an enriched-shaped doc (no top-level date) is still found by date
    store.put("enriched", "e1", {"event_id": "e1", "ingestion_metadata": {"published_at": "2026-11-02T00:00:00Z"},
                                 "newspaper": {"paper_id": "toi"}})
    assert [d["event_id"] for d in store.all("enriched", "2026-11-02")] == ["e1"]


@docker
def test_store_counters_contract(store):
    store.increment("k", "2026-11-01"); store.increment("k", "2026-11-01", 2); store.increment("k", "2026-11-02")
    store.increment("other", "2026-11-01")
    assert store.counter("k", "2026-11-01") == 3 and store.counter("k", "2026-11-09") == 0
    assert store.history("k", "2026-11-03") == [1, 3]                         # newest first
    assert store.history("k", "2026-11-02", days=5) == [3]
    assert store.days_of_history("2026-11-03") == 2 and store.days_of_history("2026-11-01") == 0


@docker
def test_store_roundtrips_a_real_enriched_doc_exactly(store, docs):
    d = copy.deepcopy(docs[0])
    d["credibility"]["cross_paper"] = {"headline_by_paper": {"Mid-Day.com": "x", "$odd key": "y"}}   # dots / $ in field names
    store.put("enriched", d["event_id"], d)
    assert store.get("enriched", d["event_id"]) == json.loads(json.dumps(d))


@docker
def test_mongo_collections_are_named_by_stage(mongo_uri):
    name = f"t_{uuid.uuid4().hex[:8]}"
    s = MongoStore(mongo_uri, name)
    try:
        s.put("pages", "p", {"x": 1}); s.put("ocr", "r", {"x": 1}); s.put("clean", "c", {"x": 1}); s.put("enriched", "e", {"x": 1})
        s.put("weird", "w", {"x": 1})
        assert set(s.db.list_collection_names()) >= {"pages", "raw_events", "clean_events", "enriched_events", "weird_events"}
        assert s.db["raw_events"].find_one({"_id": "r"})["x"] == 1
        assert any(v.get("unique") for v in s.db["counters"].index_information().values())
    finally:
        s._client.drop_database(name); s.close()


@docker
def test_shared_mongo_client_survives_store_close(mongo_uri):
    st = Settings(store_backend="mongo", mongo_uri=mongo_uri, mongo_db=f"t_{uuid.uuid4().hex[:8]}")
    shared = {}
    a, b = make_store(st, shared), make_store(st, shared)
    a.put("ocr", "k", {"x": 1})
    a.close()
    assert b.get("ocr", "k")["x"] == 1                       # the client a shared with b is still open
    b.db.client.drop_database(st.mongo_db)
    shared["mongo_client"].close()
    assert isinstance(make_store(Settings(store_backend="sqlite", db_path=":memory:")), Store)


# ================================================================================================= pgvector
@pytest.fixture()
def table():
    return "t_" + uuid.uuid4().hex[:8]


def unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


@docker
def test_pgvector_search_matches_in_memory_index(pg_dsn, table):
    pg, mem = PgVectorIndex(pg_dsn, 3, table), InMemoryIndex()
    items = [("a", unit(1, 0, 0), "2026-11-01"), ("b", unit(0.9, 0.4, 0), "2026-11-02"), ("c", unit(0, 1, 0), "2026-11-02"),
             ("d", unit(0.8, 0.5, 0.2), "2026-11-09")]
    for i, v, day in items:
        payload = {"date": day, "paper_id": "p", "story_id": f"s-{i}"}
        pg.add(i, v, payload); mem.add(i, v, payload)
    q = unit(1, 0.1, 0)
    for flt in (None, DateWindow(dt.date(2026, 11, 2), 1), lambda p: p["story_id"] != "s-a"):
        a, b = pg.search(q, 3, flt), mem.search(q, 3, flt)
        assert [x[0] for x in a] == [x[0] for x in b]
        assert [round(x[1], 4) for x in a] == [round(x[1], 4) for x in b]
    assert [x[0] for x in pg.search(q, 5, DateWindow(dt.date(2026, 11, 2), 0))] == ["b", "c"]       # d is outside the window
    assert len(pg) == 4 and PgVectorIndex(pg_dsn, 3, table).search(q, 1)[0][0] == "a"             # persisted across connections
    pg.close()


@docker
def test_pgvector_upsert_relabel_and_validation(pg_dsn, table):
    pg = PgVectorIndex(pg_dsn, 3, table)
    pg.add("a", unit(1, 0, 0), {"date": "2026-11-01", "paper_id": "p", "story_id": "s1", "v": 1})
    pg.add("a", unit(0, 1, 0), {"date": "2026-11-01", "paper_id": "p", "story_id": "s1", "v": 2})      # upsert, new vector
    assert len(pg) == 1 and pg.search(unit(0, 1, 0), 1)[0][2]["v"] == 2
    pg.add("b", unit(0, 0, 1), {"date": "2026-11-01", "paper_id": "q", "story_id": "s2"})
    assert pg.relabel_story("s2", "s1") == 1
    assert {r[2]["story_id"] for r in pg.search(unit(1, 1, 1), 5)} == {"s1"}
    with pytest.raises(ValueError, match="3-dim vectors"):
        PgVectorIndex(pg_dsn, 8, table)
    with pytest.raises(ValueError, match="bad table"):
        PgVectorIndex(pg_dsn, 3, "x; DROP TABLE y")
    pg.close()


@docker
def test_pgvector_uses_hnsw_when_allowed_and_exact_scan_above_2000_dims(pg_dsn, table):
    small = PgVectorIndex(pg_dsn, 1024, table)
    big = PgVectorIndex(pg_dsn, 4096, table + "_big")
    idx = lambda t: {r[0] for r in small._conn.execute("select indexname from pg_indexes where tablename = %s", (t,)).fetchall()}
    assert f"{table}_hnsw" in idx(table) and f"{table}_big_hnsw" not in idx(table + "_big")
    v = np.random.default_rng(1).normal(size=4096).astype(np.float32); v /= np.linalg.norm(v)
    big.add("x", v, {"date": "2026-11-01", "paper_id": "p"})
    assert big.search(v, 1)[0][0] == "x" and big.search(v, 1)[0][1] == pytest.approx(1.0, abs=1e-3)
    small.close(); big.close()


def _articles_from(docs_):
    return [dict(event_id=d["event_id"], paper_id=d["newspaper"]["paper_id"], paper_name=d["newspaper"]["paper"],
                 page_no=d["newspaper"]["page_no"], date="2026-11-02", city=d["newspaper"]["city"],
                 wire_source=d["newspaper"]["wire_source"],
                 working_headline=d["newspaper"]["translated_headline"] or d["newspaper"]["headline_clean"],
                 working_text=d["newspaper"]["translated_text"] or d["newspaper"]["text"],
                 headline=d["newspaper"]["headline_clean"], text=d["newspaper"]["text"]) for d in docs_]


@docker
def test_pgvector_clustering_equals_in_memory(docs, pg_dsn, table):
    e = HashingEmbedder(dim=1024)
    mem = StoryClusterer(e, InMemoryIndex()).cluster_all(_articles_from(docs))
    pg = StoryClusterer(e, PgVectorIndex(pg_dsn, 1024, table)).cluster_all(_articles_from(docs))
    key = lambda xs: sorted(sorted(a["event_id"] for a in xs if a["story_id"] == s) for s in {a["story_id"] for a in xs})
    assert key(mem) == key(pg) and len(key(pg)) < len(pg)


@docker
def test_story_matching_continues_across_runs(pg_dsn, table):
    """The point of a persistent vector index: tomorrow's paper joins the story a different run already stored."""
    def art(i, paper, headline, text):
        return {"event_id": f"{i:064d}", "paper_id": paper, "paper_name": paper, "page_no": 1, "date": "2026-11-02",
                "working_headline": headline, "working_text": text, "city": "Chandigarh", "wire_source": None,
                "headline": headline, "text": text}
    t1 = ("Kunwar Vijay Pratap Singh resigned from the Punjab Assembly on Monday, becoming the third AAP legislator to leave "
          "this year. He submitted his resignation to the Speaker in Chandigarh. Opposition parties demanded a by-election.")
    t2 = ("Punjab MLA Kunwar Vijay Pratap Singh quit the Assembly on Monday, the third AAP legislator to leave. The resignation "
          "was handed to the Speaker in Chandigarh and the Opposition demanded a by-election without delay.")
    run1 = StoryClusterer(HashingEmbedder(dim=1024), PgVectorIndex(pg_dsn, 1024, table))
    a1 = run1.cluster_all([art(1, "the_hindu", "Punjab MLA resigns", t1)])
    run1.index.close()
    run2 = StoryClusterer(HashingEmbedder(dim=1024), PgVectorIndex(pg_dsn, 1024, table))          # new process, same database
    a2 = run2.cluster_all([art(2, "toi", "AAP loses another MLA", t2)])
    assert a2[0]["story_id"] == a1[0]["story_id"] and a2[0]["cluster"]["cluster_status"] == "joined_story"
    assert a2[0]["cluster"]["matches"][0]["event_id"] == f"{1:064d}"
    # re-processing the same article must not match itself, and an unrelated one stays separate
    again = StoryClusterer(HashingEmbedder(dim=1024), PgVectorIndex(pg_dsn, 1024, table)).cluster_all([art(2, "toi", "AAP loses another MLA", t2)])
    assert again[0]["story_id"] == a1[0]["story_id"] and all(m["event_id"] != f"{2:064d}" for m in again[0]["cluster"]["matches"])
    other = run2.cluster_all([art(3, "ie", "RBI holds rate", "The Reserve Bank of India kept the repo rate at 6.5 per cent on Monday in Mumbai.")])
    assert other[0]["story_id"] != a1[0]["story_id"]


# ================================================================================================= neo4j: pure row building
def _doc(eid, paper, story, nodes, edges, entities=()):
    return {"event_id": eid, "newspaper": {"paper_id": paper, "paper": paper.title(), "headline": f"h-{eid}", "page_no": 1,
                                           "ocr_confidence": 1.0, "language": "en"},
            "event": {"type": "Politics.Resignation", "date": "2026-11-02"}, "story": {"story_id": story, "n_papers": 2},
            "credibility": {"source_reputation": "high"}, "geo": {"primary_location": "Punjab"}, "classification": {"topic": "politics"},
            "impact_score": {"value": 0.5}, "summary": {"multi_doc_summary": "s"}, "sentiment": {"document": {"label": "neutral"}},
            "dedup_clustering": {"suggested_story_key": "k"}, "entities": list(entities),
            "knowledge_graph": {"nodes": nodes, "edges": edges}}


def test_graph_rows_merge_acronyms_and_share_edges_across_papers():
    ents = [{"text": "AAP", "type": "ORG"}, {"text": "Aam Aadmi Party", "type": "ORG"}, {"text": "K. Singh", "type": "PERSON"}]
    a = _doc("e1", "hindu", "s1", [{"id": "k_singh", "label": "Person"}, {"id": "aap", "label": "PoliticalParty"}],
             [{"from": "k_singh", "rel": "ASSOCIATED_WITH", "to": "aap"}], ents)
    b = _doc("e2", "toi", "s1", [{"id": "k_singh", "label": "Person"}, {"id": "aam_aadmi_party", "label": "PoliticalParty"}],
             [{"from": "k_singh", "rel": "ASSOCIATED_WITH", "to": "aam_aadmi_party"}], ents)
    rows = build_graph_rows([a, b])
    assert {e["id"] for e in rows["entities"]["PoliticalParty"]} == {"aam_aadmi_party"}                # AAP folded in
    assert rows["entities"]["PoliticalParty"][0]["name"] == "Aam Aadmi Party"
    edge = rows["relations"]["ASSOCIATED_WITH"]
    assert len(edge) == 1 and edge[0]["event_ids"] == ["e1", "e2"]                                      # one edge, two sources
    assert len(rows["mentions"]) == 4 and {m["entity_id"] for m in rows["mentions"]} == {"k_singh", "aam_aadmi_party"}
    assert len(rows["stories"]) == 1 and len(rows["articles"]) == 2 and {p["id"] for p in rows["papers"]} == {"hindu", "toi"}


def test_graph_rows_drop_unsafe_or_dangling_input():
    d = _doc("e1", "hindu", "s1",
             [{"id": "a", "label": "Person"}, {"id": "b", "label": "Organization"}, {"id": "c", "label": "Evil`) MATCH (n) DETACH DELETE n //"}],
             [{"from": "a", "rel": "WORKS_FOR", "to": "b"},
              {"from": "a", "rel": "X`]-() MATCH (n) DETACH DELETE n //", "to": "b"},                    # injection attempt in rel type
              {"from": "a", "rel": "lowercase", "to": "b"},
              {"from": "a", "rel": "KNOWS", "to": "ghost"}])                                            # endpoint is not an entity node
    rows = build_graph_rows([d])
    assert set(rows["relations"]) == {"WORKS_FOR"}
    assert set(rows["entities"]) <= ENTITY_LABELS and "c" not in {e["id"] for v in rows["entities"].values() for e in v}


def test_graph_rows_story_impact_is_max_and_event_id_fallback():
    a, b = _doc("e1", "hindu", None, [], []), _doc("e2", "toi", "s1", [], [])
    b["impact_score"]["value"] = 0.9
    c = _doc("e3", "ie", "s1", [], []); c["impact_score"]["value"] = 0.7
    rows = build_graph_rows([a, b, c])
    by = {s["story_id"]: s for s in rows["stories"]}
    assert by["s1"]["impact"] == 0.9 and "e1" in by                                                     # no story -> keyed by event id


def test_value_relations_are_not_graph_edges(docs):
    rows = build_graph_rows(docs)
    assert "HOLDS_RATE_AT" not in rows["relations"]
    assert not any(e["id"].endswith("per_cent") for v in rows["entities"].values() for e in v)
    assert not any(r for r in rows["relations"] if r.startswith("APPOINTS"))                            # no guessed relations
    resign = rows["relations"]["RESIGNED_FROM"]
    assert len(resign) == 1 and len(resign[0]["event_ids"]) >= 3                                        # one edge reported by 3+ papers


# ================================================================================================= neo4j: real database
@pytest.fixture()
def graph(neo4j_conf):
    g = Neo4jGraph(neo4j_conf["uri"], neo4j_conf["user"], neo4j_conf["password"])
    with g._session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    yield g
    g.close()


def cypher(g, q, **kw):
    with g._session() as s:
        return s.run(q, **kw).values()


@docker
def test_neo4j_graph_shape_for_a_story(graph, docs):
    summary = graph.write(docs)
    assert summary["articles"] == len(docs) and summary["papers"] == len({d["newspaper"]["paper_id"] for d in docs})
    sid = next(d["story"]["story_id"] for d in docs if d["event"]["type"] == "Politics.Resignation")
    n_art = sum(d["story"]["story_id"] == sid for d in docs)
    assert cypher(graph, "MATCH (:Story {story_id:$s})<-[:PART_OF]-(a:Article) RETURN count(a)", s=sid)[0][0] == n_art >= 3
    assert cypher(graph, "MATCH (a:Article)-[:PUBLISHED_IN]->(p:Paper) RETURN count(DISTINCT p)")[0][0] == summary["papers"]
    person = cypher(graph, "MATCH (e:Entity:Person {id:'kunwar_vijay_pratap_singh'}) RETURN e.name, size([(a)-[:MENTIONS]->(e) | a])")[0]
    assert person[0] == "Kunwar Vijay Pratap Singh" and person[1] >= 3                                 # ONE node, many papers
    edge = cypher(graph, "MATCH (:Entity {id:'kunwar_vijay_pratap_singh'})-[r:RESIGNED_FROM]->(:Entity {id:'punjab_assembly'}) "
                         "RETURN r.event_ids, r.date")
    assert len(edge) == 1 and len(edge[0][0]) >= 3 and edge[0][1] == "2026-11-02"
    assert cypher(graph, "MATCH (e:Entity {id:'aap'}) RETURN count(e)")[0][0] == 0                      # acronym folded into the full name
    assert cypher(graph, "MATCH (:Entity:PoliticalParty {id:'aam_aadmi_party'}) RETURN count(*)")[0][0] == 1
    assert cypher(graph, "MATCH (e:Entity) WHERE e.id ENDS WITH 'per_cent' RETURN count(e)")[0][0] == 0


@docker
def test_neo4j_write_is_idempotent(graph, docs):
    graph.write(docs)
    snapshot = lambda: (sorted(cypher(graph, "MATCH (n) RETURN labels(n)[0], count(*)")),
                        sorted(cypher(graph, "MATCH ()-[r]->() RETURN type(r), count(*)")))
    before = snapshot()
    assert sum(c for _, c in before[1]) > 0
    graph.write(docs); graph.write(docs)
    assert snapshot() == before


@docker
def test_neo4j_reclustering_repoints_articles_and_drops_empty_stories(graph, docs):
    graph.write(docs)
    moved = copy.deepcopy(docs)
    victim = next(d for d in moved if d["event"]["type"] == "Politics.Resignation")
    old = victim["story"]["story_id"]
    for d in moved:                                               # merge the whole resignation story into another story id
        if d["story"]["story_id"] == old:
            d["story"] = {**d["story"], "story_id": "story-merged0001"}
    graph.write(moved)
    assert cypher(graph, "MATCH (s:Story {story_id:$s}) RETURN count(s)", s=old)[0][0] == 0           # emptied story removed
    assert cypher(graph, "MATCH (a:Article {event_id:$e})-[:PART_OF]->(s:Story) RETURN collect(s.story_id)", e=victim["event_id"])[0][0] == ["story-merged0001"]


@docker
def test_neo4j_story_graph_query(graph, docs):
    graph.write(docs)
    sid = next(d["story"]["story_id"] for d in docs if d["event"]["type"] == "Politics.Resignation")
    g = graph.story_graph(sid)
    assert len(g["articles"]) >= 3 and {a["paper"] for a in g["articles"]} >= {"The Hindu", "The Times of India"}
    assert g["entities"][0]["mentioned_in"] >= 3
    rel = next(r for r in g["relations"] if r["rel"] == "RESIGNED_FROM")
    assert (rel["from_name"], rel["to_name"]) == ("Kunwar Vijay Pratap Singh", "Punjab Assembly") and len(rel["papers"]) >= 3
    assert graph.story_graph("story-doesnotexist") == {"story_id": "story-doesnotexist", "articles": [], "entities": [], "relations": []}


@docker
def test_neo4j_hostile_text_is_stored_as_data_not_executed(graph, docs):
    evil = copy.deepcopy(docs[:1])
    evil[0]["newspaper"]["headline"] = "x'}) MATCH (n) DETACH DELETE n //"
    evil[0]["newspaper"]["paper"] = "Paper `with` \"quotes\""
    graph.write(docs)
    n = cypher(graph, "MATCH (n) RETURN count(n)")[0][0]
    graph.write(evil)
    assert cypher(graph, "MATCH (n) RETURN count(n)")[0][0] >= n
    assert cypher(graph, "MATCH (a:Article {event_id:$e}) RETURN a.headline", e=evil[0]["event_id"])[0][0].startswith("x'})")


# ================================================================================================= all three together
@docker
def test_pipeline_writes_to_mongo_pgvector_and_neo4j(manifest, tmp_path, mongo_uri, pg_dsn, neo4j_conf):
    tag = uuid.uuid4().hex[:8]
    s = Settings(data_dir=str(tmp_path / "data"), db_path=str(tmp_path / "p.sqlite"), render_dpi=40, store_backend="mongo",
                 mongo_uri=mongo_uri, mongo_db=f"t_{tag}")
    store = make_store(s)
    index = PgVectorIndex(pg_dsn, 1024, f"emb_{tag}")
    graph = Neo4jGraph(neo4j_conf["uri"], neo4j_conf["user"], neo4j_conf["password"])
    with graph._session() as sess:
        sess.run("MATCH (n) DETACH DELETE n").consume()
    stages = []
    p = Pipeline(s, store, PhraseTableBackend(), clusterer=StoryClusterer(HashingEmbedder(dim=1024), index, settings=s),
                 graph=graph, render_pages=False, on_stage=stages.append)
    res = p.run(manifest, tmp_path / "out")
    assert stages == ["ingest", "ocr", "clean", "cluster", "enrich", "graph", "export"]

    n = res.metrics["articles"]
    assert store.count("ocr") == store.count("clean") == store.count("enriched") == n         # raw events live in Mongo
    assert store.get("ocr", store.all("ocr")[0]["article_id"])["ocr_confidence"] == 1.0
    assert {d["story"]["story_id"] for d in store.all("enriched")} == {d["story"]["story_id"] for d in res.docs}
    assert len(index) == n and res.metrics["multi_paper_stories"] >= 3                       # embeddings live in pgvector
    assert index._conn.execute(f"select count(distinct story_id) from emb_{tag}").fetchone()[0] == res.metrics["stories"]
    assert res.metrics["graph"]["articles"] == n                                              # relationships live in Neo4j
    assert cypher(graph, "MATCH (a:Article) RETURN count(a)")[0][0] == n
    assert cypher(graph, "MATCH (s:Story) RETURN count(s)")[0][0] == res.metrics["stories"]
    sqlite_docs = Pipeline(Settings(data_dir=str(tmp_path / "d2"), db_path=str(tmp_path / "q.sqlite"), render_dpi=40),
                           Store(tmp_path / "q.sqlite"), PhraseTableBackend(), clusterer=StoryClusterer(HashingEmbedder(dim=1024)),
                           render_pages=False).run(manifest).docs
    assert groups(res.docs) == groups(sqlite_docs)                                            # same answers as the local backends
    p.close(); p.close()                                                                      # close twice is safe
    store.db.client.drop_database(s.mongo_db)


# ================================================================================================= relationship graph: the full model
def test_graph_rows_for_the_new_node_types_use_only_clean_data(docs):
    from newspaper_pipeline.graph.clean import clean_authors
    rows = build_graph_rows(docs)
    n = len(docs)
    assert all(a["published"] for a in rows["articles"])
    assert len(rows["links"]["ON_PAGE"]) >= n and {p["paper_id"] for p in rows["pages"]} == {d["newspaper"]["paper_id"] for d in docs}
    assert len(rows["editions"]) >= 1 and rows["days"] == sorted(set(rows["days"]))
    saved = sum(len(d["dedup_clustering"].get("matches") or []) + len(d["dedup_clustering"].get("review_candidates") or []) for d in docs)
    assert len(rows["similarity"]) == saved > 0                                             # every saved pair becomes a link, none invented
    assert all(0 <= r["score"] <= 1 and r["rel"] in ("SIMILAR_TO", "POSSIBLE_MATCH") for r in rows["similarity"])
    for rel, cap in (("HAS_KEYWORD", 6), ("MAKES_CLAIM", 3)):
        counts = {}
        for r in rows["links"].get(rel, []):
            counts[r["event_id"]] = counts.get(r["event_id"], 0) + 1
        assert max(counts.values(), default=0) <= cap
    assert all(clean_authors(a["name"]) for a in rows["authors"])                          # nothing junk became an author
    assert not any(e["type"].startswith("General") for e in rows["events"])                # generic events do not get a node
    assert {t["id"] for t in rows["topics"]} == {d["classification"]["topic"] for d in docs}


def _counts(g):
    return (dict(cypher(g, "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*)")),
            dict(cypher(g, "MATCH ()-[r]->() RETURN type(r), count(*)")))


@docker
def test_neo4j_full_model_matches_the_rows_and_the_saved_similarity(graph, docs):
    rows = build_graph_rows(docs)
    summary = graph.write(docs)
    for label, key in (("Edition", "editions"), ("Day", "days"), ("Page", "pages"), ("Section", "sections"), ("Author", "authors"),
                       ("Topic", "topics"), ("Event", "events"), ("Claim", "claims"), ("Keyword", "keywords")):
        assert cypher(graph, f"MATCH (n:{label}) RETURN count(n)")[0][0] == len(rows[key]) == summary[key], label
    links = {(a, b, rel): (score, cross) for a, b, rel, score, cross in cypher(
        graph, "MATCH (a:Article)-[x:SIMILAR_TO|POSSIBLE_MATCH]->(b:Article) RETURN a.event_id, b.event_id, type(x), x.score, x.cross_paper")}
    expected = {(r["a"], r["b"], r["rel"]): r["score"] for r in rows["similarity"]}
    assert {k: v[0] for k, v in links.items()} == expected                                  # same pairs, same scores as the engine saved
    paper = {d["event_id"]: d["newspaper"]["paper_id"] for d in docs}
    assert all(cross == (paper[a] != paper[b]) for (a, b, _), (_, cross) in links.items())
    assert cypher(graph, "MATCH (:Article)-[:ON_PAGE]->(:Page)-[:ON_DAY]->(d:Day) RETURN count(DISTINCT d)")[0][0] == len(rows["days"])


@docker
def test_neo4j_write_is_repeatable_and_replaces_changed_links(graph, docs):
    graph.write(docs)
    before = _counts(graph)
    graph.write(docs)
    assert _counts(graph) == before                                                         # writing again duplicates nothing

    changed = copy.deepcopy(docs[0])
    eid = changed["event_id"]
    incoming_before = cypher(graph, "MATCH ()-[x:SIMILAR_TO|POSSIBLE_MATCH]->(a:Article {event_id:$e}) RETURN count(x)", e=eid)[0][0]
    old_topic = changed["classification"]["topic"]
    changed["classification"]["topic"] = "zz_new_topic"
    changed["keyphrases"] = ["a brand new phrase"]
    changed["dedup_clustering"]["matches"] = []
    changed["dedup_clustering"]["review_candidates"] = []
    graph.write([changed])
    assert [r[0] for r in cypher(graph, "MATCH (:Article {event_id:$e})-[:HAS_TOPIC]->(t:Topic) RETURN t.id", e=eid)] == ["zz_new_topic"]
    assert [r[0] for r in cypher(graph, "MATCH (:Article {event_id:$e})-[:HAS_KEYWORD]->(k:Keyword) RETURN k.id", e=eid)] == ["a brand new phrase"]
    assert cypher(graph, "MATCH (:Article {event_id:$e})-[x:SIMILAR_TO|POSSIBLE_MATCH]->() RETURN count(x)", e=eid)[0][0] == 0      # replaced, not kept
    assert cypher(graph, "MATCH ()-[x:SIMILAR_TO|POSSIBLE_MATCH]->(a:Article {event_id:$e}) RETURN count(x)", e=eid)[0][0] == incoming_before
    if not any(d["classification"]["topic"] == old_topic for d in docs[1:]):
        assert cypher(graph, "MATCH (t:Topic {id:$t}) RETURN count(t)", t=old_topic)[0][0] == 0   # a node nothing points to is removed


@docker
def test_neo4j_run_graph_summary_filters_cap_and_neighbours(graph, docs):
    graph.write(docs)
    ids = [d["event_id"] for d in docs]
    summ = graph.run_summary(ids)
    assert summ["nodes_by_type"]["Article"] == len(docs)
    assert summ["nodes_by_type"]["Paper"] == len({d["newspaper"]["paper_id"] for d in docs})
    assert summ["links_by_type"]["SIMILAR_TO"] == cypher(graph, "MATCH ()-[x:SIMILAR_TO]->() RETURN count(x)")[0][0]

    g = graph.run_graph(ids, {"Paper", "Story"}, 0.0, 500)
    types = {n["type"] for n in g["nodes"]}
    assert types == {"Article", "Paper", "Story"} and g["truncated"] is False
    assert {e["type"] for e in g["edges"]} >= {"PUBLISHED_IN", "PART_OF", "SIMILAR_TO"} - {"SIMILAR_TO"}
    node_ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in node_ids and e["target"] in node_ids for e in g["edges"])      # no edge points at a missing node
    assert all(n["id"].startswith(n["type"] + ":") and n["label"] for n in g["nodes"])

    sim_scores = [e["props"]["score"] for e in graph.run_graph(ids, set(), 0.0, 500)["edges"] if e["type"] == "SIMILAR_TO"]
    assert sim_scores and not [e for e in graph.run_graph(ids, set(), 0.999, 500)["edges"] if e["type"] in ("SIMILAR_TO", "POSSIBLE_MATCH")]
    assert [e for e in graph.run_graph(ids, set(), min(sim_scores), 500)["edges"] if e["type"] == "SIMILAR_TO"]

    rich = graph.run_graph(ids, {"Paper", "Story", "Entity", "Keyword", "Topic", "Page", "Day", "Edition"}, 0.0, 2000)
    assert {"Entity", "Keyword", "Topic", "Page", "Day", "Edition"} <= {n["type"] for n in rich["nodes"]}
    capped = graph.run_graph(ids, {"Paper", "Story", "Entity", "Keyword", "Topic", "Page", "Day", "Edition"}, 0.0, 30)
    assert capped["truncated"] is True and len(capped["nodes"]) == 30 and capped["total_nodes"] == len(rich["nodes"])
    kept_types = {n["type"] for n in capped["nodes"]}
    assert {"Paper", "Story", "Entity", "Keyword", "Topic", "Page", "Day", "Edition"} <= kept_types   # a cap never drops a whole type
    assert sum(n["type"] == "Article" for n in capped["nodes"]) >= 1

    only = graph.run_graph(ids[:2], {"Paper"}, 0.0, 100)
    assert {n["id"] for n in only["nodes"] if n["type"] == "Article"} == {f"Article:{i}" for i in ids[:2]}   # the run's own articles only

    ent = next(n["id"] for n in rich["nodes"] if n["type"] == "Entity")
    nb = graph.neighbors(ent, {"Article", "Paper"}, ids[:3], 50)
    assert all(n["type"] in ("Article", "Paper") for n in nb["nodes"]) and all(n["id"] in {f"Article:{i}" for i in ids[:3]} or n["type"] == "Paper" for n in nb["nodes"])
    assert graph.neighbors("Nonsense:x", {"Article"}, None, 10) == {"nodes": [], "edges": []}
    assert graph.neighbors("Entity:x`) MATCH (n) DETACH DELETE n //", {"Article"}, None, 10)["nodes"] == []
    assert cypher(graph, "MATCH (a:Article) RETURN count(a)")[0][0] == len(docs)             # the injection attempt deleted nothing
