# Implementation report: relationship and similarity graph (Neo4j) with a Graph tab

| | |
|---|---|
| Project | e-Paper OCR |
| Date | 2026-11-13 |
| Plan implemented | `docs/neo4j-graph-implementation-plan.md` (decisions D1–D7 taken as recommended in the plan) |
| Status | Implemented, deployed to the running stack and verified. **Not yet committed.** |

---

## 1. Summary

When two or more newspapers are processed, the enrichment stage now builds a relationship graph in Neo4j around the articles and their similarity, and a new **Graph** tab shows it in the UI.

- **13 node types** are written: Paper, Edition, Day, Page, Section, Author, Article, Story, Entity, Topic, Event, Claim, Keyword.
- **Two similarity link types** carry the matching engine's saved scores: `SIMILAR_TO` (merged pairs, 75% or more) and `POSSIBLE_MATCH` (50–75%, kept apart for review). Nothing is recomputed.
- **Automatic:** the graph is written by the existing "Relationship graph" enrichment stage. In a fresh 3-newspaper test it took **0.7 seconds** for 63 articles.
- **UI:** an interactive graph (Cytoscape.js) with views, a toggle per node type with counts, a similarity slider, search, a details panel, "show more neighbours" and a table view.
- **Verified:** on two real runs, the stored articles, a direct Neo4j query and the API agree exactly on all 13 node types and both link types; 29 of 29 browser checks pass on each run; writing a run again changes nothing.

**Caveat in one sentence:** the graph is only as good as the text and extraction it is built from. Authors, events and claims inherit OCR damage, and the new Event and Keyword nodes rarely connect different newspapers yet (section 8).

---

## 2. What was built

### 2.1 Backend

| Part | What it does | File |
|---|---|---|
| Cleaning rules (work package 2) | Decides what is good enough to become a node: author validator (accepts "By Neeraj Mohan", "Staff Reporter", wire agencies, several authors in one byline, initials; rejects phone numbers, emails, numbers, brackets, sentence fragments), keyword clean-up (no generic words, no duplicates of entity names, at most 6 per article), shared Event key (type + actor + institution + date + place; generic "General.Report" gets no node), claims (check-worthy only, trimmed to 300 characters, at most 3, same sentence = one node) | new `newspaper_pipeline/graph/clean.py` (150 lines) |
| Graph rows and writer (WP3, WP4) | `build_graph_rows` produces every new node and link; `Neo4jGraph.write` merges them, **replaces** an article's derived links instead of piling up new ones, and removes nodes nothing points to any more; new unique constraints for all new labels, indexes on `Article.date` and `Entity.name` | `newspaper_pipeline/graph/neo4j_graph.py` (520 lines, was 169) |
| Read API (WP5) | `GET /api/runs/{id}/graph/summary`, `GET /api/runs/{id}/graph`, `GET /api/runs/{id}/graph/neighbors` | `newspaper_pipeline/api/server.py` |
| Size cap | `select_nodes`: when more nodes exist than the cap, small structural types are kept whole, articles next, and the remaining room is shared in turn between the large types | `neo4j_graph.py` |
| Rebuild tool (WP8) | Writes the graph of finished runs from the enriched articles stored in each run's folder; reads only, idempotent, supports `--run` and `--dry-run` | new `newspaper_pipeline/graph/rebuild.py` (98 lines) |

**API behaviour:** all node types and the scope are validated against a whitelist (an injection attempt returns 400); article ids come from the server's own stored `stories.json`; `min_score` must be 0–1 and `max_nodes` 20–2000; database errors return 502 without internal details; the existing 404 ("graph not enabled") and 409 ("run not finished") rules apply. The graph is **shared across runs** (decision D1): the default scope is the run's own articles, and `scope=all` shows the most recent articles of every run.

### 2.2 Graph model written to Neo4j

New nodes: `Edition`, `Day`, `Page`, `Section`, `Author`, `Topic`, `Event`, `Claim`, `Keyword` (existing: `Paper`, `Article`, `Story`, `Entity`).

New links: `OF_PAPER`, `IN_EDITION`, `ON_DAY`, `ON_PAGE`, `IN_SECTION`, `WRITTEN_BY`, `HAS_TOPIC {confidence}`, `REPORTS`, `MAKES_CLAIM`, `SUPPORTED_BY`, `HAS_KEYWORD`, `SIMILAR_TO {score, semantic, entity, keyword, date, location, cosine, veto, reasons, cross_paper}` and `POSSIBLE_MATCH` (same properties). Existing links (`PUBLISHED_IN`, `PART_OF`, `MENTIONS`, entity relations) are unchanged.

### 2.3 Frontend

