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


def test_agent_identity_and_metadata_over_postgres(pg_client):
    import json

    from tbay import agent

    tool_name = f"pg_agent_{uuid.uuid4().hex}"

    @guarded(pg_client, policy="mutating", tool_name=tool_name)
    def act(x: int) -> dict:
        return {"x": x}

    with agent("billing-agent-7", model="gpt-5", team="payments"):
        act(1)

    record = pg_client.backend.list_executions(tool_name=tool_name)[0]
    assert record.agent_id == "billing-agent-7"
    assert json.loads(record.agent_meta) == {"model": "gpt-5", "team": "payments"}


def test_rejection_reason_over_postgres(pg_client):
    import pytest as _pytest

    from tbay import ApprovalRejected

    tool_name = f"pg_reject_{uuid.uuid4().hex}"
    pg_client.policies["destructive"].approval_timeout = 10.0

    @guarded(pg_client, policy="destructive", tool_name=tool_name)
    def dangerous(x: int) -> dict:
        return {"ran": x}

    outcome = {}

    def call():
        try:
            outcome["result"] = dangerous(1)
        except Exception as exc:
            outcome["error"] = exc

    t = threading.Thread(target=call, daemon=True)
    t.start()
    deadline = time.time() + 5
    execution_id = None
    while time.time() < deadline and execution_id is None:
        waiting = pg_client.backend.list_executions(tool_name=tool_name, status="WAITING_APPROVAL")
        if waiting:
            execution_id = waiting[0].id
        else:
            time.sleep(0.02)
    assert execution_id, "never reached WAITING_APPROVAL"

    pg_client.backend.resolve_approval(execution_id, approved=False, resolver="op", note="budget exceeded")
    t.join(timeout=5)
    assert isinstance(outcome.get("error"), ApprovalRejected)
    assert "budget exceeded" in str(outcome["error"])
