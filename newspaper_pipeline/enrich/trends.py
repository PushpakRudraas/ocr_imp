"""Burst and novelty from daily counters. With fewer than `min_days` of history there is no baseline, so the status
says so instead of guessing."""
from __future__ import annotations

import statistics

from ..store import Store


def trend_novelty(keys: list[str], day: str, store: Store | None, min_days: int = 3) -> dict:
    base = {"counters_to_increment": keys}
    if store is None:
        return {"burst_status": "needs_corpus", "novelty_status": "needs_corpus", **base}
    if store.days_of_history(day) < min_days:
        return {"burst_status": "baseline_building", "novelty_status": "baseline_building",
                "history_days": store.days_of_history(day), **base}
    bursting, novel = [], []
    for k in keys:
        hist = store.history(k, day)
        if not hist:
            novel.append(k)
            continue
        today = store.counter(k, day)
        mean = statistics.mean(hist)
        sd = statistics.pstdev(hist) or 1.0
        if today >= 3 and (today - mean) / sd >= 2:
            bursting.append(k)
    return {"burst_status": "burst" if bursting else "normal", "bursting": bursting,
            "novelty_status": "novel" if novel else "seen_before", "novel_keys": novel,
            "history_days": store.days_of_history(day), **base}


def increment_counters(keys: list[str], day: str, store: Store) -> None:
    for k in set(keys):
        store.increment(k, day)
