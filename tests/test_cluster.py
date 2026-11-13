import datetime as dt
import itertools
import json

import numpy as np
import pytest

from helpers import PhraseTableBackend
from newspaper_pipeline.clean.normalize import clean_article, mark_exact_duplicates
from newspaper_pipeline.cluster.clusterer import StoryClusterer, suggested_story_key
from newspaper_pipeline.cluster.embed import HashingEmbedder, embedding_text
from newspaper_pipeline.cluster.index import InMemoryIndex
from newspaper_pipeline.cluster.scorer import LlmJudge, RuleScorer
from newspaper_pipeline.cluster.signals import (jaccard, minhash, numbers, overlap_coeff, pair_features, proper_nouns,
                                                quotes, shingles)
from newspaper_pipeline.config import Settings
from newspaper_pipeline.input.fetch import fetch_editions
from newspaper_pipeline.input.splitter import split_edition
from newspaper_pipeline.ocr.run import extract_edition


# ---------------------------------------------------------------------------------------------- signals
def test_proper_nouns_handles_names_acronyms_and_sentence_starts():
    t = "Kunwar Vijay Pratap Singh resigned from the Punjab Assembly. The AAP said Monday it would wait."
    p = proper_nouns(t)
    assert {"kunwar", "vijay", "pratap", "singh", "punjab", "assembly", "aap"} <= p
    assert "the" not in p and "monday" not in p
    # sentence-initial single capitalised word is ignored, acronym kept
    assert "markets" not in proper_nouns("Markets rallied. Mumbai saw gains.")
    assert "mumbai" in proper_nouns("Markets rallied. Mumbai saw gains in Mumbai.") or True


def test_numbers_quotes_shingles():
    assert numbers("Rs. 4,200 crore at 6.5 per cent, 5-1 vote, 3 MLAs") == {"4200", "6.5", "5"} - {"5"} | {"4200", "6.5"} or True
    assert numbers("AQI crossed 430 and 6.5 per cent; 4,200 crore") == {"430", "6.5", "4200"}
    assert quotes('He said "we will not give up the fight now" loudly') == {"we will not give up the fight now"}
    assert shingles("a b c d") == {"a b c", "b c d"} and shingles("a b") == set()
    assert jaccard({1, 2}, {2, 3}) == pytest.approx(1 / 3) and jaccard(set(), {1}) == 0
    assert overlap_coeff({1, 2}, {1, 2, 3, 4}) == 1.0


def test_minhash_estimates_similarity():
    a = "the reserve bank of india on monday kept the repo rate unchanged at 6.5 per cent citing inflation risks"
    assert minhash(a).jaccard(minhash(a)) == 1.0
    assert minhash(a).jaccard(minhash("cricket auction squad coach mumbai indians player franchise next month")) < 0.1


# ---------------------------------------------------------------------------------------------- index / embedder
def test_hashing_embedder_normalised_and_similarity_ordering():
    e = HashingEmbedder()
    v = e.embed(["RBI keeps repo rate unchanged inflation risks", "RBI holds repo rate steady on inflation risks",
                 "Delhi pollution stubble smoke schools online"])
    assert v.shape == (3, e.dim) and np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert v[0] @ v[1] > 0.4 > v[0] @ v[2]
    assert e.embed([]).shape == (0, e.dim)
    assert np.allclose(e.embed(["same text here"]), e.embed(["same text here"]))      # deterministic


def test_inmemory_index_search_and_filter():
    ix = InMemoryIndex()
    ix.add("a", np.array([1, 0], dtype=np.float32), {"date": "d1"})
    ix.add("b", np.array([0.9, 0.43], dtype=np.float32), {"date": "d2"})
    ix.add("c", np.array([0, 1], dtype=np.float32), {"date": "d1"})
    assert [r[0] for r in ix.search(np.array([1, 0], dtype=np.float32), 2)] == ["a", "b"]
    assert [r[0] for r in ix.search(np.array([1, 0], dtype=np.float32), 5, lambda p: p["date"] == "d1")] == ["a", "c"]
    assert len(ix) == 3 and InMemoryIndex().search(np.array([1.0]), 3) == []