| Part | What it does | File |
|---|---|---|
| Graph tab | Appears after enrichment when the server reports `graph: neo4j`; views Overview / Similarity / Structure / Content; a toggle per node type with its count (greyed when none); similarity slider; size limit; scope (this run / all runs); layout choice; search; fit to screen; **table view** of the same data for accessibility | new `components/graph/GraphView.tsx` (235 lines) |
| Drawing | Cytoscape.js canvas: one colour and shape per type, similar-to links solid and thicker with higher score, possible matches dashed, search highlight, selection | new `components/graph/GraphCanvas.tsx` (101 lines) |
| Details panel | Node properties, what it is connected to (with the similarity score on the link), "Show more neighbours", and shortcuts to the Similar articles and Stories tabs | `GraphView.tsx` |
| Logic | Node type table, presets, merge/append, search, neighbours, details formatting | new `lib/graph.ts` (107 lines) |
| Wiring | API client, types, tab, styles | `api.ts`, `types.ts`, `RunPage.tsx`, `styles.css` |
| Dependency | `cytoscape` 3.33.1 (MIT licence) and its type definitions | `package.json`, `package-lock.json` |

The built JavaScript file is now 731,593 bytes (Cytoscape's own minified file is 434,037 bytes of that; the previous bundle size was not measured).

---

## 3. Tests

| Suite | Result |
|---|---|
| Python (all runnable suites) | **185 tests, 0 failed, 5 skipped** (was 170) |
| Frontend (vitest, 9 files) | **83 tests, 0 failed**, passed in 3 of 3 final runs (was 65) |
| TypeScript type check | clean |

**New tests (15 Python, 18 frontend):**
- `tests/test_graph_clean.py` (8): every junk byline seen in the stored data is rejected; real bylines accepted; keyword, event, claim and section rules; the rebuild tool (finished runs only, batches); the node-selection cap.
- `tests/test_services.py` (4 new, against a real throw-away Neo4j): all new node counts equal the built rows; every similarity link has exactly the score saved on the article and the right `cross_paper` flag; writing twice changes nothing; a changed article loses its old links and orphan nodes are removed; summary, type filters, similarity threshold, size cap, the run's own articles only, neighbours, and a Cypher-injection attempt that deletes nothing.
- `tests/test_api.py` (3): the endpoints use only the run's own articles, validate every parameter (bad types, scores, caps, scopes, injection text), return 404 when the graph is off, 409 before enrichment and 502 without leaking internals.
- Frontend: `lib/graph.test.ts` (8) and `GraphView.test.tsx` (9: views, toggles, slider, cap notice, search, details and expanding neighbours, table view, error and empty states), plus 1 test in `RunPage.test.tsx` that the tab appears only when the graph is on.

**Defects the tests found and that were fixed:** expanding a node's neighbours dropped the new link to the existing node (a merge rule discarded links whose other end was not yet in the "extra" set).

---

## 4. Verification on the real stack

### 4.1 Three-way count check

For each run, three independent sources were compared: the counts expected from the stored enriched articles, a direct Neo4j query, and the API summary.

| | Run `15e830a36fdc` (HT + TOI, written by the rebuild tool) | Run `e83d491c3ff5` (HT + TOI + The Hindu, written **automatically** by the pipeline) |
|---|---|---|
| Articles / Papers / Stories | 60 / 2 / 54 | 63 / 3 / 53 |
| Entities | 397 | 403 |
| Editions / Days / Pages / Sections | 2 / 1 / 4 / 0 | 3 / 1 / 7 / 2 |
| Authors / Topics / Events | 7 / 11 / 17 | 10 / 11 / 19 |
| Claims / Keywords | 130 / 20 | 135 / 26 |
| `SIMILAR_TO` inside the run | 6 | 6 (90 including links to articles of other runs) |
| `POSSIBLE_MATCH` inside the run | 55 | 30 (179 including other runs) |
| Result | **all equal** | **all equal** |

Every similarity link in Neo4j carries exactly the score saved on the article (checked on both runs). The extra links of the second run go to earlier runs' identical articles, because matching works across runs; they appear with `scope=all`.

### 4.2 Real browser (headless Chrome through Playwright), 29 checks per run, both runs 29 of 29

The Graph tab is present; the canvas has a real size; for every node type the drawn count equals the API summary (with every type switched on); similar-to and possible-match links drawn equal the API; the status line states the right counts; the Similarity view shows only linked articles and every drawn article has a similarity link; raising the slider above the best score removes the links; clicking a node opens its details and its connections; search highlights; the table view lists the same nodes and links as the canvas; no browser errors. Screenshots were inspected.

### 4.3 Behaviour of the data and the system

| Check | Result |
|---|---|
| Write the same run again (live database) | every node and link count identical before and after |
| Rebuild all 10 finished runs | 4.2 seconds in total |
| Whole graph after the rebuild | 6,956 nodes, 20,026 links (the plan estimated 11,000–12,000 nodes; cleaning and caps made it smaller) |
| API, this run, default view | 0.06 s, 99 KB |
| API, this run, every node type | 0.26 s, 445 KB |
| API, **worst case**: all runs, every type, 2000 nodes | 1.2 s, 4.6 MB, 11,016 links |
| Neighbours of an entity | 0.13 s |
| Neo4j container memory at that size | 1.11 GiB (heap limit 512 MB, page cache 128 MB), healthy throughout, no errors |

**Neo4j memory (decision D6):** no change was needed at this size, so the settings were left as they are. They should be re-checked if the graph grows to tens of thousands of nodes.

---

## 5. A design flaw found by the worst-case test, and its fix

The first version of the size cap kept node types in a fixed priority order. With all runs and every node type switched on, Stories (1,013) used all the free room, so the small structural types (Edition, Day, Topic, Section) and every content type (Entity, Keyword, Claim) never appeared. This contradicted the plan. Fix: `select_nodes` keeps small structural types whole, then articles, then shares the remaining room between the other types in turn. Re-test on the live system: all 13 types are represented (5 papers, 5 editions, 4 days, 11 topics, 2 sections, 109 pages, 94 authors, 1,386 articles, and 76 to 77 of each of Story, Event, Entity, Keyword and Claim), and every drawn link ends at a drawn node. A unit test now reproduces that exact situation.

---

## 6. Differences from the plan

| Plan | Delivered | Why |
|---|---|---|
| Phase A (first version) then phase B | Both phases in one pass: all 13 node types | The data-quality rules were small enough to build together |
| Cap: "structural types first, then large types by degree" | `select_nodes` (section 5) | The first rule starved types in the worst case |
| Neo4j memory "probably raise" | Left unchanged | Measured: fine at the current size |
| API `types` default | Article, Paper, Story | As planned |
| "Open in Similar articles" opens the right article | Opens the tab only | The Similar articles tab has no deep link yet |

---

## 7. How to use it

- Open a finished run with two or more newspapers and click **Graph**. Start with **Overview**; switch to **Similarity** to see the linked articles; use **Structure** and **Content** for the other node types.
- To see links between runs, set **Scope** to "All runs".
- Existing runs: `docker compose exec api python -m newspaper_pipeline.graph.rebuild --workdir /data/runs` (all) or `--run <id>` (one), `--dry-run` to count only. Running it twice changes nothing.
- Switch the graph off entirely with `NP_GRAPH=none`; the new labels can be removed with one Cypher statement per label.

---

## 8. Limitations (be aware of these)

1. **Authors inherit OCR damage.** 94 author nodes exist (76 people, 18 desks). Glued text produces names such as "Neeraj Mohanand Bhavey Nagpal" and "Gurwinder Singhalias Afridi". The validator cannot safely split glued words ("Anand" contains "and").
2. **Events are mostly single-paper.** Of 243 Event nodes, **0 are shared by two or more newspapers** yet. Some actors are garbled ("Cheema HTCorrespondent", "theAam Aadmi Party"). The Similarity view, not the Event nodes, is what connects the same event across papers today.
3. **Keywords rarely cross papers.** 205 keyword nodes; 148 are used by 2 or more articles, but only 3 by 2 or more newspapers.
4. **Entities do bridge papers:** 220 of 2,697 are mentioned by 2 or more newspapers.
5. **Claims are long garbled sentences** and only 2 are supported by a second paper. They are hidden by default.
6. **Sections are rare** (2 in the last run; scanned pages give none). The Section toggle is greyed when a run has none.
7. **Day is the date entered at upload,** not read from the PDF. The legend says so.
8. **Counts differ slightly from the Similar articles tab:** the graph holds all possible-match links (55 in the first run), while the Similar articles tab lists the top 6 per story (51 pairs).
9. **The 2000-node view is heavy** (4.6 MB, 11,000 links). The default is 300 nodes; the limit can be raised in the UI at the user's choice.
10. **Cross-run links** point to articles of other runs, so they are visible only with `scope=all`.
11. **Test data remains in the system:** runs `15e830a36fdc` and `e83d491c3ff5` (and their graph nodes) were created for these tests.

---

## 9. Files

**New (10 files):** `newspaper_pipeline/graph/clean.py`, `newspaper_pipeline/graph/rebuild.py`, `tests/test_graph_clean.py`, `frontend/src/components/graph/GraphCanvas.tsx`, `GraphView.tsx`, `GraphView.test.tsx`, `frontend/src/lib/graph.ts`, `frontend/src/lib/graph.test.ts`, `docs/neo4j-graph-implementation-plan.md`, this report.

**Modified (11 files, about +720 / -28 lines):** `newspaper_pipeline/graph/neo4j_graph.py`, `newspaper_pipeline/api/server.py`, `tests/test_api.py`, `tests/test_services.py`, `frontend/src/api.ts`, `types.ts`, `components/RunPage.tsx`, `components/RunPage.test.tsx`, `styles.css`, `package.json`, `package-lock.json`.

**Not changed:** the matching engine, MongoDB, PostgreSQL, and the existing story-level "Relationships" panel and endpoint.

## 10. Remaining work and next steps

- **Commit and push** (not done).
- Link "Open in Similar articles" to the specific article.
- Improve the data feeding the graph: glued bylines and garbled actors are OCR and extraction problems and belong to the next phase (steps 5–7: NER-based entities, weak and headline-only articles, the all-capitals headline fix).
- Re-check the Neo4j memory settings when the graph grows past tens of thousands of nodes.
