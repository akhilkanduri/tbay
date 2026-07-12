"""The SQLite backend gets full coverage in test_tbay.py and
test_policy_features.py. These tests re-check the behaviors that are
backend-specific enough to be worth verifying against a real Redis: the same
idempotency/volatile/concurrency/caching semantics, implemented with Lua
scripts instead of SQL transactions.

Skipped unless TBAY_TEST_REDIS_URL is set (see conftest.py's redis_client
fixture).
"""
import threading
import time
import uuid

import pytest

from tbay import ExecutionFailed, guarded


def test_idempotency_over_redis(redis_client):
    calls = []
    tool_name = f"redis_idempotency_{uuid.uuid4().hex}"

    @guarded(redis_client, policy="mutating", tool_name=tool_name)
    def create(title: str) -> dict:
        calls.append(title)
        return {"title": title}

    assert create("x") == create("x")
    assert calls == ["x"]


def test_cache_ttl_expires_over_redis(redis_client):
    calls = []
    tool_name = f"redis_ttl_{uuid.uuid4().hex}"
    redis_client.policies["readonly"].cache_ttl = 0.05

    @guarded(redis_client, policy="readonly", tool_name=tool_name)
    def lookup(q: str) -> dict:
        calls.append(q)
        return {"q": q, "n": len(calls)}

    lookup("a")
    lookup("a")  # within TTL: served from cache
    time.sleep(0.1)
    lookup("a")  # TTL elapsed: the stale row is reclaimed and re-run

    assert len(calls) == 2


def test_volatile_never_dedupes_over_redis(redis_client):
    calls = []
    tool_name = f"redis_volatile_{uuid.uuid4().hex}"

    @guarded(redis_client, policy="volatile", tool_name=tool_name)
    def ask(prompt: str) -> dict:
        calls.append(prompt)
        return {"n": len(calls)}

    assert ask("same prompt") != ask("same prompt")
    assert len(calls) == 2


def test_failure_is_stored_and_replayed_over_redis(redis_client):
    tool_name = f"redis_failure_{uuid.uuid4().hex}"

    @guarded(redis_client, policy="mutating", tool_name=tool_name)
    def flaky(x: int) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        flaky(1)
    # mutating has max_retries=0, so the second call replays the stored error
    with pytest.raises(ExecutionFailed):
        flaky(1)


def test_retry_reclaims_failed_row_over_redis(redis_client):
    calls = []
    tool_name = f"redis_retry_{uuid.uuid4().hex}"
    redis_client.policies["mutating"].max_retries = 1

    @guarded(redis_client, policy="mutating", tool_name=tool_name)
    def flaky(x: int) -> dict:
        calls.append(x)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": x}

    with pytest.raises(RuntimeError):
        flaky(1)
    assert flaky(1) == {"ok": 1}
    assert len(calls) == 2


def test_max_concurrent_is_atomic_over_redis(redis_client):
    tool_name = f"redis_concurrency_{uuid.uuid4().hex}"
    redis_client.policies["mutating"].max_concurrent = 1
    redis_client.policies["mutating"].concurrency_wait_timeout = 5.0
    peak = []
    running = []

    @guarded(redis_client, policy="mutating", tool_name=tool_name)
    def slow(n: int) -> dict:
        running.append(1)
        peak.append(len(running))
        time.sleep(0.2)
        running.pop()
        return {"n": n}

    threads = [threading.Thread(target=slow, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
        time.sleep(0.01)
    for t in threads:
        t.join(timeout=10)

    assert peak
    assert max(peak) == 1


def test_semantic_cache_over_redis(redis_client):
    calls = []
    tool_name = f"redis_semantic_{uuid.uuid4().hex}"
    redis_client.policies["readonly"].semantic_cache = True

    @guarded(redis_client, policy="readonly", tool_name=tool_name)
    def search(query: str) -> dict:
        calls.append(query)
        return {"answer": query}

    first = search("weather in berlin today")
    second = search("today weather in berlin")  # same tokens reordered: semantic hit

    assert second == first
    assert len(calls) == 1


def test_reasoning_recorded_over_redis(redis_client):
    from tbay import reasoning

    tool_name = f"redis_reasoning_{uuid.uuid4().hex}"

    @guarded(redis_client, policy="mutating", tool_name=tool_name)
    def act(x: int) -> dict:
        return {"x": x}

    with reasoning("verifying the redis audit trail"):
        act(1)

    records = redis_client.backend.list_executions(tool_name=tool_name)
    assert records[0].reasoning == "verifying the redis audit trail"