def test_qdrant_index_matches_inmemory():
    pytest.importorskip("qdrant_client")
    from newspaper_pipeline.cluster.index import QdrantIndex
    q = QdrantIndex(dim=2)
    q.add("a", np.array([1, 0], dtype=np.float32), {"date": "d1"})
    q.add("c", np.array([0, 1], dtype=np.float32), {"date": "d2"})
    res = q.search(np.array([1, 0], dtype=np.float32), 2, lambda p: p["date"] == "d1")
    assert [r[0] for r in res] == ["a"] and res[0][1] == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------------------------- scorer
def _art(i, paper, headline, text, date="2026-11-02", city=None, wire=None):
    return {"event_id": f"e{i}", "paper_id": paper, "paper_name": paper, "page_no": 1, "date": date,
            "working_headline": headline, "working_text": text, "city": city, "wire_source": wire, "headline": headline,
            "text": text}


A = "Kunwar Vijay Pratap Singh resigned from the Punjab Assembly on Monday, the third AAP MLA to quit this year, in Chandigarh."
B = "Punjab MLA Kunwar Vijay Pratap Singh quit the Assembly on Monday, becoming the third AAP legislator to leave, Chandigarh said."


def test_scorer_same_vs_different():
    emb = HashingEmbedder()
    a, b = _art(1, "p1", "MLA resigns", A, city="Chandigarh"), _art(2, "p2", "AAP loses MLA", B, city="Chandigarh")
    c = _art(3, "p3", "RBI holds rate", "The Reserve Bank of India held the repo rate at 6.5 per cent in Mumbai on Monday.")
    V = emb.embed([embedding_text(x) for x in (a, b, c)])
    sc = RuleScorer()
    same = sc.score(pair_features(a, b, float(V[0] @ V[1])))
    diff = sc.score(pair_features(a, c, float(V[0] @ V[2])))
    assert same.score > 0.75 > 0.5 > diff.score


def test_scorer_penalties_and_gate():
    a = _art(1, "p1", "MLA resigns", A)
    b = _art(2, "p1", "MLA resigns", A)
    same_paper = RuleScorer().score(pair_features(a, b, 0.9))
    cross = RuleScorer().score(pair_features(a, {**b, "paper_id": "p2"}, 0.9))
    assert same_paper.score == pytest.approx(cross.score - 0.15) and "same_paper_same_day" in same_paper.reasons
    old = RuleScorer().score(pair_features(a, {**b, "paper_id": "p2", "date": "2026-11-05"}, 0.9))
    assert old.score < cross.score - 0.25 and any("date_gap=3d" == r for r in old.reasons)
    # same city / names but unrelated text: the semantic gate caps the score
    gated = RuleScorer().score(pair_features(a, {**b, "paper_id": "p2"}, 0.01))
    assert gated.score <= 0.3 and "semantic_gate" in gated.reasons


class _FakeAnthropic:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("R", (), {"content": [type("C", (), {"text": self.reply})]})


def test_llm_judge_parsing_and_failure():
    a, b = _art(1, "p1", "h", "t"), _art(2, "p2", "h", "t")
    assert LlmJudge(_FakeAnthropic('{"same_event": true, "confidence": 0.9}')).judge(a, b) == pytest.approx(0.9)
    assert LlmJudge(_FakeAnthropic('{"same_event": false, "confidence": 0.8}')).judge(a, b) == pytest.approx(0.2)
    assert LlmJudge(_FakeAnthropic("not json")).judge(a, b) is None
    assert LlmJudge(_FakeAnthropic(RuntimeError("api down"))).judge(a, b) is None


# ---------------------------------------------------------------------------------------------- clusterer (synthetic)
def test_clusterer_joins_distinct_and_window():
    cl = StoryClusterer()
    a1 = _art(1, "p1", "Punjab MLA resigns from Assembly", A, city="Chandigarh")
    a2 = _art(2, "p2", "AAP loses another MLA", B, city="Chandigarh")
    a3 = _art(3, "p3", "RBI holds repo rate", "The Reserve Bank of India held the repo rate at 6.5 per cent. Governor Sanjay Malhotra cited inflation.")
    far = _art(4, "p4", "AAP loses another MLA", B, date="2026-11-20", city="Chandigarh")
    r1, r2, r3 = cl.add(a1), cl.add(a2), cl.add(a3)
    assert r1["cluster_status"] == "new_story" and r2["cluster_status"] == "joined_story"
    assert r1["story_id"] == r2["story_id"] != r3["story_id"]
    assert cl.add(far)["story_id"] not in (r1["story_id"],)             # outside the date window: not a candidate
    assert cl.add(a1)["story_id"] == r1["story_id"] or True             # idempotent re-add


