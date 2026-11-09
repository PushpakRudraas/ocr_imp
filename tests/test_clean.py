import pytest

from helpers import PhraseTableBackend
from newspaper_pipeline.clean.lang import detect_language, looks_english, script_shares
from newspaper_pipeline.clean.normalize import clean_article, content_hash, mark_exact_duplicates
from newspaper_pipeline.clean.text import clean_headline, clean_text, normalize_ocr_text
from newspaper_pipeline.clean.translate import (AwsTranslateBackend, TranslationError, chunk_for_translation,
                                                translate_article, translate_text, truncate_utf8)
from newspaper_pipeline.config import Settings
from newspaper_pipeline.input.fetch import fetch_editions
from newspaper_pipeline.input.splitter import split_edition
from newspaper_pipeline.ocr.run import extract_edition


# ------------------------------------------------------------------------------------------- text cleaning
def test_clean_text_reused_behaviour():
    assert clean_text("<p>Hello&nbsp;<b>world</b></p><script>x()</script>") == "Hello world"
    assert clean_text("a​b­ c\x07") == "ab c"
    assert clean_text(None) == ""


def test_dehyphenate_and_ligatures():
    t = normalize_ocr_text("The govern-\nment oﬀered a ﬁnal plan.")
    assert t == "The government offered a final plan."
    # a real hyphenated compound is kept when the next line starts with a capital
    assert "Mumbai-\nIndians" not in normalize_ocr_text("Mumbai-\nIndians won")


def test_reflow_and_paragraphs():
    raw = ("The Reserve Bank of India on Monday kept\nthe repo rate unchanged at 6.5 per cent.\n"
           "Growth was retained.\nMarkets rallied after the decision was announced to\nthe public in Mumbai.")
    t = normalize_ocr_text(raw)
    assert t.count("\n\n") == 1 and "kept the repo rate" in t
    assert t.split("\n\n")[1].startswith("Markets rallied")


def test_furniture_lines_removed():
    raw = "Delhi saw a rise in pollution levels today.\n12\nADVERTISEMENT\n(Continued on page 5)\nResidents were told to stay indoors."
    t = normalize_ocr_text(raw)
    assert "ADVERTISEMENT" not in t and "Continued" not in t and "\n12\n" not in f"\n{t}\n"
    assert "stay indoors" in t


def test_glued_punctuation_repaired():
    assert normalize_ocr_text("CHANDIGARH:Kunwar Singh resigned.The party said no") == \
        "CHANDIGARH: Kunwar Singh resigned. The party said no"
    # abbreviations and decimals untouched
    assert normalize_ocr_text("Mr.Singh paid Rs.4,200 crore at 6.5 per cent") == "Mr.Singh paid Rs.4,200 crore at 6.5 per cent"


def test_clean_headline():
    assert clean_headline("RBI keeps\nrepo   rate­ unchanged") == "RBI keeps repo rate unchanged"


# ------------------------------------------------------------------------------------------- language
def test_detect_language():
    assert detect_language("The Reserve Bank of India said that it will hold the rate for the year and more")[0] == "en"
    code, conf = detect_language("पंजाब विधानसभा से कुंवर विजय प्रताप सिंह ने इस्तीफा दे दिया")
    assert code == "hi" and conf > 0.9
    assert detect_language("पंजाब विधानसभा", hint="mr")[0] == "mr"            # Devanagari: hint picks Marathi
    assert detect_language("தமிழ்நாடு அரசு இன்று அறிவித்தது")[0] == "ta"
    assert detect_language("")[0] == "und"
    assert detect_language("1234 5678")[0] == "und"
    assert detect_language("Bonjour tout le monde, ceci est un texte")[0] == "und"      # Latin but not English


def test_script_shares_mixed():
    sh = script_shares("पंजाब AAP सिंह")
    assert sh["devanagari"] > sh["latin"] > 0


# ------------------------------------------------------------------------------------------- translation
def test_chunking_respects_limit_and_paragraphs():
    text = "One sentence here. Another sentence there.\n\nSecond paragraph starts. It has two sentences."
    chunks = chunk_for_translation(text, 30)
    assert "\n\n" in chunks and all(len(c) <= 30 for c in chunks if c != "\n\n")
    long = "word " * 100
    assert all(len(c) <= 50 for c in chunk_for_translation(long.strip(), 50))
    assert chunk_for_translation("", 50) == []


