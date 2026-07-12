"""Semantic caching: a stored result satisfies a new call whose arguments are
similar by embedding cosine, not byte-identical. These run against SQLite with
the default HashingEmbedder, which scores reordered-same-tokens text as
identical, giving the tests a deterministic "similar but not equal" case."""
import time

from tbay import guarded


def _enable_semantic(client, **overrides):
    pol = client.policies["readonly"]
    pol.semantic_cache = True
    for name, value in overrides.items():
        setattr(pol, name, value)
    return pol


def test_semantic_hit_skips_execution(client):
    _enable_semantic(client)
    calls = []

    @guarded(client, policy="readonly")
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": f"result for {query}"}

    first = search("weather in berlin today")
    # Same tokens, different order: a different idempotency key, but the
    # HashingEmbedder maps it to the same vector, so it's a semantic hit.
    second = search("today weather in berlin")

    assert second == first
    assert calls == ["weather in berlin today"]


def test_semantic_miss_runs_fresh(client):
    _enable_semantic(client)
    calls = []

    @guarded(client, policy="readonly")
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": query}

    search("weather in berlin")
    search("nvidia stock price")  # nothing in common: must execute

    assert calls == ["weather in berlin", "nvidia stock price"]


def test_semantic_respects_cache_ttl(client):
    _enable_semantic(client, cache_ttl=0.05)
    calls = []

    @guarded(client, policy="readonly")
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": query}

    search("weather in berlin today")
    time.sleep(0.1)  # let the cached result expire
    search("today weather in berlin")

    assert len(calls) == 2


def test_threshold_can_be_tightened_to_exact_only(client):
    # A threshold above 1.0 can never be reached, so every call misses and
    # falls through to the normal exact-match path.
    _enable_semantic(client, semantic_threshold=1.5)
    calls = []

    @guarded(client, policy="readonly")
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": query}

    search("weather in berlin today")
    search("today weather in berlin")

    assert len(calls) == 2


def test_custom_embedder_is_used(tmp_path):
    from tbay import TbayClient

    class ConstantEmbedder:
        """Maps every text to the same vector, so everything is a hit."""

        def embed(self, text):
            return [1.0, 0.0]

    client = TbayClient(f"sqlite:///{tmp_path}/tbay.sqlite", poll_interval=0.02, embedder=ConstantEmbedder())
    client.policies["readonly"].semantic_cache = True
    calls = []

    @guarded(client, policy="readonly")
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": query}

    first = search("anything at all")
    second = search("something completely different")

    assert second == first
    assert len(calls) == 1