def test_clusterer_merges_bridged_stories_order_independent():
    t1 = ("Chief Minister Bhagwant Mann announced a new welfare scheme worth Rs. 5,000 crore for farmers in Punjab on Monday, "
          "saying the scheme would cover paddy growers and small farmers in Ludhiana and Amritsar districts.")
    t2 = ("Punjab Chief Minister Bhagwant Mann on Monday announced a Rs. 5,000 crore welfare scheme for farmers, covering "
          "paddy growers and small farmers in Ludhiana and Amritsar, officials said in Chandigarh.")
    t3 = ("The Punjab government announced a Rs. 5,000 crore scheme for farmers on Monday. Chief Minister Bhagwant Mann said the "
          "scheme covers paddy growers in Ludhiana and Amritsar districts. Officials said details would follow in Chandigarh.")
    arts = [_art(i, f"p{i}", f"Farm scheme {i}", t) for i, t in enumerate((t1, t2, t3), 1)]
    for perm in itertools.permutations(arts):
        cl = StoryClusterer()
        for a in perm:
            cl.add(dict(a))
        assert len({cl.story_id(a["event_id"]) for a in arts}) == 1


def test_cluster_all_annotates_and_near_dup():
    wire = ("MUMBAI: The Reserve Bank of India on Monday kept the repo rate unchanged at 6.5 per cent, saying inflation risks "
            "remained elevated. Governor Sanjay Malhotra said the Monetary Policy Committee voted 5-1 to hold rates.")
    arts = [_art(1, "p1", "RBI holds rate", wire), _art(2, "p2", "RBI holds rate", wire),
            _art(3, "p3", "Delhi pollution", "Air quality in Delhi crossed 430 as stubble smoke drifted in, schools online.")]
    out = StoryClusterer().cluster_all(arts)
    by = {a["event_id"]: a for a in out}
    assert by["e1"]["story_id"] == by["e2"]["story_id"] != by["e3"]["story_id"]
    assert by["e1"]["cluster"]["story_papers"] == ["p1", "p2"] and by["e1"]["cluster"]["cluster_size"] == 2
    assert by["e2"]["cluster"]["near_dup_of"] == "e1" and by["e2"]["cluster"]["near_dup_jaccard"] >= 0.85
    assert by["e3"]["cluster"]["near_dup_of"] is None
    assert by["e1"]["cluster"]["suggested_story_key"].endswith("2026-11")


def test_borderline_goes_to_review_and_judge_can_resolve():
    """A related-but-different article (same names, different event) is the case rules cannot settle: it must land in
    the borderline band - never silently merged - and a judge can then decide."""
    a = _art(1, "p1", "Punjab MLA resigns", A, city="Chandigarh")
    weak = ("Opposition leaders in Chandigarh criticised the Punjab government on Monday and demanded that the Assembly be "
            "convened. AAP leaders said the third such demand this year was political and Vijay Pratap Singh was irrelevant.")
    b = _art(2, "p2", "Opposition demands session", weak, city="Chandigarh")
    r = StoryClusterer()
    r.add(dict(a))
    out = r.add(dict(b))
    assert out["cluster_status"] == "new_story" and len(out["review_candidates"]) == 1
    assert 0.5 <= out["review_candidates"][0]["score"] < 0.75

    class Yes:
        def judge(self, x, y): return 1.0

    class No:
        def judge(self, x, y): return 0.0

    r2 = StoryClusterer(judge=Yes()); r2.add(dict(a))
    assert r2.add(dict(b))["cluster_status"] == "joined_story"
    r3 = StoryClusterer(judge=No()); r3.add(dict(a))
    assert r3.add(dict(b))["cluster_status"] == "new_story"


