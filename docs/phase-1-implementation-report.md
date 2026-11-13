# Phase 1 implementation report: cross-newspaper story matching (steps 1–4)

| | |
|---|---|
| Project | e-Paper OCR |
| Repository / branch | `PushpakRudraas/ocr_imp`, `main` |
| Phase | Story-matching upgrade, plan steps 1–4, plus the UI that explains matches |
| Report date | 2026-11-13 |
| Status | Implemented, deployed and verified. Committed in two commits: `d920305` (matching changes, explanation panel, evaluation tool) and a second commit (the "Similar articles" tab and this report). |

This report consolidates and supersedes the earlier `story-matching-upgrade-report.md` (which is kept as the detailed working history).

---

## 1. Executive summary

**Goal.** When several newspapers report the same event in different words and tone, the pipeline should put them in one story, and two different events on a similar topic should stay apart.

**What was delivered.**
1. BGE-M3 embeddings (replacing MiniLM) with a separate vector table per model.
2. Keywords weighted by rarity (IDF) with persisted statistics.
3. A place signal that only counts against a pair.
4. Veto rules for different places and different winning parties.
5. Two UI features: a "Why these articles were grouped" panel in the Stories tab, and a new "Similar articles" tab showing both complete articles, their event IDs, the similarity score and the shared context.
6. An evaluation tool (labelling sheet export, precision / recall / F1 scoring).
7. Test infrastructure fixes (services suite, flaky polling tests).

**Result in one table** (real data: 162 Hindustan Times + 162 Times of India articles stored in MongoDB):

| Measure | Before | After |
|---|---|---|
| Stories found in both papers | 3 | 2 |
| Wrong merges among them | 0 | 0 |
| Borderline pairs sent to review | 532 | 243 (-54%) |
| Matching time, 324 articles | 3 s | 11 s |

On a fresh upload with the improved OCR, all three known real pairs merged (80%, 77%, 86%).

**Verdict.** The phase works as designed and passes all checks, but the evidence for *better matching accuracy* is thin: only 3 real pairs exist in the test data, and the stored articles were read by the older, lower-quality OCR. The measurable gains are a smaller review queue (-54%), protection against merging different events (unit-tested), and full explainability of every match. Accuracy cannot be proven or tuned until a labelled set exists (plan step 8).

---

## 2. Scope

**In scope:** plan steps 1–4, the UI that shows match reasons, evaluation tooling, deployment and verification.

**Out of scope / not started:** step 5 (NER model for matching entities), step 6 (weak or headline-only articles), step 7 (wider time window), step 8 (tuning on labelled data). The merge thresholds (0.75 same story, 0.50 borderline), the 48-hour candidate window and the top-10 candidates per article were **not changed**.

---

## 3. How a pair of articles is scored (final design)

1. **Candidates.** For each article, the 10 nearest articles by embedding within a 48-hour window.
2. **Five signals**, each normalised to 0–1:

| Signal | Weight | Source |
|---|---|---|
| Meaning | 0.55 | Cosine of the two BGE-M3 embeddings divided by the model's reference cosine (0.75), capped at 1 |
| Names | 0.20 | Shared capitalised names: overlap, Jaccard, count of shared names (4+ is full), small bonus for the same dateline city or wire |
| Keywords | 0.10 | Overlap of rare content words (headline x2, names x1.5, IDF-weighted), plus shared numbers, quotes and copied phrases |
| Date | 0.10 | Same day 0.7, one day 0.5, two days 0.2, three or more 0 |
| Place | 0.05 | Counts only **against** a pair (0.0 when both are clearly about different places); otherwise its weight is left out of the blend |

3. **Corrections.** Date gap beyond one day: -0.15 per extra day. Same newspaper and same day: -0.15. Very low meaning (ratio below 0.3): score capped at 0.30.
4. **Veto rules.** Different places, or headlines naming different winning parties: score capped at **0.40**.
5. **Decision.** 0.75 or more: merged into one story. 0.50–0.75: "possible match", kept apart for review. Below 0.50: unrelated. An optional LLM judge for borderline pairs exists in the code and is off.

Worked example (the Hasina pair): (0.55 x 1.00 + 0.20 x 0.47 + 0.10 x 0.10 + 0.10 x 0.70) / 0.95 = 0.76, merged.

The score is a ranking score, not a calibrated probability, and the UI says so.

---

## 4. Implementation by step

### Step 1: BGE-M3 embeddings
| Item | Before | After | File |
|---|---|---|---|
| `NP_EMBEDDER` default | `minilm` | `bge-m3` | `.env`, `.env.example`, `docker-compose.yml` |
| Model size and reading length (measured) | 384 dim, 128 tokens | 1024 dim, 8192 tokens | - |
| Text embedded per article | headline + 1500 chars | headline + 3000 chars | `cluster/embed.py` |
| pgvector table | always `article_embeddings` | MiniLM keeps that name; others get `article_embeddings_<model>` (BGE-M3: `article_embeddings_baai_bge_m3`) | `cli.py` (`_index_table`) |

