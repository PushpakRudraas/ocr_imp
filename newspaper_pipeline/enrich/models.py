"""Real NLP models for enrichment (HuggingFace transformers, lazily loaded, CPU or GPU).

| job            | model                                                  |
|----------------|--------------------------------------------------------|
| topic          | MoritzLaurer/mDeBERTa-v3-base-mnli-xnli (zero-shot)    |
| entities (NER) | Babelscape/wikineural-multilingual-ner                 |
| sentiment      | cardiffnlp/twitter-xlm-roberta-base-sentiment          |
| emotion        | j-hartmann/emotion-english-distilroberta-base          |

(The embedding model, paraphrase-multilingual-MiniLM-L12-v2, lives in cluster/embed.py.) Each model loads on first
use. `pipeline_factory(task, model, **kw)` can be injected for tests; the default is `transformers.pipeline`.
Emotion is English-only, so feed it the translated working text.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("np.models")
# kwargs that belong to pipeline construction (the rest are per-call), per model key
_PIPE_INIT_KW = {"ner": {"aggregation_strategy"}, "sentiment": {"top_k", "truncation", "max_length"},
                 "emotion": {"top_k", "truncation", "max_length"}, "category": set()}

CATEGORY_MODEL = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
NER_MODEL = "Babelscape/wikineural-multilingual-ner"
SENTIMENT_MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
EMOTION_MODEL = "j-hartmann/emotion-english-distilroberta-base"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# topic key (matches lexicons.TOPIC_TO_DOMAIN) -> natural-language label shown to the zero-shot model
TOPIC_LABELS = {
    "politics": "politics and government", "business": "business and the economy", "sports": "sports",
    "crime": "crime and courts", "environment": "environment, pollution and weather",
    "disaster": "natural disaster", "health": "health and medicine", "technology": "science and technology",
    "world": "international affairs", "infrastructure": "infrastructure and transport",
    "entertainment": "entertainment and culture",
}
NER_KIND = {"PER": "PERSON", "ORG": "ORG", "LOC": "GPE"}          # MISC is dropped: too noisy to type
_MAX_CHARS = 1500


def _chunks(text: str, limit: int = _MAX_CHARS) -> list[str]:
    """Sentence-aligned pieces small enough for a 512-token model."""
    out, cur = [], ""
    for s in re.split(r"(?<=[.!?।])\s+|\n+", text or ""):
        if cur and len(cur) + len(s) + 1 > limit:
            out.append(cur); cur = ""
        cur = f"{cur} {s}".strip()
        while len(cur) > limit:                       # one unbroken run: hard split
            out.append(cur[:limit]); cur = cur[limit:]
    if cur.strip():
        out.append(cur)
    return out


def _whole_words(chunk: str, r: dict) -> str | None:
    """Widen a span to word boundaries: the NER model sometimes stops mid-token ("AA" for "AAP")."""
    a, b = r.get("start"), r.get("end")
    if not isinstance(a, int) or not isinstance(b, int) or not 0 <= a < b <= len(chunk):
        return None
    while a > 0 and chunk[a - 1].isalnum():
        a -= 1
    while b < len(chunk) and chunk[b].isalnum():
        b += 1
    return " ".join(chunk[a:b].split())


@dataclass
class Mention:
    text: str
    kind: str            # PERSON | ORG | GPE
    score: float


class HfModels:
    def __init__(self, device: int | str | None = None, pipeline_factory: Callable | None = None,
                 category: str = CATEGORY_MODEL, ner: str = NER_MODEL, sentiment: str = SENTIMENT_MODEL,
                 emotion: str = EMOTION_MODEL, min_ner_score: float = 0.6) -> None:
        self.device, self._factory = device, pipeline_factory
        self.names = {"category": category, "ner": ner, "sentiment": sentiment, "emotion": emotion}
        self.min_ner_score = min_ner_score
        self.batch_size = max(1, int(os.environ.get("NP_HF_BATCH", "32")))
        self._p: dict[str, Callable] = {}
        self._cache: dict[tuple[str, str], object] = {}      # (job, input text) -> raw pipeline output, filled by prefetch()

    # ------------------------------------------------------------------------------------------------ batching
    def _batched(self, key: str, task: str, texts: list[str], **kw) -> list:
        """Run one pipeline over many texts in GPU batches; outputs line up with `texts`."""
        pipe = self._pipe(key, task, **{k: v for k, v in kw.items() if k in _PIPE_INIT_KW[key]})
        call_kw = {k: v for k, v in kw.items() if k not in _PIPE_INIT_KW[key]}
        return list(pipe(texts, batch_size=self.batch_size, **call_kw))

    def prefetch(self, items: list[tuple[str, str]]) -> None:
        """Pre-compute every model output the per-article calls below will ask for, for all (headline, text) pairs at
        once, so the GPU sees full batches instead of one tiny call per article. Purely an optimisation: any failure
        leaves the cache partly empty and the per-article methods simply compute what is missing."""
        if not items:
            return
        jobs = {"ner": [], "sent": [], "emo": [], "topic": []}
        for head, text in items:
            for part in (head, text):
                jobs["ner"] += _chunks(part)
            doc = f"{head}. {text}"[:_MAX_CHARS]
            for k in ("sent", "emo", "topic"):
                jobs[k].append(doc)
        specs = {"ner": ("ner", "token-classification", {"aggregation_strategy": "simple"}),
                 "sent": ("sentiment", "text-classification", {"top_k": None, "truncation": True, "max_length": 512}),
                 "emo": ("emotion", "text-classification", {"top_k": 1, "truncation": True, "max_length": 512}),
                 "topic": ("category", "zero-shot-classification",
                           {"candidate_labels": list(TOPIC_LABELS.values()),
                            "hypothesis_template": "This news article is about {}.", "multi_label": False})}
        for job, texts in jobs.items():
            todo = [t for t in dict.fromkeys(texts) if t.strip() and (job, t) not in self._cache]
            if not todo:
                continue
            key, task, kw = specs[job]
            # longest first: similar lengths per batch means less padding
            todo.sort(key=len, reverse=True)
            try:
                for t, r in zip(todo, self._batched(key, task, todo, **kw)):
                    self._cache[(job, t)] = r
            except Exception as e:                                   # noqa: BLE001
                log.warning("batched %s failed (%s); falling back to per-article calls", job, e)

    def _cached(self, job: str, text: str, compute: Callable[[], object]):
        hit = self._cache.get((job, text))
        return hit if hit is not None else compute()

    def clear_cache(self) -> None:
        self._cache.clear()

    def _pipe(self, key: str, task: str, **kw):
        if key not in self._p:
            if self._factory:
                self._p[key] = self._factory(task, self.names[key], **kw)
            else:
                from transformers import pipeline
                if self.device is not None:
                    kw["device"] = self.device
                self._p[key] = pipeline(task, model=self.names[key], **kw)
        return self._p[key]

    # ------------------------------------------------------------------------------------------------ NER
    def ner(self, text: str) -> list[Mention]:
        pipe = self._pipe("ner", "token-classification", aggregation_strategy="simple")
        out: list[Mention] = []
        for chunk in _chunks(text):
            for r in self._cached("ner", chunk, lambda: pipe(chunk)):
                kind = NER_KIND.get(str(r.get("entity_group", "")).upper())
                name = _whole_words(chunk, r) or re.sub(r"\s+", " ", str(r.get("word", ""))).replace(" ##", "")
                name = name.strip(" .,;:'\"()-")
                if kind and len(name) >= 2 and float(r.get("score", 0)) >= self.min_ner_score:
                    out.append(Mention(name, kind, float(r["score"])))
        return out

    # ------------------------------------------------------------------------------------------------ topic
    def topic(self, text: str) -> tuple[str, float, dict[str, float]]:
        """(best topic key, probability, all probabilities). Single-label zero-shot over TOPIC_LABELS."""
        pipe = self._pipe("category", "zero-shot-classification")
        labels = list(TOPIC_LABELS.values())
        r = self._cached("topic", text[:_MAX_CHARS], lambda: pipe(
            text[:_MAX_CHARS], candidate_labels=labels, hypothesis_template="This news article is about {}.",
            multi_label=False))
        back = {v: k for k, v in TOPIC_LABELS.items()}
        probs = {back[l]: float(s) for l, s in zip(r["labels"], r["scores"])}
        best = max(probs, key=probs.get)
        return best, probs[best], probs

    # ------------------------------------------------------------------------------------------------ sentiment
    def sentiment(self, text: str) -> tuple[str, float]:
        """(label, score in -1..1 = P(positive) - P(negative)) from the XLM-R sentiment model."""
        pipe = self._pipe("sentiment", "text-classification", top_k=None, truncation=True, max_length=512)
        r = self._cached("sent", text[:_MAX_CHARS], lambda: pipe(text[:_MAX_CHARS]))
        r = r[0] if r and isinstance(r[0], list) else r
        p = {str(x["label"]).lower(): float(x["score"]) for x in r}
        score = round(p.get("positive", 0.0) - p.get("negative", 0.0), 3)
        label = max(("negative", "neutral", "positive"), key=lambda k: p.get(k, 0.0))
        return label, score

    # ------------------------------------------------------------------------------------------------ emotion
    def emotion(self, text: str) -> tuple[str, float]:
        pipe = self._pipe("emotion", "text-classification", top_k=1, truncation=True, max_length=512)
        r = self._cached("emo", text[:_MAX_CHARS], lambda: pipe(text[:_MAX_CHARS]))
        r = r[0] if r and isinstance(r[0], list) else r
        top =r[0] if isinstance(r, list) else r
        return str(top["label"]).lower(), float(top["score"])