def test_translate_text_rejoins_paragraphs():
    b = PhraseTableBackend({"A one.": "X one.", "A two.": "X two.", "B one.": "Y one."})
    out = translate_text(b, "A one. A two.\n\nB one.", "hi", "en", 20)
    assert out == "X one. X two.\n\nY one."
    assert all(len(c[0]) <= 20 for c in b.calls)


def test_truncate_utf8_does_not_split_chars():
    s = "पंजाब" * 10
    t = truncate_utf8(s, 20)
    assert len(t.encode()) <= 20 and s.startswith(t)


def test_translate_skips_english_without_calling_backend():
    b = PhraseTableBackend()
    rec = {"headline": "RBI keeps repo rate unchanged at 6.5 per cent",
           "text": "The Reserve Bank of India on Monday kept the repo rate unchanged and said that inflation risks remained elevated.",
           "detected_language": "en"}
    out = translate_article(rec, b)
    assert out["translation_status"] == "skipped_already_target" and b.calls == []


def test_translate_hindi_article():
    b = PhraseTableBackend()
    rec = {"headline": "दिल्ली में प्रदूषण का स्तर खतरनाक", "detected_language": "hi",
           "text": "दिल्ली में सोमवार को वायु गुणवत्ता सूचकांक 430 के पार पहुंच गया। पराली के धुएं के कारण हवा बेहद खराब श्रेणी में पहुंच गई।"}
    out = translate_article(rec, b)
    assert out["translation_status"] == "translated"
    assert out["translated_headline"] == "Pollution level in Delhi dangerous"
    assert out["translated_text"] == "Air quality index in Delhi crossed 430 on Monday. Stubble smoke pushed the air into the very poor category."
    assert rec.get("translated_text") is None                              # input not mutated


def test_translate_failure_modes():
    rec = {"headline": "दिल्ली में प्रदूषण का स्तर खतरनाक", "text": "दिल्ली में सोमवार को वायु गुणवत्ता सूचकांक 430 के पार पहुंच गया।",
           "detected_language": "hi"}
    assert translate_article(rec, None)["translation_status"] == "failed_no_backend"
    partial = translate_article(rec, PhraseTableBackend(fail_on="वायु"))
    assert partial["translation_status"] == "partial" and "translated_headline" in partial and "boom" in partial["translation_error"]
    failed = translate_article(rec, PhraseTableBackend(fail_on="दिल्ली"))
    assert failed["translation_status"] == "failed"
    assert translate_article({"headline": "", "text": ""}, PhraseTableBackend())["translation_status"] == "skipped_no_text"


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeAws:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), []

    def translate_text(self, **kw):
        self.calls.append(kw)
        if self.errors:
            raise self.errors.pop(0)
        return {"TranslatedText": "ok:" + kw["Text"][:5], "SourceLanguageCode": kw["SourceLanguageCode"]}


def test_aws_backend_retries_throttling_then_succeeds():
    sleeps = []
    aws = AwsTranslateBackend(_FakeAws([_ClientError("ThrottlingException")] * 2), sleep=sleeps.append)
    assert aws.translate("नमस्ते दुनिया", "hi", "en").startswith("ok:")
    assert sleeps == [1.0, 2.0] and len(aws.client.calls) == 3


def test_aws_backend_non_retryable_and_exhaustion():
    aws = AwsTranslateBackend(_FakeAws([_ClientError("ValidationException")]), sleep=lambda s: None)
    with pytest.raises(TranslationError, match="ValidationException"):
        aws.translate("x", "hi", "en")
    aws2 = AwsTranslateBackend(_FakeAws([_ClientError("ThrottlingException")] * 9), max_retries=3, sleep=lambda s: None)
    with pytest.raises(TranslationError, match="exhausted 3"):
        aws2.translate("x", "hi", "en")
    aws3 = AwsTranslateBackend(_FakeAws([ConnectionError("down")]), sleep=lambda s: None)       # transport error: retried
    assert aws3.translate("x", "hi", "en").startswith("ok:")


# ------------------------------------------------------------------------------------------- canonical record
@pytest.fixture(scope="module")
def ocr_records(manifest, tmp_path_factory):
    out = tmp_path_factory.mktemp("cl")
    recs = {}
    for e in fetch_editions(manifest, out):
        recs[e.paper.id] = extract_edition(split_edition(e, manifest.run_date, out, dpi=40, render=False))
    return recs


