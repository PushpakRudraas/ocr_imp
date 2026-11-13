"""Unit tests for the evaluation tool's metrics (pure functions; no database or models needed)."""
import pytest

from newspaper_pipeline.evaltools.story_eval import Counts, confusion, pair_id, recall_at_k, report


def rows():
    return [
        {"label": "same", "score": 0.90, "rank": 1}, {"label": "same", "score": 0.80, "rank": 2},
        {"label": "same", "score": 0.60, "rank": 12}, {"label": "same", "score": 0.30, "rank": 40},
        {"label": "different", "score": 0.78, "rank": 3}, {"label": "different", "score": 0.40, "rank": 4},
        {"label": "different", "score": 0.20, "rank": 9}, {"label": "different", "score": 0.10, "rank": 30},
        {"label": "unsure", "score": 0.99, "rank": 1},
    ]


def test_confusion_and_rates_at_merge_threshold():
    c = confusion(rows(), 0.75)
    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 2, 3)            # 'unsure' is ignored
    assert c.precision == pytest.approx(2 / 3) and c.recall == pytest.approx(0.5)
    assert c.f1 == pytest.approx(2 * (2 / 3) * 0.5 / (2 / 3 + 0.5))
    assert c.fpr == pytest.approx(1 / 4) and c.fnr == pytest.approx(0.5)


def test_lower_threshold_finds_more_same_pairs():
    lo = confusion(rows(), 0.50)
    assert (lo.tp, lo.fp, lo.fn, lo.tn) == (3, 1, 1, 3)       # 0.60 is now found; 0.78 (different) is still wrongly merged
    assert lo.recall == pytest.approx(0.75) and lo.precision == pytest.approx(0.75)


def test_empty_classes_give_none_not_a_crash():
    c = Counts()
    assert c.precision is None and c.recall is None and c.f1 is None and c.fpr is None and c.fnr is None
    assert confusion([{"label": "different", "score": 0.1}], 0.75).recall is None


def test_candidate_retrieval_recall_uses_only_same_pairs():
    r = recall_at_k(rows(), ks=(1, 5, 10, 20))
    assert r == {1: 0.25, 5: 0.5, 10: 0.5, 20: 0.75}


def test_pair_id_is_order_independent_and_report_mentions_every_metric():
    assert pair_id("a", "b") == pair_id("b", "a") != pair_id("a", "c")
    text = report(rows())
    for word in ("precision", "recall", "F1", "false-positive rate", "false-negative rate", "recall@10", "unsure=1"):
        assert word in text
