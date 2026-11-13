# Implementation plan: relationship and similarity graph (Neo4j) with an interactive Graph tab

| | |
|---|---|
| Project | e-Paper OCR |
| Status | **Implemented, deployed and verified (2026-11-13).** See `docs/neo4j-graph-implementation-report.md` for what was built, the test results and the differences from this plan. |
| Date | 2026-11-13 |
| Author | Claude, for the project owner |
| Feature | When two or more newspapers are processed, build a relationship graph in Neo4j around the articles and their similarity, and show it to the user in the UI |

---

## 1. Goal

After a run with two or more newspapers finishes enrichment, the user can open a **Graph** tab and see how the newspapers, pages, days, sections, authors, articles, stories, entities, topics, events, claims and keywords are connected, including **which articles are similar and how strongly**. The graph is built automatically as part of the existing enrichment stage; the user does not have to do anything extra.

The 13 requested node types: Paper, Edition, Author, Page, Day, Section, Article, Story, Entity, Topic, Event, Claim, Keyword.

## 2. What exists today (verified in the code and the live system)

| Item | State |
|---|---|
| Neo4j 5 (community) | Running, healthy, volume `neo4j-data` |
| Graph writer | `newspaper_pipeline/graph/neo4j_graph.py` (169 lines): `build_graph_rows` (pure function, unit-testable) and `Neo4jGraph.write` (MERGE-based, safe to repeat) |
| Node types in the live graph | Paper 5, Article 1,323, Story 1,009, Entity 2,697 (People, Organisations, Parties, Locations) |
| Relationships in the live graph | MENTIONS 6,841; PUBLISHED_IN 1,323 (with page); PART_OF 1,323; a few entity-to-entity links (LOCATED_IN 30, RESIGNED_FROM 3, ASSOCIATED_WITH 2, HEADS 1) |
| Where the graph is written | `Pipeline.enrich_stored`, stage `graph`, after clustering and enrichment, when `NP_GRAPH=neo4j` (already on in the stack). It takes about 1.4 s for 60 articles. |
| Read API | `GET /api/runs/{run}/stories/{story}/graph` (one story only) |
| UI today | `Relationships.tsx`: a text list. **No graph is drawn, and the frontend has no graph-drawing library.** |
| Similarity data | Saved on every enriched article: `dedup_clustering.matches` (merged pairs) and `review_candidates` (borderline pairs), each with the score and its signal breakdown. Only runs processed since phase 1 have it. |
| Neo4j memory | heap 256 MB initial / 512 MB max, page cache 128 MB (small) |

So the database, the pipeline stage, the API connection and the similarity data already exist. This is an extension, not a new system.

## 3. Data readiness for each node type

Measured on the 1,323 enriched articles stored in MongoDB.

| Node | Source field | Coverage | Quality / issue | Plan |
|---|---|---|---|---|
| Paper | `newspaper.paper_id` | 100% | good | exists |
| Edition | `newspaper.edition` | 100% | only 2 values ("main", "Delhi") | include |
| Page | `newspaper.page_no` | 100% | good | include (key = paper + date + page) |
| Day | `ingestion_metadata.published_at` | 100% | the date **entered at upload**, not read from the PDF | include, labelled accordingly |
| Section | `newspaper.section` | **6%** (Business, Front Page, Nation) | scanned pages give no section | include only when present |
| Author | `newspaper.byline` | 60% | **junk**: 489 distinct values such as "+91 91488 62559", "1987", "(their ideological supporters)" | include only if a validator accepts it |
| Article | event id | 100% | good | exists |
| Story | `story.story_id` | 100% | good | exists |
| Entity | `entities` (84% of articles have at least 1, average 5.2) | 84% | NER errors on garbled text | exists |
| Topic | `classification.topic` | 100% | 11 topics; many low confidence | include (confidence on the link) |
| Event | `event` (type, trigger, actor, institution, date) | type present 100%, but **69% are "General.Report"**; actor present 59% | partly generic | shared Event node only when specific (section 4, D2) |
| Claim | `claims` | 96% have at least 1; 3,718 in total | long, garbled sentences; only 136 have `corroborated_by` | include on request, capped, checkworthy only |
| Keyword | `keyphrases` | 85%; 2,001 distinct, 1,303 appear in 2+ articles | some generic phrases | include after cleaning |

Rough size if everything is written for the whole stored corpus: about 11,000–12,000 nodes. A single run (60–400 articles) would add roughly 100–2,000 nodes. This drives the display limits in section 9.

