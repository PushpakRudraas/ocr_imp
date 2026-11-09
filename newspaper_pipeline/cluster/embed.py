"""Embeddings for story matching. `HashingEmbedder` is deterministic and offline (default; fine for same-language
or already-translated text). `SentenceTransformerEmbedder("BAAI/bge-m3")` is the multilingual production option -
with it, Hindi and English copies of a story can be matched even without translation."""
from __future__ import annotations

from typing import Protocol

import numpy as np


class Embedder(Protocol):
    dim: int
    def embed(self, texts: list[str]) -> np.ndarray:
        """Returns an (n, dim) float32 array of L2-normalised vectors."""


def embedding_text(article: dict, max_chars: int = 1500) -> str:
    """Headline + the start of the (English) body: the lead carries who/what/where for a news story."""
    head = article.get("working_headline") or article.get("headline") or ""
    body = article.get("working_text") or article.get("text") or ""
    return f"{head}\n{body[:max_chars]}".strip()


class HashingEmbedder:
    name = "hashing-tfidf"
    cos_ref = 0.40          # cosine that counts as "clearly the same story" for this embedder

    def __init__(self, dim: int = 4096) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer
        self.dim = dim
        self._vec = HashingVectorizer(n_features=dim, alternate_sign=False, norm="l2", lowercase=True,
                                      stop_words="english", ngram_range=(1, 2),
                                      token_pattern=r"(?u)\b[^\W_]{2,}\b")

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        m = self._vec.transform(texts).toarray().astype(np.float32)
        return np.sqrt(m) / np.maximum(np.linalg.norm(np.sqrt(m), axis=1, keepdims=True), 1e-9)   # sublinear tf


class SentenceTransformerEmbedder:
    def __init__(self, model: str = "BAAI/bge-m3", device: str | None = None, cos_ref: float = 0.75) -> None:
        from sentence_transformers import SentenceTransformer
        self._m = SentenceTransformer(model, device=device)
        self.dim = self._m.get_sentence_embedding_dimension()
        self.name = model
        self.cos_ref = cos_ref

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self._m.encode(texts, normalize_embeddings=True, convert_to_numpy=True), dtype=np.float32)


def minilm_embedder(device: str | None = None) -> SentenceTransformerEmbedder:
    """paraphrase-multilingual-MiniLM-L12-v2: 384-dim, Hindi/English in one space. Same-story cosine is ~0.6."""
    return SentenceTransformerEmbedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", device, cos_ref=0.60)
