import datetime as dt
import json
from pathlib import Path

import pytest

from helpers import PhraseTableBackend
from newspaper_pipeline.clean.normalize import clean_article, mark_exact_duplicates
from newspaper_pipeline.cluster.clusterer import StoryClusterer
from newspaper_pipeline.enrich import analysis as A
from newspaper_pipeline.enrich.entities import extract_entities, resolve_country
from newspaper_pipeline.enrich.event import extract_event, find_trigger, resolve_event_date
from newspaper_pipeline.enrich.geo import geo_scope, resolve_geo
from newspaper_pipeline.enrich.pipeline import enrich_article, enrich_stories, multi_doc_summary
from newspaper_pipeline.enrich.trends import increment_counters, trend_novelty
from newspaper_pipeline.input.fetch import fetch_editions
from newspaper_pipeline.input.splitter import split_edition
from newspaper_pipeline.ocr.run import extract_edition
from newspaper_pipeline.store import Store

SAMPLE = json.loads((Path(__file__).parent.parent.parent / "sample.json").read_text())
RESIGN_H = "Punjab MLA Kunwar Vijay Pratap Singh resigns from Assembly"
RESIGN_T = ("CHANDIGARH, Nov 2 (ANI): Kunwar Vijay Pratap Singh resigned from the Punjab Assembly on Monday, becoming the third "
            "legislator to leave the Aam Aadmi Party in the State this year. He submitted his resignation to the Speaker in "
            "Chandigarh. Opposition parties demanded that a by-election be held without delay. Mr. Singh said he was quitting "
            "over differences with the party leadership on governance issues.")


def names(ents, t):
    return [e["text"] for e in ents if e["type"] == t]


# ------------------------------------------------------------------------------------------------ entities
def test_entities_resignation_story():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    assert names(ents, "PERSON") == ["Kunwar Vijay Pratap Singh"]               # "Mr. Singh" folded in, MLA not a name
    assert {"Punjab", "Chandigarh"} <= set(names(ents, "GPE"))
    assert "Aam Aadmi Party" in names(ents, "ORG") and "Punjab Assembly" in names(ents, "ORG")
    assert "Assembly" not in names(ents, "ORG") and "ANI" in names(ents, "ORG")
    person = next(e for e in ents if e["type"] == "PERSON")
    assert person["role"] == "subject" and person["kb_id"] is None
    q = next(e for e in ents if e["type"] == "CARDINAL_GROUP")
    assert (q["value"], q["unit"]) == (3, "legislators")
    punjab = next(e for e in ents if e["text"] == "Punjab")
    assert punjab["subtype"] == "Indian state" and punjab["country_iso"] == "IN"


def test_entities_acronym_folding_and_dateline_case():
    ents = extract_entities("RBI keeps repo rate unchanged", "MUMBAI: The Reserve Bank of India (RBI) said so. The RBI voted 5-1. "
                            "Governor Sanjay Malhotra said the Monetary Policy Committee agreed.")
    orgs = names(ents, "ORG")
    assert "Reserve Bank of India" in orgs and "Monetary Policy Committee" in orgs
    assert names(ents, "GPE") == ["Mumbai"] and names(ents, "PERSON") == ["Sanjay Malhotra"]


def test_entities_no_false_people_from_sentence_openers():
    ents = extract_entities("Markets rally", "Markets rallied on Monday. Analysts said growth would hold. Schools were closed.")
    assert names(ents, "PERSON") == [] and names(ents, "ORG") == []


def test_quantities_numbers_and_words():
    ents = extract_entities("x", "Three MLAs and 12 farmers met. The 5th MLA left.")
    q = {(e["value"], e["unit"]) for e in ents if e["type"] == "CARDINAL_GROUP"}
    assert (3, "MLAs") in q and (12, "farmers") in q


def test_resolve_country_reused_from_argus():
    assert resolve_country("Pakistan") == "PK" and resolve_country("UK") == "GB" and resolve_country("Narnia") is None