## 4. Decisions needed from the owner (with my recommendation)

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | One shared graph for all runs, or a separate graph per run? | **One shared graph, filtered to the run in the UI** | The same person or place then links papers across uploads. The run's articles are known from `stories.json`, so no schema change is needed to filter. |
| D2 | What is an "Event" node? | **A shared Event node** keyed by type + actor + institution + date + main place, created only for non-generic events (type is not General.Report and an actor or institution exists) | Lets the graph show the same event reported by several papers. About 31% of articles qualify today. |
| D3 | Author and Section | **Only when they pass a quality check;** otherwise leave them out | Author is junk in many records and Section exists for 6%; including them blindly pollutes the graph. |
| D4 | Phasing | **MVP first, then the rest** (section 14) | Gets a working graph in about a week; the data-quality-heavy nodes come second. |
| D5 | Drawing library | **Cytoscape.js** (MIT licence) | Handles a few thousand nodes, has built-in layouts, filtering and styling. Alternatives: react-force-graph, vis-network. Adds a few hundred KB to the bundle (to be measured). |
| D6 | Neo4j memory | Decide **after measuring** in WP9; likely raise heap to 1 GB and page cache to 512 MB | Current values are small. |
| D7 | Old runs | **Rebuild their graph from stored data** with a script (WP8) | Old runs have no similarity links until rebuilt, and those processed before phase 1 have no saved match details at all. |

## 5. Target graph model

### 5.1 Nodes

| Label | Unique key | Main properties | New? |
|---|---|---|---|
| `Paper` | `id` | `name`, `reputation` | exists |
| `Edition` | `id` = `paper_id|edition` | `name`, `paper_id` | new |
| `Day` | `date` (YYYY-MM-DD) | `date` | new |
| `Page` | `id` = `paper_id|date|page_no` | `page_no`, `date`, `paper_id` | new |
| `Section` | `id` = `paper_id|section` | `name`, `paper_id` | new |
| `Author` | `id` = slug of the validated byline | `name` | new |
| `Article` | `event_id` | `headline`, `page_no`, `date`, `ocr_confidence`, `language`, `sentiment`, `event_type` | exists |
| `Story` | `story_id` | `key`, `event_type`, `location`, `topic`, `impact`, `n_papers`, `date` | exists |
| `Entity` (+ `Person`, `Organization`, `PoliticalParty`, `Location`) | `id` (slug) | `name`, `label` | exists |
| `Topic` | `id` = topic | `name` | new |
| `Event` | `id` = hash of type + actor + institution + date + place | `type`, `trigger`, `actor`, `institution`, `date`, `location` | new |
| `Claim` | `id` = hash of normalised claim text | `text` (trimmed), `status`, `checkworthy` | new |
| `Keyword` | `id` = normalised phrase | `text` | new |

### 5.2 Relationships

| Relationship | From → to | Properties | New? |
|---|---|---|---|
| `PUBLISHED_IN` | Article → Paper | `page` | exists |
| `PART_OF` | Article → Story | | exists |
| `MENTIONS` | Article → Entity | | exists |
| entity relations (`LOCATED_IN`, `RESIGNED_FROM`, ...) | Entity → Entity | `date`, `event_ids` | exists |
| `OF_PAPER` | Edition → Paper | | new |
| `IN_EDITION` | Page → Edition | | new |
| `ON_DAY` | Page → Day | | new |
| `ON_PAGE` | Article → Page | | new |
| `IN_SECTION` | Article → Section | | new |
| `WRITTEN_BY` | Article → Author | | new |
| `HAS_TOPIC` | Article → Topic | `confidence` | new |
| `REPORTS` | Article → Event | | new |
| `MAKES_CLAIM` | Article → Claim | | new |
| `SUPPORTED_BY` | Claim → Paper | | new (from `corroborated_by`) |
| `HAS_KEYWORD` | Article → Keyword | | new |
| **`SIMILAR_TO`** | Article → Article | `score`, `semantic`, `entity`, `keyword`, `date`, `location`, `cross_paper`, `reasons` | new (merged pairs, score 0.75 or more) |
| **`POSSIBLE_MATCH`** | Article → Article | same as above | new (borderline pairs, 0.50 to 0.75) |

The similarity links copy the scores and signal values already saved on each article; **no similarity is recomputed**. They are treated as undirected when queried.

### 5.3 Write rules (same safety as today)
- All writes are `MERGE`, so repeating a run creates no duplicates.
- For each article being written, its derived links (`PART_OF`, `MENTIONS`, `ON_PAGE`, `IN_SECTION`, `WRITTEN_BY`, `HAS_TOPIC`, `REPORTS`, `MAKES_CLAIM`, `HAS_KEYWORD`, `SIMILAR_TO`, `POSSIBLE_MATCH`) are removed first and recreated, so a re-cluster leaves no stale links.
- After writing, nodes of the new labels that have no article attached are removed (the same clean-up already done for empty stories).
- Only whitelisted labels and relationship names are ever placed into query text; all values are query parameters.
- New unique constraints for every new label, plus indexes on `Article.date` and `Entity.name`.

