# Newspaper intelligence pipeline (standalone test run)

E-paper PDFs from several national newspapers -> OCR / layout -> clean + translate -> **cross-paper story
clustering** -> enrichment -> one JSON per article in the [`../sample.json`](../sample.json) schema, plus a
`stories.json` that groups the same news across papers.

Architecture diagram: [`../newspaper-ocr-pipeline.excalidraw`](../newspaper-ocr-pipeline.excalidraw).
Not part of ARGUS - it is its own stack. By default it runs locally on SQLite; with Docker it runs against MongoDB, Postgres+pgvector
and Neo4j (see **Docker stack**). It reuses ARGUS code where it fit (table below).

## Quick start

```bash
cd Newspaper
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest                              # 135 tests, ~13 s
.venv/bin/python -m newspaper_pipeline demo --out out   # synthetic papers, no models/GPU needed
.venv/bin/streamlit run newspaper_pipeline/output/review_app.py -- --out out   # optional UI (pip install streamlit)
```

With real papers: copy `manifest.example.yaml`, point `pdf:` (or `url:`) at each edition, then

```bash
.venv/bin/python -m newspaper_pipeline run --manifest manifest.yaml --out out \
    --ocr rapidocr --translator aws --embedder hashing --index memory
```

Outputs in `out/`: `articles/<paper>/<id>.json` (sample.json schema + `newspaper` and `story` blocks),
`stories.json`, `review.json` (low-confidence OCR, garbled text, broken continuations, failed translation,
borderline story matches), `metrics.json`.

## Docker stack (everything containerised)

```bash
cp .env.example .env            # change the passwords for anything beyond a laptop
docker compose up -d --build    # first build is ~2 min (the api image bundles the OCR runtime, ~1.5 GB)
open http://localhost:8080      # upload UI (port = WEB_PORT in .env);  docker compose down  stops it,  down -v also deletes the data
```

| Service | Image | Role | Data |
|---|---|---|---|
| `web` | nginx + built Vite UI | UI on `:8080`, proxies `/api` to the API (streams large PDF uploads) | - |
| `api` | `Dockerfile.api` (Python 3.12) | FastAPI: accepts uploads, **publishes jobs to Kafka**, serves results. Does no pipeline work | uploads/outputs in volume `api-data` |
| `kafka` | `apache/kafka:3.8.0` (KRaft, single node) | carries every hop: topics `newspaper.jobs.extract`, `.events.clean`, `.jobs.enrich`, `.events.enriched` | volume `kafka-data` |
| `extract-worker` | same image | **phase 1**: consumes `jobs.extract` -> PDF -> OCR + layout -> clean + translate -> publishes `events.clean` | shares `api-data` |
| `datasync` | same image | the **only writer of pipeline documents to MongoDB**: consumes `events.clean` / `events.enriched`, upserts, then marks the run done (and queues enrichment if it was asked for) | shares `api-data` |
| `enrich-worker` | same image | **phase 2**: consumes `jobs.enrich` -> reads the clean events back from Mongo -> cluster + enrich (NLP models, pgvector, Neo4j) -> publishes `events.enriched` | shares `api-data` / `hf-models` |
| `mongo` | `mongo:7` | **raw events**: collections `pages`, `raw_events` (articles straight from OCR), `clean_events`, `enriched_events`, `counters` | volume `mongo-data` |
| `postgres` | `pgvector/pgvector:pg16` | **embeddings**: table `article_embeddings` (vector + HNSW index, story id, date) used for cross-paper matching, also across runs | volume `pg-data` |
| `neo4j` | `neo4j:5-community` | **relationships**: `Paper <- Article -> Story`, `Article -> Entity`, and `Entity -[RESIGNED_FROM, ASSOCIATED_WITH, LOCATED_IN, HEADS...]-> Entity` | volume `neo4j-data` |