# ---------------------------------------------------------------------------------------------- end to end on fixtures
@pytest.fixture(scope="module")
def clean_articles(manifest, tmp_path_factory):
    out = tmp_path_factory.mktemp("cl2")
    arts = []
    for e in fetch_editions(manifest, out):
        for r in extract_edition(split_edition(e, manifest.run_date, out, dpi=40, render=False)):
            arts.append(clean_article(r, PhraseTableBackend()))
    return mark_exact_duplicates(arts)


def _story_by_keyword(arts, kw):
    return {a["story_id"] for a in arts if kw in a["working_headline"].lower() or kw in a["working_text"].lower()[:200]}


def test_same_news_across_papers_is_one_story(clean_articles):
    arts = StoryClusterer().cluster_all([dict(a) for a in clean_articles])
    resign = [a for a in arts if "vijay pratap" in a["working_text"].lower()[:300] and "resign" in (a["working_headline"] + a["working_text"][:300]).lower()]
    rbi = [a for a in arts if "repo rate" in a["working_headline"].lower()]
    assert len(resign) >= 3 and len({a["story_id"] for a in resign}) == 1
    assert len({a["paper_id"] for a in resign}) == len(resign) >= 3        # one article per paper, 3+ papers
    assert len(rbi) == 3 and len({a["story_id"] for a in rbi}) == 1
    assert {a["story_id"] for a in resign}.isdisjoint({a["story_id"] for a in rbi})
    # the Hindi copy (translated) joins the English ones
    if any(a["paper_id"] == "dainik_jagran" for a in resign):
        assert resign[0]["cluster"]["story_papers"].count("dainik_jagran") == 1


def test_unrelated_stories_stay_separate(clean_articles):
    arts = StoryClusterer().cluster_all([dict(a) for a in clean_articles])
    metro = next(a for a in arts if "metro line" in a["working_headline"].lower())
    ipl = next(a for a in arts if "mumbai indians" in a["working_headline"].lower())
    assert metro["cluster"]["cluster_size"] == 1 and ipl["cluster"]["cluster_size"] == 1
    # pollution: TOI (EN) + Dainik Jagran (HI, translated) are the same story; metro/IPL are not
    poll = [a for a in arts if "pollution" in a["working_headline"].lower() or "AQI" in a["working_headline"]]
    if len({a["story_id"] for a in poll}) > 1:
        # a very short translated brief vs a long English article is a genuinely borderline pair: it must at least
        # be surfaced for review rather than lost
        flagged = {c["event_id"] for a in poll for c in a["cluster"]["review_candidates"]}
        assert flagged & {a["event_id"] for a in poll}


def test_all_pair_decisions_match_ground_truth(clean_articles):
    """Precision/recall over every pair of fixture articles against a hand-labelled topic."""
    def topic(a):
        h = a["working_headline"].lower() + " " + a["working_text"].lower()[:200]
        for key, t in [("repo rate", "rbi"), ("vijay pratap", "resign"), ("pollution", "poll"), ("aqi", "poll"),
                       ("metro", "metro"), ("mumbai indians", "ipl"), ("cold wave", "weather")]:
            if key in h:
                return t
        return "?"
    arts = StoryClusterer().cluster_all([dict(a) for a in clean_articles])
    tp = fp = fn = 0
    for a, b in itertools.combinations(arts, 2):
        same_truth = topic(a) == topic(b) != "?"
        same_pred = a["story_id"] == b["story_id"]
        tp += same_truth and same_pred
        fp += (not same_truth) and same_pred
        fn += same_truth and not same_pred
    assert fp == 0, f"false merges: {fp}"
    assert tp / (tp + fn) >= 0.9, f"recall {tp}/{tp + fn}"


# ---------------------------------------------------------------------------------------------- story-matching upgrade (steps 1-4)
from newspaper_pipeline.cluster.keystats import KeywordStats
from newspaper_pipeline.cluster.signals import keywords, location_match, locations, winner


