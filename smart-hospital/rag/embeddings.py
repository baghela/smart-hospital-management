"""Embedding backends.

``sentence-transformers`` is used when installed; otherwise a deterministic
hashing embedder keeps ingestion, retrieval and the test-suite fully runnable
offline. Both produce L2-normalised vectors so cosine similarity is a dot
product.
"""
from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import List, Sequence

from backend.core.config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def stable_hash(value: str) -> int:
    """Process-independent hash.

    ``hash()`` is randomised per interpreter run (PYTHONHASHSEED), so an index
    written in one process could not be queried from another. blake2b keeps the
    embedder genuinely deterministic.
    """
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class HashingEmbedder:
    """Dependency-free bag-of-ngrams embedder (deterministic, offline)."""

    name = "hashing-512"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        tokens = tokenize(text)
        grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for gram in grams:
            vector[stable_hash(f"{gram}:{self.dim}") % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # lazy import

        self.name = model_name
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> List[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


@lru_cache(maxsize=1)
def get_embedder():
    """Return the configured embedder, falling back to hashing when needed."""
    model_name = get_settings().embedding_model
    if model_name.startswith("hashing"):
        return HashingEmbedder()
    try:
        return SentenceTransformerEmbedder(model_name)
    except Exception:  # missing package / no model download
        return HashingEmbedder()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))

