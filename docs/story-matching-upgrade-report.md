# Story-matching upgrade (steps 1–4): change and test report

Project: e-Paper-OCR. Base commit for this report: `a912ed2`. Date of the work: 2026-11-13.

## 1. Summary

**Goal.** When two newspapers report the same event in different words and tone, the pipeline should put them in one story. Two different events on a similar topic (for example two state elections) should stay apart.

**What was done.** Four changes to the story-matching code (`newspaper_pipeline/cluster/`), plus a UI panel that explains each match:

1. Switch the embedding model from MiniLM to BGE-M3.
2. Weight keywords by how rare they are across all articles (IDF).
3. Add a place signal.
4. Add veto rules (different places, different winners).

**Result on real data (162 HT + 162 TOI articles from the project's MongoDB).**

| Measure | Before | After |
|---|---|---|
| Stories found in both papers | 3 | 2 |
| Wrong merges among them | 0 | 0 |
| Borderline pairs sent to review (clusterer count) | 532 | 243 (-54%) |
| Time to match all 324 articles | 3 s | 11 s |

**Honest reading of the result.**
- Matching recall did not improve on this sample. One of three known real pairs (Cheema / gangster) moved from "merged" to the review queue (score 0.70, below the 0.75 merge line).
- The clearest gain is the smaller review queue (-54%) and the new protection against merging different events (proven by a unit test, not yet needed on this data).
- The sample is very small (3 known real pairs) and the stored HT articles were read by the old low-resolution OCR (glued words such as "PrimeMinisterNar"), which weakens the name and keyword signals. The real effect needs a re-run on freshly uploaded papers.

**Status.** Written, tested, **deployed (images rebuilt) and verified end to end** on 2026-11-13; see section 11 for the follow-up verification. Sections 1–10 describe the state at the end of the first round and are kept unchanged except where a later finding corrected them.

## 2. Scope

Included: plan steps 1–4 (BGE-M3, IDF keywords, place factor, veto rules) and the Stories-tab UI that shows why articles were grouped.

Not included (not part of steps 1–4): using the NER model for matching (step 5), handling headline-only articles (step 6), widening the 48-hour candidate window (step 7), tuning the weights on a labelled set (step 8). The merge thresholds (0.75 same story, 0.50 borderline) were **not changed**.

## 3. Changes, step by step

### Step 1 — BGE-M3 embeddings

| Item | Before | After |
|---|---|---|
| `NP_EMBEDDER` (`.env`, `.env.example`, `docker-compose.yml` default) | `minilm` | `bge-m3` |
| Vector size / reading length (measured in the container) | MiniLM: 384 dim, 128 tokens | BGE-M3: 1024 dim, 8192 tokens |
| Text embedded per article (`cluster/embed.py`, `embedding_text`) | headline + first 1500 chars | headline + first 3000 chars |
| pgvector table (`cli.py`, new `_index_table`) | always `article_embeddings` | MiniLM keeps `article_embeddings`; other models get `article_embeddings_<model>` (BGE-M3: `article_embeddings_baai_bge_m3`) |

Why a new table: the table refuses vectors of a different size. Keeping MiniLM's table untouched means data stored before the switch is not lost. `.env` is git-ignored; its `NP_EMBEDDER` line was changed locally.

### Step 2 — keywords weighted by rarity (IDF)

New file `cluster/keystats.py` (`KeywordStats`). `signals.keywords()` now takes the statistics.

- Base weight: headline word 2.0, body word 1.0 (unchanged).
- New: a name (capitalised word) counts 1.5x.
- New: multiplied by an IDF factor = `clamp(log((n+1)/(df+1)) / log(n+1), 0.35, 1.0)`, where `n` = articles seen and `df` = articles containing the word. With fewer than 30 articles the factor is 1.0 (too little data).
- Each article is counted once (by `event_id`), so re-running enrichment does not double-count.
- Counts are saved to the JSON file named by `NP_KEYWORD_DF` (set to `/data/keyword_df.json` for the enrich-worker in `docker-compose.yml`) after every clustering batch. Without that variable the counts live in memory only.

### Step 3 — place signal (`cluster/signals.py`)

- `locations()` scans the headline and text for Indian states, Indian cities and 15 foreign country names. Headline mention x3, first 600 characters x1.5, the rest x1, dateline city +2. A city also adds half its weight to its state. "Delhi" / "New Delhi" are treated as weak (the capital appears in most national stories).
- `location_match()` returns: same city 0.8, same state 0.5, same foreign country 0.2, only the capital in common 0.1, clearly different places 0.0, and "unknown" (neutral) when either article names no place or only incidental places.
- Final role in the score (after testing, section 5): place **only counts against** a pair (0.0 when both articles are clearly about different places). Agreeing on a place adds nothing, because different events often happen in the same city. The tier values above are still computed and shown in the UI.

### Step 4 — veto rules (`cluster/scorer.py`, `cluster/signals.py`)

- Place conflict: both articles are clearly about somewhere (named in the headline, or at least twice, excluding the capital) and share no place. Example: Jharkhand vs Maharashtra.
- Winner conflict: both headlines say a party won ("BJP wins …", "Congress alliance wins …", "Cong sweeps …"; 16 party names plus the abbreviation "Cong") and they name different winners, **unless** a headline names the other's winner anywhere (so "Cong-JMM sweep" does not conflict with "Congress alliance wins").
- Effect: the pair's score is capped at 0.40 (`VETO_CAP`), below the 0.50 borderline line, and the reason `veto: …` is recorded. Switch off with `NP_SCORER_VETOES=0` (default on).

### The final scoring formula (`RuleScorer`)

| Signal | Weight |
|---|---|
| Meaning (embedding cosine / `cos_ref`) | 0.55 |
| Names (shared proper nouns, shared-entity count, dateline city / wire) | 0.20 |
| Keywords (+ numbers, quotes, copied phrases) | 0.10 |
| Date (same day 0.7, 1 day 0.5, 2 days 0.2, 3+ 0) | 0.10 |
| Place | 0.05, used only as 0.0 when places clearly conflict; otherwise its weight is redistributed |

Unchanged: date-gap penalty (0.15 per day beyond 1), same-paper-same-day penalty (0.15), the semantic gate (cosine ratio below 0.3 caps the score at 0.3). `cos_ref`: BGE-M3 0.75, MiniLM 0.60 (existing values).

### Supporting changes

- `clusterer.py`: keeps a `KeywordStats`, passes it to the scorer, updates it as articles are added, saves it after each batch; each stored match now also keeps `components`, `veto`, `cosine`.
- `enrich/pipeline.py` (+1 line): the `dedup_clustering` block of each enriched article now also contains `matches` (previously only `review_candidates`). Any consumer of that JSON sees one new key.
- `output/export.py`: each story's coverage rows get `match` (strongest match, its score, signals, veto, the matched paper and headline); each story gets `possible_matches` (up to 6 borderline pairs).
- UI: new component `MatchReasons.tsx` inside `StoryDetail.tsx`: "Why these articles were grouped", with a match percentage per paper, chips for Meaning / Names / Place / Keywords / Date (weak ones highlighted, "Place not stated" when unknown), a "Held back by a rule" note, and a "Possibly the same story - please check" list. Shown only when the data has match information (older runs show nothing).
- `types.ts`: new types for the above.

### Size of the change (against `a912ed2`)

13 tracked files changed (+317 / -37 lines) and 3 new files (`keystats.py` 67 lines, `MatchReasons.tsx` 63, `MatchReasons.test.tsx` 39). This count includes a 10-line fix to two flaky frontend tests (section 4).

## 4. Automated test results

Run after the final change.

| Suite | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| Python (all suites that can run here) | 139 | 134 | 0 | 5 |
| Frontend (vitest, 5 files) | 41 | 41 | 0 | 0 |
| TypeScript type check | - | clean (exit code 0) | - | - |

Python per file: test_api 19, test_clean 22, test_cluster 22, test_device 7, test_input 15, test_models 5, test_ocr 24, test_two_phase 25. The 5 skipped tests were not investigated one by one.

**New tests (6 Python, 2 frontend).**
- `test_keyword_stats_idf_persist_and_no_double_count`: neutral below 30 articles; a common word is scaled down and a rare one keeps its weight; saving, reloading and double-counting.
- `test_locations_and_tiers`: same city 0.8, same state 0.5, unknown is neutral, Jharkhand vs Maharashtra gives a conflict, the capital alone never conflicts.
- `test_winner_extraction`.
- `test_veto_stops_two_elections_merging_even_when_wording_is_similar`: at cosine 0.9, the score is capped at 0.40 with vetoes on and higher with them off.
- `test_same_event_in_two_papers_is_not_vetoed_and_place_is_neutral_when_unknown`.
- `test_index_table_name_per_embedder`.
- Frontend `MatchReasons.test.tsx` (2 tests): the panel shows the match, signals, weak and unstated chips, the veto note and the borderline list; and shows nothing for a story without match data.

**Suites not run in the final state.**
- `tests/test_enrich.py` and `tests/test_pipeline.py` cannot be collected: `sample.json` is missing from the repository.
- `tests/test_services.py` needs a reachable PostgreSQL; it hung at its pgvector tests earlier in the session, so it was excluded and not re-run. It does not exercise the new code.

**Frontend flaky tests (found during this work).** Two RunPage tests that wait for short-lived states ("Queued on Kafka…") with a 5 ms poll failed intermittently. They also failed on the previously pushed commit (2 of 3 runs), so they were not caused by steps 1–4. Fix: those three tests use a 60 ms poll. After the fix the suite passed 8 of 8 repeated runs, then 3 of 3 again at the end.

## 5. Evaluation on real data

**Data.** From MongoDB `clean_events`, date label 2026-11-12: 162 Hindustan Times + 162 Times of India articles (articles with at least 120 characters of text). The date label is the run date, not necessarily the printed edition date. The HT text was read by the earlier low-resolution OCR. The database was only read, not changed.

**Baseline ("before")**: MiniLM, the old weights (meaning 0.6, names 0.2, date 0.1, keywords 0.1, no place, no veto), `cos_ref` 0.60.

**First attempt (not kept).** Weights 0.40 / 0.25 / 0.10 / 0.15 / 0.10 and a place score that was 0.0 whenever places did not match.
- Result: 320 stories, **0** in both papers, 46 borderline pairs. All three real pairs scored 0.60–0.71, below 0.75.
- Causes found: (a) the place score gave 0.0 to the Hasina pair although nothing conflicted (fixed: 0.0 only when both are clearly about different places, otherwise neutral); (b) lowering the meaning weight from 0.6 to 0.4 hurt pairs whose names and keywords were weak because of the garbled text.

**Tuning on 972 candidate pairs** (top 6 TOI matches by BGE-M3 cosine for each of the 162 HT articles):

| Weights (meaning / names / place / keywords / date) | Pairs at 0.75+ | Pairs 0.60–0.75 | Pairs vetoed |
|---|---|---|---|
| old (0.6 / 0.2 / - / 0.1 / 0.1) | 3 | 23 | 0 |
| v1 (0.40 / 0.25 / 0.10 / 0.15 / 0.10) | 0 | 9 | 14 |
| **v2 (0.55 / 0.20 / 0.05 / 0.10 / 0.10), chosen** | 3 | 16 | 14 |
| v3 (0.50 / 0.20 / 0.05 / 0.15 / 0.10) | 1 | 11 | 14 |

Cosine of the best cross-paper match per HT article: BGE-M3 median 0.53, 90th percentile 0.62, 99th 0.74, max 0.78; MiniLM median 0.44, 90th 0.58, 99th 0.67, max 0.78.

**The 14 vetoed pairs** (measured with v2, before the last change below) were all genuinely about different places, for example Ludhiana vs Maharashtra, Haryana vs Assam, China vs Israel, Punjab vs Gujarat. Their embedding cosines were 0.45–0.57, and without the veto their scores were 0.40–0.50 under v2 (0.43–0.54 under the old weights), far below the 0.75 merge line. So no wrong merge was prevented on this data, and the veto only moved at most one pair (exactly 0.50) out of the borderline band. **The drop in the review queue (532 → 243) therefore comes from the new embedder and weights, not from the vetoes.** The veto rule itself was not changed by the last tweak, but the count of 14 was not re-measured afterwards.

**One more change after a failing unit test.** With v1 and then v2 the existing test `test_borderline_goes_to_review_and_judge_can_resolve` (a related-but-different pair that must stay borderline) failed, because agreeing on a city added to its score: it sat at 0.750 against the 0.75 line (0.748 under the old weights). Fix: place now only counts against a pair. All cluster tests then passed.

**Final end-to-end result** (full clustering run, final code, 324 articles):

| | Before (MiniLM, old weights) | After (BGE-M3, final code) |
|---|---|---|
| Stories (all) | 314 | 315 |
| Stories in both papers | 3 | 2 |
| Borderline pairs for review | 532 | 243 |
| Run time | 3 s | 11 s (+ one-time model load) |

Stories in both papers:
- Before: (1) HT "fromabroadin6 months: Cheema" + TOI "Wanted Punjab gangster brought back from Ethiopia"; (2) HT "Modi:Dimagi" + TOI "Maoists gone, but 'dimagi Naxals'…"; (3) HT "Hasina…extradition" + TOI "Hasina a 'convicted criminal'…".
- After: (1) HT "Modi:Dimagi" + HT "NARENDRA MODI, PM" + the same TOI article; (2) the Hasina pair. The gangster pair is now in the review queue (about 0.70 in the tuning run, with v2 weights).
- All matches in both columns looked correct to me by eye. I did this check myself; there is no independent labelled set.

Note on counts: "borderline pairs" in the last table are counted by the clusterer for every article against its own candidates (both directions, within the same paper too), so they are larger than the 972-pair figures in the tuning table. The two are different measures and should not be compared with each other.

## 6. Efficiency assessment

| Aspect | Verdict |
|---|---|
| Finding more matching stories | **No gain on this sample** (3 real pairs before, 2 after; one moved to review). Too small and too noisy (old OCR) to conclude either way. |
| Human review load | **Better**: borderline pairs 532 → 243 (-54%). |
| Protection against merging different events | **Added and unit-tested** (two elections cannot merge even at cosine 0.9). Not needed on this data, so not proven in practice. |
| Explainability | **Better**: every match now shows its signals and any rule that applied. |
| Reading long articles | **Better in principle**: 8192-token reading (MiniLM 128) and 3000 characters of text (was 1500); no measured effect on this sample. |
| Speed | **Slower**: 3 s → 11 s for 324 articles, plus a one-time model load. |

## 7. Not verified, not deployed, open items (state at the end of the first round; resolved items are listed in section 11)

- **Not deployed.** The images were not rebuilt after these changes. The code was hot-copied into the running `newspaper-enrich-worker-1` container for testing; that copy is lost when the container is recreated. The running worker still has `NP_EMBEDDER=minilm` and no `NP_KEYWORD_DF`. A rebuild and restart (`docker compose up -d --build`) are required.
- **Not committed or pushed** to GitHub (16 changed or new files, plus this report).
- **Not exercised outside unit tests**: the new pgvector table in a real Postgres (the evaluation used the in-memory index); saving and reloading `keyword_df.json` inside the running worker (the evaluation built its statistics in memory from the same corpus); the `match` and `possible_matches` export fields have no dedicated Python test; the `MatchReasons` panel was not viewed in a browser.
- **Weights are hand-tuned** on one small sample, not fitted. Plan step 8 (a labelled set of about 200–300 pairs) is still open.
- **Container incident.** During the evaluation the worker container restarted on its own after Docker lost its internal network for a while; the evaluation was re-run. No data was affected.

## 8. Differences from the plan

| Plan | Delivered | Reason |
|---|---|---|
| Weights 0.35 / 0.25 / 0.15 / 0.15 / 0.10 | 0.55 / 0.20 / 0.05 / 0.10 / 0.10 | The first weights lost real matches (section 5). |
| Place tiers 1.0 / 0.7 / 0.3 | 0.8 / 0.5 / 0.2 and place only counts against a pair | Agreement on a city is weak evidence; it pushed a test pair over the merge line. |
| Winner veto | Implemented with a rule that a headline naming the other's winner is not a conflict | A first version falsely vetoed "Congress alliance" vs "Cong-JMM". |

## 9. Recommended next steps

1. Rebuild and restart the stack, then upload HT and TOI for the same date with the improved OCR and repeat the comparison.
2. Label 200–300 article pairs (same event / different) and fit the weights and thresholds on them.
3. Commit and push these changes to the `ocr_imp` repository.
4. Then consider plan steps 5–7 (NER-based entities, headline-only articles, a wider time window).

## 10. How to reproduce

- Python: `python -m pytest -q tests --ignore=tests/test_enrich.py --ignore=tests/test_pipeline.py --ignore=tests/test_services.py`
- Frontend: `cd frontend; npx tsc --noEmit; npx vitest run`
- Real-data comparison: it ran inside the enrich-worker container against the `clean_events` collection; the script is not part of the repository.

## 11. Follow-up verification (2026-11-13, after the first version of this report)

This section was written after the "Development Change Requirements Report" (changes 01–10) was received. It covers the items that could be done and verified straight away. Changes 02–06, 08 (feedback capture) and 09 are **not started**; they depend on the labelled dataset (change 01) and are listed at the end.

### 11.1 Deployment (change 10)

| Item | Result |
|---|---|
| Docker images rebuilt (api, extract-worker, datasync, enrich-worker, web) | Done. All services up. |
| Worker settings inside the running container | `NP_EMBEDDER=bge-m3`, `NP_KEYWORD_DF=/data/keyword_df.json`, `NP_SCORER_VETOES=1`, `NP_DEVICE=cuda` |
| BGE-M3 loaded in the worker | Yes: dimension 1024, reading length 8192 tokens |
| New web bundle contains the "Why these articles were grouped" panel | Yes |
| Worker restart and recovery | Worker restarted, resumed consuming `newspaper.jobs.enrich`, settings kept |

### 11.2 Database and persistence (change 07), on the real PostgreSQL of the stack, using a throw-away table

| Check | Result |
|---|---|
| New table layout | `vector(1024)` plus primary key, `day`, `story_id` and HNSW indexes |
| Insert 200 articles, then process the same 200 again | 200 rows both times: **0 duplicate rows**; keyword documents counted 200 both times: **0 double-counted** |
| Keyword statistics file saved and reloaded | Saved (115,840 bytes) and reloaded by a fresh process |
| Existing MiniLM table | Untouched: `vector(384)`, 967 rows |
| Stored row carries what a later run needs | Yes (date, paper, story id, text snapshot) |
| A copy of a stored article, in another paper, handled by a fresh process | Joined the stored article's story |
| `/data` writable by the worker (target of the keyword file) | Yes |

Not tested: behaviour on a very large corpus, and concurrent writers.

### 11.3 End-to-end run through the real application

A small upload through the API (Hindustan Times pages 2 and 6, Times of India pages 1 and 6), with enrichment:

| Measure | Result |
|---|---|
| Pages / articles / stories | 4 / 106 / 54 |
| Stories in both papers | **3** |
| Mean OCR confidence | 98.6% |
| Time | 71.6 s total: OCR 42.8 s, clustering 3.1 s, enrichment 20.4 s, graph 1.4 s (on the GPU) |
| Browser test (headless Chrome through Playwright) | All five tabs present (Articles, All OCR results, Page view, Stories, Needs review); the explanation panel rendered on all 3 cross-paper stories; **no browser errors**; screenshots inspected |

The three cross-paper stories were the same three events as in section 5, now matched at 80% (gangster brought from Ethiopia), 77% (Hasina extradition) and 86% (Modi at Bastar). With fresh OCR text the gangster pair, which was missed on the old stored text (0.70), is matched. This is consistent with the earlier explanation (garbled old OCR) but is still only 3 pairs.

### 11.4 False-negative analysis (change 01) of the 3 known pairs

On the old stored articles, using the new evaluation tool. The "same event" judgements are mine, by eye; they are not ground truth.

| Pair | Cosine | Rank of the partner among nearest articles | Score (final code) | Result |
|---|---|---|---|---|
| Cheema / gangster | 0.650 | 1 | 0.699 (names 0.51, keywords 0.15) | review queue |
| Modi / Dimagi Naxals | 0.784 | 1 | 0.786 | merged |
| Hasina extradition | 0.749 | 1 | 0.761 | merged |

All three partners were the nearest cross-paper article (candidate-retrieval recall@5/10/20 = 100% on this sample). The one miss happened in **scoring**, not retrieval, caused by weak name and keyword signals on garbled text. Observation: all-capitals headlines remain glued after OCR (for example "LINKEDTOSHIGH-PROFILEKILLINGS"), because the space-restoring step deliberately skips all-capital words to protect acronyms.

### 11.5 Service tests (`tests/test_services.py`)

- Earlier this suite appeared to hang. Cause: its fixtures connect through the name `localhost`, which on this machine resolves to both `127.0.0.1` and `::1`, and the IPv6 address wastes 2 seconds on every new connection; the suite opens hundreds. It was slow, not stuck, and it is unrelated to the matching changes.
- Fix (tests only): `tests/conftest.py` now uses `127.0.0.1` for the throw-away MongoDB, PostgreSQL and Neo4j.
- Result: **26 of 26 passed in 24.7 s**, including the full pipeline run into MongoDB, pgvector and Neo4j. 12 throw-away containers left behind by the earlier killed runs were removed.

### 11.6 Final test results (after all of the above)

| Suite | Tests | Failed | Skipped |
|---|---|---|---|
| Python | 170 | 0 | 5 |
| Frontend (vitest) | 41 | 0 | 0 (passed in 3 of 3 runs) |
| TypeScript type check | - | clean | - |

Python per file: test_api 19, test_clean 22, test_cluster 22, test_device 7, test_input 15, test_models 5, test_ocr 24, test_services 26, test_story_eval 5, test_two_phase 25. `tests/test_enrich.py` and `tests/test_pipeline.py` still cannot be collected because `sample.json` is missing from the repository.

### 11.7 New: evaluation tool (change 01)

`newspaper_pipeline/evaltools/story_eval.py`, run inside the enrich-worker:
- `export` writes candidate pairs to a CSV with the current score, decision and signals, and an empty `label` column (same / different / unsure). A first sheet with **1,472 pairs** (Hindustan Times vs Times of India, 2026-11-12, sorted by score; 3 at the merge level, 208 in the review band, 1,261 apart) is at `eval/pairs_to_label.csv` (git-ignored because it contains newspaper excerpts).
- `score` reads the labelled sheet and prints precision, recall, F1, false-positive rate, false-negative rate, candidate-retrieval recall at 5, 10 and 20, and the missed and wrongly merged pairs with their signals.
- Unit-tested (5 tests). It never labels anything itself.

Caution on the plan's target of 100 genuine matches: in these two editions only about 3 evident genuine pairs were found among the top candidates, so the target probably needs more newspapers and dates.

### 11.8 Still open (requirements 01–10)

| Change | Status |
|---|---|
| 01 Labelled dataset and metrics | Tool and first sheet ready; **labelling by a person is needed** |
| 02 Candidate retrieval (BM25, entity-assisted, union) | Not started. Current evidence (rank 1 for all 3 known pairs) shows no retrieval problem so far, but it is not measurable without labels. |
| 03 Scoring with lexical and event signals | Not started |
| 04 Veto categories (hard / soft / missing evidence), logged reasons | Not started; vetoes are logged in the match reasons today |
| 05 Event extraction | Not started |
| 06 OCR-quality-aware matching | Not started |
| 07 Database integration | Verified (11.2) |
| 08 Explanation UI | Verified in a browser (11.3); showing article date and identifier, applied threshold, and recording reviewer rejections are **not** implemented |
| 09 Performance | Stage timings exist in the run metrics; no optimisation done |
| 10 Deployment validation | Images rebuilt, services tests passing, UI browser-tested; the rest is committed to git (see git log) |