## 6. Work packages

| WP | Work | Main files | Tests | Estimate |
|---|---|---|---|---|
| **WP1** | Review and sign off this model and the decisions in section 4 | this document | | 0.5 d |
| **WP2** | Cleaning rules as pure functions: author validator (accepts "By <name>", "Staff Reporter", 2–4 capitalised words; rejects numbers, phone numbers, emails, fragments), keyword normalisation and stop-list (cap 6 per article), event key, claim selection (checkworthy only, trimmed, capped) | new `graph/clean.py` | unit tests with real examples from the data, including every junk byline listed in section 3 | 1.5 d |
| **WP3** | Extend `build_graph_rows` to produce the new nodes and links, including similarity links from `dedup_clustering` | `graph/neo4j_graph.py` | unit tests on fixture documents (no database) | 1.5 d |
| **WP4** | Extend `Neo4jGraph.write` and `ensure_schema`: new constraints and indexes, replace-and-recreate of derived links, orphan clean-up | `graph/neo4j_graph.py` | integration tests on a throw-away Neo4j container (the services suite already does this); a repeat-run test proving no duplicates | 1.5 d |
| **WP5** | Read API for a run (section 7) | `api/server.py`, `graph/neo4j_graph.py` | API tests with a fake graph; integration test with the container | 1.5 d |
| **WP6** | Frontend: add Cytoscape.js; **Graph** tab shell, drawing, type toggles with counts and colours, similarity slider, search, layouts | `frontend/src/components/graph/*`, `lib/graph.ts`, `package.json` | unit tests for the data transformation, filters and limits | 3 d |
| **WP7** | Details panel (node properties, neighbours, jump to the Similar articles / Stories tab), presets, accessible table view, empty / error / truncated states | same | component tests | 1.5 d |
| **WP8** | Rebuild tool for existing runs from stored enriched articles (no new OCR or enrichment) | `graph/rebuild.py` or a CLI command | test on stored fixtures | 0.5–1 d |
| **WP9** | End-to-end verification, performance test on the 1,300-article set, Neo4j memory decision, report | | see section 11 | 1.5 d |

## 7. API contract (proposed)

All read-only, parameterised, protected by the same checks as the story graph endpoint (run finished, enrichment done, `NP_GRAPH=neo4j`, otherwise 404/409).

1. `GET /api/runs/{run_id}/graph/summary` returns the counts per node type and per link type for the run (cheap; fills the legend and toggles).
2. `GET /api/runs/{run_id}/graph?types=Paper,Article,Story&min_score=0.5&max_nodes=300` returns
   ```json
   {
     "run_id": "...", "truncated": false,
     "totals": {"nodes_by_type": {"Article": 106}, "edges_by_type": {"SIMILAR_TO": 3}},
     "nodes": [{"id": "Article:<event_id>", "type": "Article", "label": "<headline>", "props": {}}],
     "edges": [{"id": "...", "source": "...", "target": "...", "type": "SIMILAR_TO", "props": {"score": 0.86}}]
   }
   ```
3. `GET /api/runs/{run_id}/graph/neighbors?node=<id>&types=...&limit=50` returns the neighbours of one node (the "expand" action).

How the run is scoped: the server reads the run's article IDs from its own stored `stories.json` and passes them as a query parameter; the shared graph needs no schema change. When there are more nodes than `max_nodes`, the highest-degree nodes are kept and `truncated` is set. Queries run with a time limit; a database error returns 502 as the story endpoint does today.

## 8. UI specification

A **Graph** tab after Stories / Similar articles, shown only when enrichment is done, the server has Neo4j on, and the run has at least one article.

- **Canvas:** nodes coloured and shaped by type with a legend; link width and opacity follow the similarity score; similar-to links are drawn solid and possible-match links dashed.
- **Controls:** a toggle per node type with its count; a minimum-similarity slider; search by headline, entity or keyword; layout choice; fit, zoom and reset; "show only this run" / "all runs" (D1).
- **Presets:** *Similarity* (Articles with SIMILAR_TO and POSSIBLE_MATCH only), *Structure* (Paper, Edition, Day, Page, Section, Article), *Content* (Article, Entity, Topic, Event, Keyword, Claim).
- **Default view:** Paper, Article, Story and the similarity links only. Entities, keywords, claims, authors and sections are off until the user switches them on (prevents an unreadable "hairball").
- **Details panel:** click a node to see its properties and neighbours; an Article node offers "Open in Similar articles" and "Open in Stories".
- **Table view:** the same nodes and links as a table, for accessibility, copy-paste and testing (the canvas itself cannot be read by screen readers).
- **States:** loading, empty ("no graph for this run"), truncated ("showing the 300 best-connected nodes of 2,140"), error, and "graph database is not enabled".
- **Honest labelling:** the Day node is labelled as the date entered at upload; the similarity score is shown as a ranking score, not a probability.