Startup order is enforced by health checks (`api` waits for all three databases, `web` waits for `api`). Data stores publish
ports on `127.0.0.1` only (Mongo 27017, Postgres 5432, Neo4j browser 7474 / bolt 7687), so you can inspect them:

```bash
docker compose exec mongo mongosh -u newspaper -p newspaper-dev --authenticationDatabase admin newspaper
docker compose exec postgres psql -U newspaper -d newspaper -c "select count(*), count(distinct story_id) from article_embeddings"
open http://localhost:7474     # MATCH (s:Story)<-[:PART_OF]-(a:Article)-[:PUBLISHED_IN]->(p:Paper) RETURN s, a, p
```

How it behaves: every write is an upsert/MERGE keyed by the article's event id, so uploading the same PDFs again changes
nothing in any database (verified by uploading twice and comparing counts). Because embeddings persist, a paper uploaded
tomorrow can join a story first seen today (candidate window is +-48 h). Without Docker nothing changes: the same code uses
SQLite + in-memory vectors + no graph (`NP_STORE=sqlite NP_INDEX=memory NP_GRAPH=none`, the defaults). Backends are env-selected
(`NP_STORE` sqlite|mongo, `NP_INDEX` memory|qdrant|pgvector, `NP_GRAPH` none|neo4j, `NP_MONGO_URI`, `NP_PG_DSN`, `NP_NEO4J_*`,
`NP_EMBED_DIM` - 1024 in compose; pgvector builds an HNSW index only up to 2000 dims) or CLI flags `--store/--index/--graph`.
Changing `NP_EMBED_DIM` on an existing database is refused with a clear error (the table's dimension is fixed).

Tests: `pytest -m "not docker"` is the fast offline set (161 tests). `pytest -m docker` (19 tests) starts **throwaway** Mongo,
pgvector and Neo4j containers on random ports - it never touches the compose stack's data - and checks store contract
parity with SQLite, pgvector parity with the in-memory index, cross-run story matching, graph shape/idempotency/injection safety,
and one test that runs the whole pipeline into all three databases at once.

## The flow: everything passes through Kafka

```
upload --> api --[jobs.extract]--> extract-worker   PDF -> OCR + layout -> clean + translate
                                        |
                                  [events.clean]    pages, raw_events, clean_events + an "extract_done" marker
                                        v
                                    datasync --> MongoDB        run is DONE only once its documents are stored
                                        |  (only if "Run enrichment" was on, or later via the button)
                                  [jobs.enrich]     pointers only: run_id, run_date, clean event_ids
                                        v
                                  enrich-worker     reads clean_events from MongoDB -> cluster + enrich (+ pgvector, Neo4j)
                                        |
                                 [events.enriched]  enriched_events + an "enrich_done" marker
                                        v
                                    datasync --> MongoDB        enrichment is DONE only once stored
```

* **datasync is the only service that writes pipeline documents to MongoDB.** The workers' store is a `BusStore`: reads and the
  daily burst/novelty counters go to the database, but every document they produce is published to Kafka instead
  (`busstore.py`). The extract worker never even connects to Mongo.
* The upload page has a **"Run enrichment after extraction"** switch (off by default). Off: the run ends once the cleaned,
  translated articles are stored, and the run page shows them (Articles tab). A finished run has a **Run enrichment** button
  (which publishes the same `jobs.enrich` message); a failed enrichment has **Retry**.
* The enrichment job is one message per *run*, not per article, because story clustering needs the whole batch.
* A run's documents and its marker share a key and the topics have one partition, so the marker is consumed after the documents.
  datasync checks that every article named in the marker is really in Mongo before marking anything done.
* Delivery is at-least-once: offsets are committed after a batch is handled and every write is an upsert, so redelivery is harmless.
  If MongoDB is down, datasync does not commit; it restarts and Kafka redelivers, so no document is lost.
  Progress and errors go to the run's `run.json` (shared volume), which the UI polls through the API.
* Without Kafka (`NP_BUS=inline`, the default outside compose) the API runs both phases itself and writes straight to the store:
  same code, no broker. `python -m newspaper_pipeline run` / `demo` also run both phases back to back.
* API: `POST /api/runs` takes `enrich=true`, `POST /api/runs/{id}/enrich` starts phase 2 later, `GET /api/runs/{id}/clean`
  returns the phase-1 articles. Env: `NP_BUS` (`inline|kafka`), `NP_KAFKA_BOOTSTRAP`, `NP_KAFKA_GROUP`, `NP_TOPIC_EXTRACT`,
  `NP_TOPIC_CLEAN`, `NP_KAFKA_TOPIC` (enrich jobs), `NP_TOPIC_ENRICHED`.

## Web UI (upload from the browser)

`frontend/` is a Vite + React + TypeScript app; `newspaper_pipeline/api/` is the FastAPI server it talks to. Upload one
e-paper PDF per newspaper (drag & drop; the name, language and date are guessed from the filename and editable), watch the
extraction stages (and, if switched on, the enrichment stages) progress, read the cleaned articles, then compare each story across papers side by side (original / English text, claims with
cross-paper verification, enrichment JSON) and review flagged articles.

```bash
# terminal 1 - API (each run gets its own folder under NP_WORKDIR, default data/web)
.venv/bin/uvicorn newspaper_pipeline.api.server:app --port 8000
# terminal 2 - UI on http://localhost:5173 (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

Production: `cd frontend && npm run build`; the API then serves `frontend/dist` itself, so `uvicorn ...` alone is enough.
Backends are chosen by env vars on the API: `NP_OCR` (`auto` = RapidOCR if installed, else none), `NP_TRANSLATOR`
(`none|aws|indictrans2`), `NP_EMBEDDER`, `NP_INDEX`, `NP_JUDGE`, `NP_LLM_REFINE=1`; limits `NP_MAX_PAPERS`, `NP_MAX_UPLOAD_MB`.
The UI warns when the server has no OCR engine (scanned PDFs get skipped) or no translator (non-English papers cannot be
matched). Runs execute one at a time; a run interrupted by a restart is marked failed.
Tests: `.venv/bin/python -m pytest` (API included) and `cd frontend && npm test`.

## Stages (one package per stage)

| Stage | Module | What it does |
|---|---|---|
| 1 Input | `input/` | YAML manifest, fetch/copy + SHA-256 dedupe of editions, split PDF into pages + 300 dpi images, detect text layer |
| 2 OCR + layout | `ocr/` | Block extraction (embedded text, or OCR engine for scans), headline/byline/caption/furniture classification, article segmentation, column reading order, "continued on page N" stitching, dateline/wire/section/byline extraction, quality gate (VLM fallback hook, review flag) |
| 3 Clean + translate | `clean/` | OCR cleanup (ligatures, de-hyphenation, furniture, reflow, glued punctuation), script-based language detection, translation to English with pluggable backends, canonical record (`input_quality`, `ingestion_metadata`, content hash) |
| 4 Dedup + clustering | `cluster/` | Embeddings -> ANN candidates (date window) -> pair score (entities, figures, quotes, shingles, dateline, penalties) -> optional LLM judge for borderline -> incremental clustering with union-find merges |
| 5 Enrichment | `enrich/` | Per article: entities, event, relations, keyphrases, geo, classification, sentiment, emotion/tone, credibility, claims, summary, timeline, knowledge graph, impact, forecast. Per story: multi-doc summary, cross-paper claim verification, framing comparison, burst/novelty counters |
| 6 Output | `output/` | JSON export, story views, review queue, Streamlit UI |

`pipeline.py` wires the stages through `store.py` (SQLite or MongoDB; each stage persists its output, so any stage can be
re-run or inspected). `cli.py` is the command line.

## Pluggable backends (all optional, imported lazily)

| Need | Default (offline) | Production option |
|---|---|---|
| Page text | embedded PDF text | `--ocr rapidocr` (CPU, tested), `tesseract`, `paddle` (GPU, Indic) |
| Translation | none (English only) | `--translator aws` (same service ARGUS uses), `indictrans2` (local, GPU) |
| Embeddings | hashing TF-IDF | `--embedder bge-m3` (multilingual, matches Hindi/English without translation) |
| Vector index | in-memory numpy | `--index pgvector` (Postgres, persistent, tested), `qdrant` (local mode, tested) |
| Document store | SQLite | `--store mongo` (tested) |
| Relationship graph | none | `--graph neo4j` (tested) |
| Pair judge | rules only | `--judge claude` for borderline pairs |
| Enrichment refinement | rules only | `--llm-refine` (Claude, whitelisted/validated fields) |

## Reused from ARGUS

| ARGUS source | Reused as |
|---|---|
| `etl-pipeline-svc/etl/clean.py` | `clean/text.py::clean_text` (HTML/entities/NFC/zero-width/whitespace) |
| `etl-pipeline-svc/etl/translate.py` | `looks_english` heuristic, UTF-8 truncation, AWS Translate retry/backoff + status stamping (`clean/translate.py`) |
| `etl-pipeline-svc/etl/dedup.py` | MinHash shingling for near-duplicate / wire-copy detection (`cluster/signals.py`) |
| `enrichment-svc/scoring.py` | conflict / casualty / economic / breaking keyword lists (`enrich/lexicons.py`) |
| `enrichment-svc/countries.py` | country-name -> ISO resolution (`enrich/entities.py`) |

ARGUS services use Kafka/Redis/Mongo and bare-module imports, so the code was adapted into this package instead of imported.

## What has and has not been validated

Tested (161 offline + 19 docker-backed Python tests, 28 frontend tests, plus manual Chromium runs of the full upload flow, including against the docker stack): every stage in isolation, plus the whole pipeline on synthetic multi-column newspapers (4 papers,
one in Hindi, one story continued across pages), including a real OCR run (RapidOCR) on rendered page images and the
Qdrant index. On the fixtures the same news is grouped across papers with zero false merges.

Not validated - be aware before trusting results on real papers:

- **No real newspaper PDFs were available.** Layout heuristics (font-size ratios, column adjacency) are tuned on the
  synthetic pages. Expect to adjust `classify_blocks` thresholds, or plug in a layout detector (DocLayout-YOLO) that sets
  `Block.kind` - `segment_articles` only needs kinds + geometry.
- **Scanned pages have no photo detector**, so captions are found by tiny type only; photos are not cropped.
- **Hindi/other languages need a translator or BGE-M3.** The tests use a phrase-table stand-in, not a real MT model.
  `IndicTrans2Backend` and `SentenceTransformerEmbedder` are written but untested here (no GPU / model download).
- **Enrichment is a rule-based baseline**: gazetteer/lexicon NER, trigger-based events, lexicon sentiment, extractive
  summaries. Entity linking is intentionally unresolved (`kb_id: null`, as in sample.json). `--llm-refine` improves a
  whitelist of fields but is only tested against a fake client.
- Story thresholds (`NP_SAME_STORY_THRESHOLD` 0.75, `NP_BORDERLINE_THRESHOLD` 0.50) and `cos_ref` are calibrated to the
  fixtures and the hashing embedder. Related-but-different articles land in the borderline band by design
  (`review.json` / judge) rather than being silently merged. Re-calibrate with a labelled sample of your papers.
- `published_at` is the edition date at 00:00Z (`published_precision: date_only`); e-papers carry no timestamp.
- The Streamlit UI needs `pip install streamlit`.
- Entity matching in the graph is by name only (acronyms like AAP are folded into their full names); two different people with the same name would merge.
- Docker images and credentials: defaults are for local development; the API has no authentication and is meant for local/test use; do not expose it publicly as is.
