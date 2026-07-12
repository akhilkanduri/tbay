"""Embedders turn a call's arguments into a vector so the semantic cache can
ask "have I already answered something close enough to this?" instead of
requiring byte-identical arguments.

Any object with an `embed(text) -> list[float]` method works, so you can plug
in a real embedding model in a few lines:

    class OpenAIEmbedder:
        def __init__(self, client):
            self.client = client

        def embed(self, text):
            out = self.client.embeddings.create(model="text-embedding-3-small", input=text)
            return out.data[0].embedding

    client = TbayClient(db_url, embedder=OpenAIEmbedder(openai_client))
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import List

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    """The zero-dependency default: hash each token of the args text into a
    fixed-size term-frequency vector, normalized to unit length.

    This catches calls whose arguments use the same words in a different
    order or shape ({"query": "weather berlin today"} vs
    {"query": "today weather berlin"}). It does NOT understand meaning:
    "weather in berlin" and "berlin forecast" share almost no tokens and
    won't match. For true paraphrase matching, pass a real embedding model
    to TbayClient(embedder=...) as shown in the module docstring."""

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dims
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.md5(token.encode()).hexdigest()  # stable across processes, unlike hash()
            vector[int(digest, 16) % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm:
            vector = [v / norm for v in vector]
        return vector


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Standard cosine similarity, safe against zero vectors and vectors an
    external embedding model didn't normalize."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