# ------------------------------------------------------------------------------------------------ events
@pytest.mark.parametrize("headline,etype", [
    ("AAP loses another MLA as Vijay Pratap Singh quits Punjab Assembly", "Politics.Resignation"),
    ("Third AAP MLA walks out: Vijay Pratap Singh resigns, Punjab by-election likely", "Politics.Resignation"),
    ("RBI keeps repo rate unchanged at 6.5%", "Economy.MonetaryPolicy"),
    ("Metro line extension gets nod", "Government.Approval"),
    ("Delhi pollution: AQI slips to 'severe' as stubble smoke blows in", "Environment.AirQuality"),
    ("Police arrest two over bank fraud", "Crime.Arrest"),
    ("Six killed as bus falls into gorge", "Conflict.Fatality"),
    ("Weather: cold wave to continue", "Disaster.NaturalHazard"),
    ("Something happened somewhere", "General.Report"),
])
def test_event_type_from_headline(headline, etype):
    assert find_trigger(headline, "")[0] == etype


def test_event_actor_institution_date_and_confidence():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    ev, rel, notes = extract_event(RESIGN_H, RESIGN_T, ents, dt.date(2026, 11, 2), "CHANDIGARH, Nov 2 (ANI)")
    assert ev == {"type": "Politics.Resignation", "trigger": "resigns", "actor": "Kunwar Vijay Pratap Singh",
                  "institution": "Punjab Assembly", "date": "2026-11-02", "confidence": "high"}
    assert ["Kunwar Vijay Pratap Singh", "resigned_from", "Punjab Assembly"] in rel
    assert ["Kunwar Vijay Pratap Singh", "former_member_of", "Aam Aadmi Party"] in rel
    assert ["Aam Aadmi Party", "has_defections_count", "3 legislators"] in rel
    assert "party link stated in the text" in notes and "ordinal" in notes


def test_event_relations_monetary_policy():
    h, t = "RBI keeps repo rate unchanged at 6.5%", ("MUMBAI: The Reserve Bank of India on Monday kept the repo rate unchanged at "
                                                    "6.5 per cent. Governor Sanjay Malhotra said the Monetary Policy Committee voted 5-1.")
    ents = extract_entities(h, t)
    ev, rel, _ = extract_event(h, t, ents, dt.date(2026, 11, 2))
    assert ev["type"] == "Economy.MonetaryPolicy" and ev["actor"] == "Reserve Bank of India"
    assert ["Reserve Bank of India", "holds_rate_at", "6.5 per cent"] in rel
    assert ["Sanjay Malhotra", "governor_of", "Reserve Bank of India"] in rel


def test_event_date_resolution():
    pub = dt.date(2026, 11, 3)                                               # a Tuesday
    assert resolve_event_date("It happened on Monday.", pub) == dt.date(2026, 11, 2)
    assert resolve_event_date("It happened yesterday.", pub) == dt.date(2026, 11, 2)
    assert resolve_event_date("Nothing dated.", pub) == pub
    assert resolve_event_date("x", pub, "CHANDIGARH, Nov 1 (PTI)") == dt.date(2026, 11, 1)
    assert resolve_event_date("x", pub, "चंडीगढ़, 1 नवंबर") == pub               # unparseable dateline falls back


def test_low_confidence_when_no_trigger():
    ev, rel, _ = extract_event("Quiet day", "Nothing much to report here at all.", [], dt.date(2026, 11, 2))
    assert ev["type"] == "General.Report" and ev["confidence"] == "low" and rel == []


# ------------------------------------------------------------------------------------------------ geo
def test_geo_state_story_with_disambiguation():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    geo = resolve_geo(RESIGN_H, RESIGN_T, ents, "Chandigarh", "Punjab Assembly")
    assert (geo["primary_location"], geo["admin_level"], geo["country_iso"]) == ("Punjab", "state", "IN")
    assert (geo["lat"], geo["lon"]) == (31.15, 75.34)
    assert geo["disambiguation"].startswith("India, not Pakistan, because the text mentions")
    assert "centroid" in geo["coordinates_note"]