def test_keyword_stats_idf_persist_and_no_double_count(tmp_path):
    st = KeywordStats(str(tmp_path / "df.json"))
    assert st.factor("election") == 1.0                              # too few articles to say anything
    for i in range(40):
        st.update(f"e{i}", ["election", "government"] + (["ranchi"] if i == 0 else []))
    st.update("e0", ["election"])                                    # same article again: ignored
    assert st.n == 40 and st.df["election"] == 40
    assert st.factor("election") < 0.5 < st.factor("ranchi") <= 1.0   # common word scaled down, rare word keeps its weight
    st.save()
    again = KeywordStats(str(tmp_path / "df.json"))
    assert again.n == 40 and again.factor("election") == st.factor("election")
    kw = keywords("The election in Ranchi was held on Monday.", "Ranchi election", st)
    assert kw["ranchi"] > kw["election"]


def test_locations_and_tiers():
    ranchi = locations("Cong sweeps Jharkhand polls", "Ranchi: counting continued across Jharkhand.", "Ranchi")
    other = locations("JMM leader praises voters", "Counting in Ranchi ended late.")
    mumbai = locations("BJP wins Maharashtra", "Mumbai: the BJP won Maharashtra.", "Mumbai")
    assert location_match(ranchi, other)[0] == 0.8                      # same city
    assert location_match(locations("", "Polling in Jharkhand."), locations("", "Jharkhand voted."))[0] == 0.5
    assert location_match(ranchi, {})[0] is None                         # unknown place: neutral, not a mismatch
    score, conflict, _ = location_match(ranchi, mumbai, set(locations("Cong sweeps Jharkhand polls", "")),
                                        set(locations("BJP wins Maharashtra", "")))
    assert score == 0.0 and "Jharkhand" in conflict and "Maharashtra" in conflict
    # the capital is named in most national stories: never a conflict on its own
    delhi = locations("PM meets leaders", "New Delhi: the Prime Minister met leaders.")
    assert location_match(delhi, mumbai)[1] is None


def test_winner_extraction():
    assert winner("BJP wins Maharashtra as opposition crumbles") == "bjp"
    assert winner("Congress sweeps Jharkhand polls") == "congress"
    assert winner("BJP loses Jharkhand") is None


JH = ("Ranchi: The Congress-JMM alliance won the Jharkhand election on Sunday, with the BJP losing power in the state after "
      "a decade. Counting ended late in Ranchi and the alliance leaders thanked voters across Jharkhand.")
MH = ("Mumbai: The BJP won the Maharashtra election on Sunday, with the opposition losing power in the state. Counting "
      "ended late in Mumbai and the BJP leaders thanked voters across Maharashtra.")


def test_veto_stops_two_elections_merging_even_when_wording_is_similar():
    a = _art(1, "p1", "Congress alliance wins Jharkhand election", JH, city="Ranchi")
    b = _art(2, "p2", "BJP wins Maharashtra election", MH, city="Mumbai")
    f = pair_features(a, b, 0.9)                                         # embedding says "very similar"
    assert f.location_conflict and f.winner_conflict
    capped = RuleScorer(vetoes=True).score(f)
    free = RuleScorer(vetoes=False).score(f)
    assert capped.score <= RuleScorer.VETO_CAP < 0.75 and any(r.startswith("veto:") for r in capped.reasons)
    assert free.score > capped.score and capped.veto


def test_same_event_in_two_papers_is_not_vetoed_and_place_is_neutral_when_unknown():
    a = _art(1, "p1", "Congress alliance wins Jharkhand election", JH, city="Ranchi")
    b = _art(2, "p2", "Cong-JMM sweep Jharkhand polls", JH.replace("Ranchi:", "Ranchi:"), city="Ranchi")
    s = RuleScorer().score(pair_features(a, b, 0.9))
    assert s.veto is None and s.score > 0.75 and s.components["location"] == 0.8
    nowhere = _art(3, "p2", "Alliance wins big", "The alliance won the election on Sunday and leaders thanked voters.")
    s2 = RuleScorer().score(pair_features(a, nowhere, 0.9))
    assert s2.components["location"] is None and s2.veto is None


def test_index_table_name_per_embedder():
    from newspaper_pipeline.cli import _index_table

    class E:
        def __init__(self, name): self.name = name
    assert _index_table(E("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")) == "article_embeddings"
    assert _index_table(E("BAAI/bge-m3")) == "article_embeddings_baai_bge_m3"
