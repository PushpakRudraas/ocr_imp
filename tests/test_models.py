"""The HuggingFace model path of enrichment, driven by a fake `transformers.pipeline` so it runs offline."""
import datetime as dt

from newspaper_pipeline.enrich import analysis as A
from newspaper_pipeline.enrich.entities import extract_entities
from newspaper_pipeline.enrich.models import HfModels, TOPIC_LABELS, _chunks


def fake_factory(task, model, **kw):
    if task == "token-classification":
        def ner(text):
            out = []
            for name, grp in (("Vijay Pratap Singh", "PER"), ("Punjab Assembly", "ORG"), ("Chandigarh", "LOC"),
                              ("Monday", "MISC"), ("Vijay", "PER")):
                if name in text:
                    out.append({"entity_group": grp, "word": name, "score": 0.97})
            if "AAP" in text:                                                         # truncated span, widened by offsets
                i = text.index("AAP"); out.append({"entity_group": "ORG", "word": "AA", "score": 0.9, "start": i, "end": i + 2})
            if "Congress" in text:
                out.append({"entity_group": "LOC", "word": "Congress", "score": 0.9})
            if "Assembly" in text:
                out.append({"entity_group": "ORG", "word": "Assembly", "score": 0.9})
            out.append({"entity_group": "PER", "word": "Xy", "score": 0.2})          # below min score
            return out
        return ner
    if task == "zero-shot-classification":
        def zs(text, candidate_labels, **k):
            ranked = sorted(candidate_labels, key=lambda l: l != TOPIC_LABELS["politics"])
            return {"labels": ranked, "scores": [0.7] + [0.3 / (len(ranked) - 1)] * (len(ranked) - 1)}
        return zs
    if kw.get("top_k") is None:                                                       # sentiment
        return lambda text: [[{"label": "negative", "score": 0.7}, {"label": "neutral", "score": 0.2},
                              {"label": "positive", "score": 0.1}]]
    return lambda text: [[{"label": "anger", "score": 0.8}]]                          # emotion


M = HfModels(pipeline_factory=fake_factory)
HEAD = "Vijay Pratap Singh resigns"
TEXT = ("Vijay Pratap Singh quit the Punjab Assembly on Monday in Chandigarh. He said Vijay would not return. "
        "The AAP lost a seat and Congress reacted.")


def test_ner_maps_labels_and_filters_low_scores():
    ms = M.ner(TEXT)
    assert {(m.text, m.kind) for m in ms} == {("Vijay Pratap Singh", "PERSON"), ("Punjab Assembly", "ORG"),
                                              ("Chandigarh", "GPE"), ("Vijay", "PERSON"), ("AAP", "ORG"),
                                              ("Congress", "GPE"), ("Assembly", "ORG")}


def test_entities_from_model_fold_and_decorate():
    ents = extract_entities(HEAD, TEXT, models=M)
    by = {e["text"]: e for e in ents}
    assert set(by) == {"Vijay Pratap Singh", "Punjab Assembly", "Chandigarh", "AAP", "Congress"}   # Vijay/Assembly folded
    assert by["Congress"]["type"] == "ORG" and by["AAP"]["type"] == "ORG"
    assert by["Vijay Pratap Singh"]["type"] == "PERSON" and by["Vijay Pratap Singh"]["role"] == "subject"
    assert by["Punjab Assembly"]["subtype"] == "legislature" and by["Chandigarh"]["country_iso"] == "IN"


def test_topic_sentiment_emotion_from_models():
    ev = {"type": "Politics.Resignation"}
    cls = A.classify(HEAD, TEXT, ev, {"scope": "x", "country_iso": "IN", "primary_location": "Chandigarh"}, M)
    assert cls["topic"] == "politics" and cls["topic_confidence"] == "high"
    ents = extract_entities(HEAD, TEXT, models=M)
    s = A.sentiment(HEAD, TEXT, ents, M)
    assert s["document"] == {"label": "negative", "score": -0.6}
    assert s["entity_level"][0]["stance"] == "negative"
    e = A.emotion_tone(HEAD, TEXT, ev, True, "negative", M)
    assert e["dominant"] == "anger" and e["tone"] == "anger-laden reporting"


def test_chunks_respect_limit():
    parts = _chunks("Sentence one. " * 400, 500)
    assert len(parts) > 1 and all(len(p) <= 500 for p in parts)
    assert _chunks("") == []