def test_geo_city_and_foreign_and_none():
    ents = extract_entities("RBI holds rate", "MUMBAI: The Reserve Bank of India held the repo rate.")
    geo = resolve_geo("RBI holds rate", "MUMBAI: The Reserve Bank of India held the repo rate.", ents, "Mumbai", None)
    assert geo["primary_location"] == "Mumbai" and geo["admin_level"] == "city"
    h, t = "Pakistan and China sign pact", "Pakistan and China signed a pact in Beijing on Monday."
    g = resolve_geo(h, t, extract_entities(h, t), None, None)
    assert g["country_iso"] in ("PK", "CN") and g["admin_level"] == "country"
    none = resolve_geo("quiet", "nothing here", [], None, None)
    assert none["primary_location"] is None and none["lat"] is None


def test_geo_scope():
    assert geo_scope({"admin_level": "state"}, "") == "state" and geo_scope({"admin_level": "city"}, "") == "local"
    assert geo_scope({"admin_level": "country", "country_iso": "IN"}, "") == "national"
    assert geo_scope({"admin_level": "country", "country_iso": "PK"}, "") == "international"
    assert geo_scope({"admin_level": None}, "The Centre announced") == "national"


# ------------------------------------------------------------------------------------------------ analysis blocks
def test_classification():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    ev, _, _ = extract_event(RESIGN_H, RESIGN_T, ents, dt.date(2026, 11, 2))
    geo = resolve_geo(RESIGN_H, RESIGN_T, ents, None, ev["institution"])
    c = A.classify(RESIGN_H, RESIGN_T, ev, geo)
    assert c["topic"] == "politics" and c["suggested_domain"] == "politics" and c["argus_domain_code"] == "P"
    assert c["subtopic"] == "legislator resignation / party defection" and c["geo_scope"] == "state"
    assert c["current_domain_field"] == "misc" and c["topic_confidence"] in ("high", "medium")


def test_sentiment_neutral_news_and_negative_entity():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    s = A.sentiment(RESIGN_H, RESIGN_T, ents)
    assert s["document"]["label"] in ("neutral", "negative")
    neg = A.sentiment("AAP faces crisis", "The AAP suffered a loss. Defections piled pressure on the AAP leadership. The AAP faces a crisis.",
                      extract_entities("AAP faces crisis", "The AAP suffered a loss. Defections piled pressure on the AAP leadership."))
    aap = next(e for e in neg["entity_level"] if e["entity"] in ("AAP", "Aam Aadmi Party"))
    assert aap["stance"] == "negative" and "negative terms" in aap["reason"]
    pos = A.sentiment("Markets rally on growth", "Stocks rallied and gains continued. Growth was a record, a big boost.", [])
    assert pos["document"]["label"] == "positive" and pos["document"]["score"] > 0.2


def test_emotion_tone_and_urgency():
    ev = {"type": "Politics.Resignation"}
    assert A.emotion_tone("h", "Plain facts only.", ev, True, "neutral") == {"dominant": "neutral", "tone": "factual / hard news", "urgency": "medium"}
    e = A.emotion_tone("Outrage", "Leaders slammed and condemned the move, furious critics blamed ministers.", ev, False, "negative")
    assert e["dominant"] == "anger"
    assert A.emotion_tone("BREAKING", "x", {"type": "General.Report"}, False, "neutral")["urgency"] == "high"
    assert A.emotion_tone("x", "Six killed in blast", {"type": "Accident.Incident"}, False, "neutral")["urgency"] == "high"


def test_credibility_clickbait_and_propaganda():
    rec = {"paper_name": "P", "reputation": "high", "wire_source": "PTI"}
    c = A.credibility(rec, "RBI keeps repo rate unchanged", "The bank said so.")
    assert c["clickbait_score"] == "low" and c["wire_backed"] and c["propaganda_indicators"] == "none detected in text"
    bad = A.credibility({"paper_name": "P"}, "SHOCKING!!! You won't believe what the traitor did", "The regime puppet...")
    assert bad["clickbait_score"] == "high" and "traitor" in bad["propaganda_indicators"]
    assert bad["source_reputation"] == "not_rated"