## 9. Limits and performance

- Default cap 300 nodes, adjustable up to 2,000. Above that the view must be narrowed with the type toggles or by search.
- Claims are never shown by default and are limited to checkworthy claims, trimmed to 300 characters.
- Keywords are capped at 6 per article and the stop-list removes generic phrases.
- Expected extra write time in the enrich stage: a few seconds per run (the graph stage takes about 1.4 s for 60 articles today); to be measured in WP9.
- Neo4j memory is re-checked on the 1,300-article set (D6).

## 10. Security and safety

- Only whitelisted node labels and relationship names reach query text; every value is a parameter.
- The type filter on the API is validated against the same whitelist; request size limits and a query timeout apply.
- No credentials or connection details are sent to the browser.
- The Neo4j ports stay bound to `127.0.0.1` as today.
- No change to MongoDB, PostgreSQL or the matching engine.

## 11. Verification plan (the feature is not done until these pass)

1. Unit tests for cleaning, row building and the front-end data transformation.
2. Integration tests on a throw-away Neo4j: write twice and check there are no duplicate nodes or links; change a story and check no stale link remains; check orphan clean-up.
3. **Real-data comparison:** upload two and then three newspapers. For each node type and link type, compare the count shown in the UI, the count from the API summary, and the count from a direct Cypher query. They must be equal.
4. Check each similarity link against the saved score on the article (for example the 3 known pairs at 86%, 81%, 80% and 77%).
5. Real-browser test (headless Chrome through Playwright): the tab appears, toggles change what is drawn, the slider hides low-score links, a node click opens its details, the table view matches the canvas, no console errors.
6. Performance: draw time and memory on the largest run; the 300-node default stays responsive.
7. Regression: the existing tests (170 Python, 65 frontend) still pass, and the existing story-level "Relationships" panel still works.

## 12. Rollout, migration and rollback

- New constraints are created automatically on the next graph write (`IF NOT EXISTS`).
- Existing runs: run the WP8 rebuild; until then they show only the old node types.
- Deployment: rebuild the api, enrich-worker and web images (the same procedure used so far).
- Rollback: the new labels and links can be dropped with one Cypher statement per label; `NP_GRAPH=none` switches the graph off completely. The old graph content is untouched because everything new is additive.

## 13. Risks and mitigations

| Risk | Effect | Mitigation |
|---|---|---|
| Too many nodes (hairball) | unreadable, slow | defaults and caps (section 9), presets, search, expand on demand |
| Author and Section data is poor | graph full of junk or empty | validator (WP2); include only validated values |
| Claim text is garbled | meaningless nodes | checkworthy only, trimmed, hidden by default |
| Event keys are unreliable (69% generic) | wrong merges of events | create Event nodes only for specific events; show the key fields in the details panel |
| Old runs lack similarity links | empty similarity view | rebuild tool (WP8) and an explanatory message |
| Larger writes slow enrichment | longer runs | measure in WP9; batch writes (already `UNWIND`) |
| Neo4j memory too small | slow or failing queries | measure and raise in WP9 |
| New front-end dependency | larger bundle, licence | Cytoscape.js is MIT; measure the bundle size |

## 14. Phasing and effort

Estimates are rough, for one developer, and will be refined after WP2.

**Phase A: MVP, about 7–8 working days.** Paper, Edition, Day, Page, Article, Story, Entity, Topic, Keyword, and the similarity links; the Graph tab with toggles, slider, search, details panel and table view; real-data verification.
- Covers WP1, a reduced WP2 (keywords only), WP3, WP4, WP5, WP6, a reduced WP7, and a reduced WP9.

**Phase B: the data-quality-heavy nodes, about 5–6 working days.** Author (with validator), Section, Event (shared), Claim, the rebuild tool for old runs, presets, memory tuning, final report.

**Total for all 13 node types: about 13–14 working days (roughly three calendar weeks).**

## 15. Definition of done

- [ ] All 13 node types exist in Neo4j with the keys in section 5 (Author and Section only when validated).
- [ ] The similarity links match the scores saved on the articles.
- [ ] Re-running a run creates no duplicates; changed stories leave no stale links.
- [ ] The Graph tab shows the run's graph with type toggles, similarity slider, search, details panel and table view.
- [ ] Counts in the UI equal the API summary equal the Cypher counts, on 2-paper and 3-paper uploads.
- [ ] No console errors; existing tests still pass; new tests added for every work package.
- [ ] The report lists files changed, tests run, results and limitations.

## 16. Out of scope

Changes to the matching engine or its weights, the MongoDB or PostgreSQL schemas, OCR quality work (plan steps 5–7), editing the graph from the UI, and graph analytics such as community detection or centrality.
