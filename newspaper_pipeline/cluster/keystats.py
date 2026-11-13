"""Document frequencies for keyword weighting (step 2 of the story-matching upgrade).

A word that appears in most articles ("election", "government", "BJP") says little about whether two articles are the SAME
event; a rare word (a candidate's name, a constituency, "Jharkhand") says a lot. `KeywordStats` counts in how many articles
each word has appeared and turns that into a multiplier in [FLOOR, 1.0] for `signals.keywords`. Counts can be persisted to a
JSON file so they keep growing across runs. With fewer than MIN_DOCS articles the numbers are too noisy and the factor is 1.0.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from typing import Iterable

MIN_DOCS = 30
FLOOR = 0.35


class KeywordStats:
    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.df: Counter[str] = Counter()
        self.seen: set[str] = set()                      # event ids already counted (a re-run must not count twice)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                self.df, self.seen = Counter(d.get("df", {})), set(d.get("seen", []))
            except (OSError, ValueError):
                pass

    @property
    def n(self) -> int:
        return len(self.seen)

    def update(self, event_id: str, words: Iterable[str]) -> None:
        if event_id in self.seen:
            return
        self.seen.add(event_id)
        self.df.update(set(words))

    def factor(self, word: str) -> float:
        """1.0 for a rare word ... FLOOR for a word found in nearly every article."""
        n = self.n
        if n < MIN_DOCS:
            return 1.0
        idf = math.log((n + 1) / (self.df.get(word, 0) + 1))
        return max(FLOOR, min(1.0, idf / math.log(n + 1)))

    def save(self) -> None:
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"df": dict(self.df), "seen": sorted(self.seen)}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass                                          # statistics are an optimisation: never fail a run over them


def from_env() -> KeywordStats:
    return KeywordStats(os.environ.get("NP_KEYWORD_DF") or None)