def test_claims_figures_flagged_opinions_demoted():
    text = ("The RBI kept the repo rate at 6.5 per cent. Growth is expected to improve and the government should act. "
            "Governor Sanjay Malhotra said the committee voted 5-1. Analysts believe things may get better.")
    ents = extract_entities("RBI holds rate", text)
    cl = A.extract_claims("RBI holds rate", text, ents)
    assert cl[0]["claim"].startswith("The RBI kept the repo rate") and cl[0]["checkworthy"] is True
    assert "figure" in cl[0]["next_step"] and cl[0]["verification_status"] == "unverified"
    assert not any("should act" in c["claim"] and c["checkworthy"] for c in cl)
    # "speak to supporters first." is not an ordinal claim
    cl2 = A.extract_claims("h", "He said he would speak to supporters first and nothing more happened.", [])
    assert all("figure" not in c["next_step"] for c in cl2)


def test_summary_and_sentence_splitter_abbreviations():
    s = A.sentences("Mr. Singh paid Rs. 4,200 crore. Dr. Rao agreed. It ended.")
    assert s == ["Mr. Singh paid Rs. 4,200 crore.", "Dr. Rao agreed.", "It ended."]
    ents = extract_entities(RESIGN_H, RESIGN_T)
    one = A.one_line_summary(RESIGN_H, RESIGN_T.split(": ", 1)[1], ents)
    assert one.startswith("Kunwar Vijay Pratap Singh resigned") and one.endswith(".") and len(one) <= 241
    assert A.one_line_summary("Just a headline", "", []) == "Just a headline"


def test_knowledge_graph_and_keyphrases():
    ents = extract_entities(RESIGN_H, RESIGN_T)
    ev, rel, _ = extract_event(RESIGN_H, RESIGN_T, ents, dt.date(2026, 11, 2))
    kg = A.knowledge_graph(ents, ev, rel)
    labels = {n["id"]: n["label"] for n in kg["nodes"]}
    assert labels["aam_aadmi_party"] == "PoliticalParty" and labels["kunwar_vijay_pratap_singh"] == "Person"
    edges = {(e["from"], e["rel"], e["to"]) for e in kg["edges"]}
    assert ("kunwar_vijay_pratap_singh", "RESIGNED_FROM", "punjab_assembly") in edges
    assert ("punjab_assembly", "LOCATED_IN", "punjab") in edges
    assert next(e for e in kg["edges"] if e["rel"] == "RESIGNED_FROM")["date"] == "2026-11-02"
    kp = A.keyphrases(RESIGN_H, RESIGN_T, ents, ev)
    assert kp[0] == "Punjab Assembly resignation" and "AAP defections" in kp and len(kp) <= 6


def test_impact_monotonic():
    rec = {"reputation": "high", "wire_source": None, "paper_name": "P"}
    sev = A.impact_score({"type": "Conflict.Fatality"}, "national", [], "Six killed, many injured in attack", rec)
    mild = A.impact_score({"type": "Government.Approval"}, "local", [], "Metro approved", rec)
    assert sev["value"] > mild["value"] and 0 <= mild["value"] <= 1
    more_papers = A.impact_score({"type": "Government.Approval"}, "local", [], "Metro approved", rec, n_papers=4)
    assert more_papers["value"] > mild["value"]
    assert set(sev["components"]) == {"event_severity", "geo_scope", "entity_importance", "source_reach"}


