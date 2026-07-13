"""The SQLite backend gets full coverage in test_tbay.py and
test_policy_features.py. These tests re-check the handful of behaviors that
are backend-specific enough to be worth verifying against a real Postgres
database too: the same idempotency/volatile/concurrency semantics, backed by
psycopg2 and an advisory lock instead of sqlite3 and BEGIN IMMEDIATE.

Skipped unless TBAY_TEST_PG_DSN is set (see conftest.py's pg_client fixture).
"""
import threading
import time
import uuid

from tbay import guarded


def test_idempotency_over_postgres(pg_client):
    calls = []
    tool_name = f"pg_idempotency_{uuid.uuid4().hex}"

    @guarded(pg_client, policy="mutating", tool_name=tool_name)
    def create(title: str) -> dict:
        calls.append(title)
        return {"title": title}

    assert create("x") == create("x")
    assert calls == ["x"]


def test_volatile_never_dedupes_over_postgres(pg_client):
    calls = []
    tool_name = f"pg_volatile_{uuid.uuid4().hex}"

    @guarded(pg_client, policy="volatile", tool_name=tool_name)
    def ask(prompt: str) -> dict:
        calls.append(prompt)
        return {"n": len(calls)}

    assert ask("same prompt") != ask("same prompt")
    assert len(calls) == 2


def test_max_concurrent_is_atomic_over_postgres(pg_client):
    tool_name = f"pg_concurrency_{uuid.uuid4().hex}"
    pg_client.policies["mutating"].max_concurrent = 1
    pg_client.policies["mutating"].concurrency_wait_timeout = 5.0
    peak = []
    running = []

    @guarded(pg_client, policy="mutating", tool_name=tool_name)
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


def test_clear_wipes_everything_over_postgres(pg_client):
    tool_name = f"pg_clear_{uuid.uuid4().hex}"

    @guarded(pg_client, policy="mutating", tool_name=tool_name)
    def act(x: int) -> dict:
        return {"x": x}

    act(1)
    assert pg_client.backend.clear() >= 1
    assert pg_client.backend.list_executions() == []