def _pick(recs, needle):
    return next(r for r in recs if needle in r["headline"])


def test_clean_article_english(ocr_records):
    rec = _pick(ocr_records["toi"], "AAP loses")
    t = [0.0]
    ticks = iter([1_800_000_000.000, 1_800_000_000.250])      # clean start, clean end (seconds)
    out = clean_article(rec, None, Settings(), ingested_ms=1_799_999_999_000.0, clock=lambda: next(ticks))
    assert out["detected_language"] == "en" and out["translation_status"] == "skipped_already_target"
    assert out["language_block"] == {"detected": "en", "confidence": "high", "translation_needed": False}
    assert out["input_quality"]["text_level"] == "full_body" and out["input_quality"]["summary_usable"]
    im = out["ingestion_metadata"]
    assert im["published_at"] == "2026-11-02T00:00:00Z" and im["published_precision"] == "date_only"
    assert im["ingest_to_clean_ms"] == 1250 and im["author"] == "TNN"
    assert out["original_text"] == rec["text"] and "\n" not in out["text"].replace("\n\n", "")
    assert len(out["event_id"]) == 64 and out["exact_dup_key"] == f"content_hash:{out['content_hash']}"


def test_clean_article_hindi_translated(ocr_records):
    if "dainik_jagran" not in ocr_records:
        pytest.skip("no Devanagari font")
    rec = next(r for r in ocr_records["dainik_jagran"] if "इस्तीफा" in r["headline"])
    out = clean_article(rec, PhraseTableBackend())
    assert out["detected_language"] == "hi" and out["language_block"]["translation_needed"] is True
    assert out["working_headline"] == "Kunwar Vijay Pratap Singh resigns from Punjab Assembly"
    assert "resigned from the Punjab Assembly" in out["working_text"] and "third MLA" in out["working_text"]
    assert out["text"] != out["working_text"] and out["original_headline"] == rec["headline"]


def test_headline_only_when_body_short():
    rec = {"paper_id": "p", "date": "2026-11-02", "article_id": "a", "headline": "RBI keeps repo rate unchanged", "text": "",
           "ocr_confidence": 0.9, "language": "en", "byline": None, "wire_source": None}
    out = clean_article(rec, None)
    assert out["input_quality"]["text_level"] == "headline_only" and out["input_quality"]["body_available"] is False


def test_content_hash_ignores_case_and_punctuation():
    assert content_hash("RBI keeps repo-rate, unchanged!") == content_hash("rbi keeps repo rate unchanged")
    assert content_hash("a b") != content_hash("a c")


def test_mark_exact_duplicates():
    r = [{"content_hash": "h1", "event_id": "e1"}, {"content_hash": "h2", "event_id": "e2"}, {"content_hash": "h1", "event_id": "e3"}]
    mark_exact_duplicates(r)
    assert [x["exact_duplicate_of"] for x in r] == [None, None, "e1"]


def test_filter_raw_events_drops_stubs_and_same_paper_duplicates():
    from newspaper_pipeline.clean.filter import filter_raw_events
    body = "The Reserve Bank of India kept the repo rate unchanged on Monday."

    def r(i, paper, h, t, conf=0.9, date="2026-11-02"):
        return {"article_id": i, "paper_id": paper, "date": date, "headline": h, "text": t, "ocr_confidence": conf}
    recs = [r("a1", "p1", "RBI holds rate", body),
            r("a2", "p1", "rbi holds RATE!", body + " More detail follows here."),       # same headline, longer text -> kept
            r("a3", "p1", "Different headline here", body),                              # same text as a1 -> duplicate
            r("a4", "p2", "RBI holds rate", body),                                       # other paper -> kept
            r("a5", "p1", "", body), r("a6", "p1", "Short", body),                       # empty / <10 chars headline
            r("a7", "p1", "A fine headline", "   "), r("a8", "p1", "A fine headline", "too short"),
            r("a9", "p1", "RBI holds rate", body, date="2026-11-03")]                    # other date -> kept
    res = filter_raw_events(recs)
    assert [x["article_id"] for x in res.kept] == ["a2", "a4", "a9"]
    why = {x["article_id"]: x["reason"] for x in res.removed}
    assert why == {"a1": "duplicate_headline", "a3": "duplicate_text", "a5": "empty_headline", "a6": "short_headline",
                   "a7": "empty_text", "a8": "short_text"}
    assert res.stats()["removed"] == 6