# ------------------------------------------------------------------------------------------------ trends / store
def test_store_documents_and_counters(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    s.put("clean", "e1", {"event_id": "e1", "date": "2026-11-02", "paper_id": "p"})
    s.put("clean", "e1", {"event_id": "e1", "date": "2026-11-02", "paper_id": "p", "v": 2})        # upsert
    assert s.count("clean") == 1 and s.get("clean", "e1")["v"] == 2 and s.get("clean", "nope") is None
    assert [d["event_id"] for d in s.all("clean", "2026-11-02")] == ["e1"] and s.all("clean", "2026-12-01") == []
    s.increment("k", "2026-11-01"); s.increment("k", "2026-11-01", 2)
    assert s.counter("k", "2026-11-01") == 3 and s.history("k", "2026-11-02") == [3] and s.days_of_history("2026-11-02") == 1
    s2 = Store(tmp_path / "x.sqlite")                                                              # persisted
    assert s2.get("clean", "e1")["v"] == 2


def test_trends_baseline_then_burst_then_novel():
    keys = ["entity:AAP", "topic:politics"]
    assert trend_novelty(keys, "2026-11-10", None)["burst_status"] == "needs_corpus"
    st = Store()
    assert trend_novelty(keys, "2026-11-10", st)["burst_status"] == "baseline_building"
    for d in range(1, 6):                                         # five quiet days: 1 mention each
        for k in keys:
            st.increment(k, f"2026-11-0{d}")
    quiet = trend_novelty(keys, "2026-11-10", st)
    assert quiet["burst_status"] == "normal" and quiet["novelty_status"] == "seen_before"
    for _ in range(6):
        st.increment("entity:AAP", "2026-11-10")
    t = trend_novelty(keys + ["entity:NewGuy"], "2026-11-10", st)
    assert t["burst_status"] == "burst" and t["bursting"] == ["entity:AAP"]
    assert t["novelty_status"] == "novel" and t["novel_keys"] == ["entity:NewGuy"]
    increment_counters(["a", "a", "b"], "2026-11-10", st)
    assert st.counter("a", "2026-11-10") == 1                     # de-duplicated within a call


# ------------------------------------------------------------------------------------------------ assembled docs
@pytest.fixture(scope="module")
def docs(manifest, tmp_path_factory):
    out = tmp_path_factory.mktemp("en")
    arts = []
    for e in fetch_editions(manifest, out):
        for r in extract_edition(split_edition(e, manifest.run_date, out, dpi=40, render=False)):
            arts.append(clean_article(r, PhraseTableBackend()))
    mark_exact_duplicates(arts)
    StoryClusterer().cluster_all(arts)
    return enrich_stories([enrich_article(a) for a in arts])


def _doc(docs, paper, needle):
    return next(d for d in docs if d["newspaper"]["paper_id"] == paper and needle in d["newspaper"]["headline"])


def test_doc_has_every_sample_json_field(docs):
    d = _doc(docs, "toi", "AAP loses")
    missing = set(SAMPLE) - set(d)
    assert not missing, f"fields in sample.json but not produced: {missing}"
    for block in ("input_quality", "ingestion_metadata", "language", "event", "geo", "classification", "sentiment",
                  "emotion_tone", "credibility", "dedup_clustering", "trend_novelty", "summary", "timeline_entry",
                  "knowledge_graph", "impact_score", "provenance"):
        absent = set(SAMPLE[block]) - set(d[block])
        assert not absent, f"{block}: missing sub-fields {absent}"
    assert set(d["claims"][0]) >= set(SAMPLE["claims"][0])
    assert set(d["entities"][0]) >= {"text", "type"} and d["entity_linking"]["status"] == "unresolved"
    json.dumps(d)                                                      # fully serialisable


def test_input_quality_is_full_body_not_headline_only(docs):
    d = _doc(docs, "toi", "AAP loses")
    assert d["input_quality"]["text_level"] == "full_body" and d["input_quality"]["summary_usable"] is True
    assert d["geo"]["primary_location"] == "Punjab" and d["event"]["type"] == "Politics.Resignation"
    assert d["event"]["actor"] in ("Kunwar Vijay Pratap Singh", "Vijay Pratap Singh")


def test_story_level_fields_filled(docs):
    d = _doc(docs, "the_hindu", "Punjab MLA")
    assert d["story"]["n_papers"] >= 3
    mds = d["summary"]["multi_doc_summary"]
    assert mds.startswith(f"Reported by {d['story']['n_papers']} papers") and "Punjab Assembly" in mds
    assert d["dedup_clustering"]["cluster_status"] in ("joined_story", "new_story")
    assert d["dedup_clustering"]["suggested_story_key"].endswith("2026-11")
    assert any(c["verification_status"] == "corroborated" and c["corroborated_by"] for c in d["claims"])
    assert d["credibility"]["bias_label"] in ("consistent framing across papers", "framing varies across papers")
    assert set(d["credibility"]["cross_paper"]["headline_by_paper"]) >= {"The Hindu", "The Times of India"}
    solo = _doc(docs, "the_hindu", "Metro")
    assert solo["story"]["n_papers"] == 1 and solo["summary"]["multi_doc_summary"].startswith("single_source")
    assert d["impact_score"]["value"] > solo["impact_score"]["value"]


def test_stories_are_idempotent(docs):
    before = json.dumps(docs, sort_keys=True)
    enrich_stories(docs)
    assert json.dumps(docs, sort_keys=True) == before


def test_conflicting_figures_detected():
    def mk(paper, text):
        return {"event_id": paper, "story": {"story_id": "s1"},
                "newspaper": {"paper": paper, "paper_id": paper, "text": text, "translated_text": None, "page_no": 1,
                              "headline": "h", "wire_source": None},
                "summary": {"one_line": "x", "multi_doc_summary": ""}, "claims": [{"claim": text, "checkworthy": True,
                "verification_status": "unverified", "next_step": ""}], "sentiment": {"document": {"label": "neutral"}},
                "credibility": {"source_reputation": "high", "bias_label": ""}, "entities": [],
                "event": {"type": "Economy.MonetaryPolicy"}, "classification": {"geo_scope": "national"},
                "trend_novelty": {"counters_to_increment": []}, "impact_score": {}}
    a = mk("A", "The Monetary Policy Committee voted 5-1 to keep the repo rate unchanged on Monday.")
    b = mk("B", "The Monetary Policy Committee voted 4-2 to keep the repo rate unchanged on Monday.")
    c = mk("C", "The Monetary Policy Committee voted 5-1 to keep the repo rate unchanged on Monday.")
    enrich_stories([a, b, c])
    # B disagrees with A and C; A and C agree with each other but conflict with B
    assert b["claims"][0]["verification_status"] == "conflicting_figures"
    assert a["claims"][0]["verification_status"] == "conflicting_figures"      # sees B's different count too
    only_agree = [mk("A", a["claims"][0]["claim"]), mk("C", c["claims"][0]["claim"])]
    enrich_stories(only_agree)
    assert only_agree[0]["claims"][0]["verification_status"] == "corroborated"


def test_multi_doc_summary_prefers_agreed_sentences():
    def m(paper, text):
        return {"newspaper": {"paper": paper, "text": text, "translated_text": None}, "summary": {"one_line": text}}
    shared = "The Reserve Bank of India kept the repo rate unchanged at 6.5 per cent on Monday."
    members = [m("A", shared + " Bank stocks rallied after the policy announcement."),
               m("B", shared + " A cricket match was held in the city yesterday evening."),
               m("C", "RBI left the repo rate unchanged at 6.5 per cent on Monday, officials in Mumbai said.")]
    s = multi_doc_summary(members)
    assert s.startswith("Reported by 3 papers") and "repo rate unchanged" in s
    assert s.count("repo rate") <= 2                                            # near-duplicate sentences are not repeated


def test_trend_fields_use_store_when_given(manifest, tmp_path):
    arts = []
    for e in fetch_editions(manifest, tmp_path):
        for r in extract_edition(split_edition(e, manifest.run_date, tmp_path, dpi=40, render=False)):
            arts.append(clean_article(r, PhraseTableBackend()))
    StoryClusterer().cluster_all(arts)
    st = Store()
    out = enrich_stories([enrich_article(a) for a in arts], st, "2026-11-02")
    assert all(d["trend_novelty"]["burst_status"] == "baseline_building" for d in out)
    # counters are incremented once per STORY, not once per newspaper copy
    assert st.counter("topic:politics", "2026-11-02") <= len({d["story"]["story_id"] for d in out})
    assert st.counter("event:resignation", "2026-11-02") == 1