The table is per model because pgvector refuses vectors of another size. The old table was left untouched.

### Step 2: keywords weighted by rarity
New `cluster/keystats.py`. IDF factor = `clamp(log((n+1)/(df+1)) / log(n+1), 0.35, 1.0)`, neutral (1.0) below 30 articles. Each article is counted once by event ID (re-running enrichment does not double count). Counts persist to `NP_KEYWORD_DF` (`/data/keyword_df.json` for the enrich-worker) after every clustering batch. `signals.keywords()` applies the factor and a 1.5x boost for names.

### Step 3: place signal (`cluster/signals.py`)
`locations()` scans headline and text for Indian states, cities and 15 foreign country names (headline x3, first 600 characters x1.5, rest x1, dateline +2; a city also credits its state at half weight; Delhi / New Delhi count as weak because they appear in most national stories). `location_match()` returns same city 0.8, same state 0.5, same foreign country 0.2, only the capital 0.1, clearly different places 0.0, and unknown (neutral) otherwise. Only the 0.0 case enters the blend.

### Step 4: veto rules (`cluster/scorer.py`, `cluster/signals.py`)
- **Place conflict:** both articles clearly about somewhere (named in the headline, or at least twice, excluding the capital) and sharing no place.
- **Winner conflict:** both headlines say a party won ("BJP wins …", "Congress alliance wins …", "Cong sweeps …"; 16 party names plus the abbreviation "Cong") and the winners differ, unless a headline names the other's winner anywhere (so "Cong-JMM sweep" does not conflict with "Congress alliance wins").
- Effect: score capped at 0.40, reason `veto: …` recorded. Switch off with `NP_SCORER_VETOES=0` (default on).

---

## 5. Supporting changes

**Data now saved with each match** (`clusterer.py`, `enrich/pipeline.py`, `output/export.py`): the signal breakdown (`components`), the veto text, the cosine, and the list of matches in each article's `dedup_clustering.matches` (new key). Story views carry each article's strongest match and up to 6 borderline pairs (`possible_matches`). No database schema or collection was changed.

**UI feature 1: "Why these articles were grouped"** (`MatchReasons.tsx`, in the Stories tab): per-paper match percentage, signal chips (weak ones highlighted, "not stated" when a place is missing), a "held back by a rule" note, and a list of borderline pairs.

**UI feature 2: "Similar articles" tab** (`SimilarArticles.tsx`, `SimilarPair.tsx`, `lib/similar.ts`). Appears after enrichment for runs with two or more newspapers. Frontend only: it uses the existing stories and article endpoints, with no backend or MongoDB change.
- A card per group of similar articles, best score first, showing the score, newspapers, headlines and short event IDs.
- Opening a card loads the full records and shows each pair side by side: newspaper, page, full event ID with a Copy button, heading, the complete text (switchable between cleaned, original OCR and English translation), the similarity score, the signal chips and the engine's notes.
- A "Similar context" box worked out in the browser from the two stored articles: shared names (grouped as people, organisations, places), key phrases, figures, words, event type, place and the closest passages. This context is **not** part of the score.
- Borderline pairs are listed separately (top 10 with a "Show all" button). Search by headline, newspaper or event ID; filters by newspaper and minimum similarity.
- Runs processed before match details were saved show the grouped articles with a note that pair scores are unavailable.

**Evaluation tool** (`evaltools/story_eval.py`, 5 tests): `export` writes a labelling CSV of candidate pairs; `score` computes precision, recall, F1, false-positive rate, false-negative rate and candidate-retrieval recall at 5, 10 and 20. A first sheet of 1,472 pairs is at `eval/pairs_to_label.csv` (git-ignored because it contains newspaper excerpts).

**Test infrastructure:** `tests/conftest.py` now connects to `127.0.0.1` (on this machine `localhost` adds 2 seconds per connection through IPv6, which made the services suite look hung). Two flaky polling tests were made stable (60 ms poll).

---

## 6. Files

**Commit `d920305` (22 files, +1,068 / -40 lines):** `cluster/clusterer.py`, `embed.py`, `scorer.py`, `signals.py`, new `keystats.py`; `cli.py`; `enrich/pipeline.py`; `output/export.py`; new `evaltools/__init__.py` and `story_eval.py`; `tests/test_cluster.py`, `tests/conftest.py`, new `tests/test_story_eval.py`; `frontend/src/types.ts`, `StoryDetail.tsx`, new `MatchReasons.tsx` and its test, `RunPage.test.tsx`; `docker-compose.yml`, `.env.example`, `.gitignore`; `docs/story-matching-upgrade-report.md`.

**Second commit (11 files, about +1,011 / -2 lines; the 10 frontend files below plus this report):** new `SimilarArticles.tsx` (179 lines), `SimilarPair.tsx` (116), `lib/similar.ts` (155), `SimilarArticles.test.tsx` (164), `lib/similar.test.ts` (86); modified `RunPage.tsx` (tab), `RunPage.test.tsx` (+1 test), `types.ts`, `MatchReasons.tsx` (one component exported), `styles.css`.

---

## 7. Verification and test results

### 7.1 Automated tests (final run)

| Suite | Tests | Failed | Skipped |
|---|---|---|---|
| Python | 170 | 0 | 5 |
| Frontend (vitest, 7 files) | 60 | 0 | 0 (passed in 5 of 5 consecutive runs) |
| TypeScript type check | - | clean | - |

Python per file: test_api 19, test_clean 22, test_cluster 22, test_device 7, test_input 15, test_models 5, test_ocr 24, test_services 26, test_story_eval 5, test_two_phase 25. `test_enrich.py` and `test_pipeline.py` cannot be collected because `sample.json` is missing from the repository (a pre-existing gap).

New in this phase: 11 Python tests (6 matching, 5 evaluation tool) and 21 frontend tests (2 for the explanation panel, 18 for the Similar articles tab and its helpers, 1 for the tab's visibility rule). The 5 skipped Python tests are explained in section 10 (missing fonts and optional libraries).

### 7.2 Real-data evaluation (MongoDB, 162 HT + 162 TOI articles)

The stored HT text came from the earlier lower-quality OCR, which weakens names and keywords.

- Final clustering run: 314 stories before vs 315 after; stories in both papers 3 vs 2; borderline pairs 532 vs 243; time 3 s vs 11 s.
- Before: Cheema/gangster, Modi/"Dimagi Naxals" and Hasina merged. After: the Modi and Hasina pairs merged (the Modi group also pulled in a second HT article on the same speech); the gangster pair scored about 0.70 and went to the review queue because garbled HT text weakened names (0.51) and keywords (0.15).
- All three partners were the nearest cross-paper article (rank 1), so candidate retrieval was not the problem; the miss was in scoring. These "same event" judgements are mine, not ground truth.
- The 14 pairs held back by a veto (measured on 972 candidate pairs) were all genuinely about different places, but all were already below the merge line, so no wrong merge was prevented on this data. **The drop in the review queue comes from the new embedder and weights, not from the vetoes.**

### 7.3 Weight tuning (972 candidate pairs, BGE-M3)

| Weights (meaning / names / place / keywords / date) | Pairs at 0.75+ | Pairs 0.60–0.75 |
|---|---|---|
| old (0.6 / 0.2 / - / 0.1 / 0.1) | 3 | 23 |
| first attempt (0.40 / 0.25 / 0.10 / 0.15 / 0.10) | 0 | 9 |
| **chosen (0.55 / 0.20 / 0.05 / 0.10 / 0.10)** | 3 | 16 |
| alternative (0.50 / 0.20 / 0.05 / 0.15 / 0.10) | 1 | 11 |

### 7.4 Deployment and database checks

| Check | Result |
|---|---|
| Rebuilt images; worker settings | `NP_EMBEDDER=bge-m3`, `NP_KEYWORD_DF=/data/keyword_df.json`, `NP_SCORER_VETOES=1`, GPU on |
| Worker restart | resumed consuming, settings kept |
| New table (throw-away copy on the real PostgreSQL) | `vector(1024)` with primary key, `day`, `story_id` and HNSW indexes |
| Process 200 articles twice | 200 rows both times: 0 duplicate rows, 0 double-counted keywords |
| Keyword file | saved and reloaded by a fresh process |
| Old MiniLM table | untouched (384 dim, 967 rows) |
| A copy of a stored article in another paper, new process | joined the stored article's story |
| Live system now | keyword file holds 356 documents and 11,296 distinct words; the BGE-M3 table holds 356 rows |

### 7.5 End-to-end run and browser tests

An upload through the real application (HT pages 2 and 6, TOI pages 1 and 6): 4 pages, 106 articles, 54 stories, 3 in both papers, OCR confidence 98.6%, 71.6 s total on the GPU (OCR 42.8 s, clustering 3.1 s, enrichment 20.4 s, graph 1.4 s). This is a test run (`15e830a36fdc`, dated 2026-11-14) that now exists in the application and its databases.

Headless Chrome (Playwright) checks:
- "Why these articles were grouped" panel rendered on 3 of 3 cross-paper stories.
- "Similar articles" tab: for **6 of 6 pairs** the percentage, both headings, the event IDs and the full article text matched what the backend stores exactly; 51 borderline pairs listed; the other tabs still worked; **no browser errors**. Screenshots were inspected.

### 7.6 Services suite
26 of 26 passed in 24.7 s, including the full pipeline into MongoDB, pgvector and Neo4j. 12 leaked throw-away containers from earlier killed runs were removed.

---

## 8. Problems found along the way

| Problem | Cause | Resolution |
|---|---|---|
| First weights found 0 cross-paper stories | Place score gave 0.0 whenever places differed even when nothing conflicted; lower weight on meaning hurt pairs with garbled text | Place is neutral unless clearly conflicting; weights re-tuned (section 7.3) |
| An existing test (related-but-different pair must stay borderline) failed | Agreeing on a city added to the score and tipped the pair over 0.75 | Place now only counts against a pair |
| False winner veto: "Congress alliance wins" vs "Cong-JMM sweep" | Same alliance read as two winners | No conflict when a headline names the other's winner; "Cong" mapped to Congress |
| Services tests appeared hung | `localhost` resolves to IPv4 and IPv6 here; IPv6 costs 2 s per connection | Fixtures use `127.0.0.1` |
| Flaky polling tests | A 5 ms poll skipped short-lived states | 60 ms poll |
| A test failed with 11 instead of 12 items | Test IDs collided (`p1` and `p10` padded with zeros) | Test data fixed (not a product bug) |

---

## 9. Limitations and risks

- **Accuracy is unproven.** Only 3 real cross-paper pairs were available. The weights and thresholds are hand-tuned. There is no labelled set yet (step 8).
- **Veto rules** have unit tests but have not met a real "two different elections" case.
- **No event-level similarity.** The engine has five signals; it does not compare the underlying events (an event-extraction idea from the later requirements document).
- **Names depend on OCR quality.** Garbled text produces junk names (for example "Doni Bal-Prabh Dassuwal" tagged as an organisation) that appear in the shared context. All-capitals headlines stay glued after OCR (for example `LINKEDTOSHIGH-PROFILEKILLINGS`).
- **Speed:** matching is slower (3 s to 11 s per 324 articles, plus a one-time model load).
- **Shared context in the new tab is computed in the browser**, not stored; the score and signal chips are the engine's own.
- **Cross-run matching** depends on the pgvector table; runs processed before this phase have no stored match details, and the new tab then shows the articles without pair scores.
- **Test data in the system:** run `15e830a36fdc` and the data of later uploads are in the application and databases.
- **Open item that remains:** `test_enrich.py` and `test_pipeline.py` need `sample.json`, which is missing from the repository (a pre-existing gap).

---

## 10. Version control and deployment status

- **Committed:** `d920305` (matching changes) and a second commit (Similar articles tab and this report). Only `d920305` had reached `origin/main` when this report was written; the second commit must be pushed with the account that owns the repository.
- **Running:** api, extract-worker, datasync and enrich-worker run the phase-1 code; the web container serves the Similar articles tab.
- **Skipped Python tests (5), now explained:** three need a Devanagari font (Hindi PDF rendering) and one needs the optional `qdrant_client` library (the pipeline uses pgvector); one needs `rapidocr_onnxruntime`, which is installed in the Docker image but not in the host Python. None is a product failure.

---

## 11. Handover reference

**Settings**

| Setting | Default | Effect |
|---|---|---|
| `NP_EMBEDDER` | `bge-m3` | `minilm` or `hashing` also available |
| `NP_SCORER_VETOES` | `1` | `0` switches the veto rules off |
| `NP_KEYWORD_DF` | `/data/keyword_df.json` (enrich-worker) | empty keeps the keyword statistics in memory only |
| `NP_SAME_STORY_THRESHOLD` / `NP_BORDERLINE_THRESHOLD` | 0.75 / 0.50 | unchanged merge and review lines |

**Rollback.** Set `NP_EMBEDDER=minilm` (its original table is untouched) and `NP_SCORER_VETOES=0`, then restart the workers. The weights themselves are constants in `cluster/scorer.py`.

**Commands**
- Python tests: `python -m pytest -q tests --ignore=tests/test_enrich.py --ignore=tests/test_pipeline.py`
- Frontend: `cd frontend; npx tsc --noEmit; npx vitest run`
- Evaluation sheet and scoring (inside the enrich-worker): `python -m newspaper_pipeline.evaltools.story_eval export|score …` (see the module header).

---

## 12. Next phase

1. **Step 6** (weak or headline-only articles) together with the all-capitals headline fix: the best-supported next step, because garbled text is what weakened the one missed pair.
2. **Step 5** (NER model for entities), which works better once the text is cleaner.
3. **Step 7** (wider time window), lowest priority until multi-day uploads are used.
4. **Step 8** (labelled check): label about 100 pairs from `eval/pairs_to_label.csv` and run the scoring tool to measure the effect of steps 5–7.
