import asyncio
import threading
import time

import pytest

from tbay import ApprovalRejected, ExecutionFailed, guarded
from tbay.policy import parse_ttl


def test_idempotency_exact_match(client):
    calls = []

    @guarded(client, policy="mutating")
    def create_ticket(title: str) -> dict:
        calls.append(title)
        return {"title": title, "id": len(calls)}

    first = create_ticket("fix flaky test")
    second = create_ticket("fix flaky test")

    assert first == second
    assert calls == ["fix flaky test"]  # real function ran exactly once


def test_different_args_execute_independently(client):
    calls = []

    @guarded(client, policy="mutating")
    def create_ticket(title: str) -> dict:
        calls.append(title)
        return {"title": title}

    create_ticket("a")
    create_ticket("b")

    assert calls == ["a", "b"]


def test_readonly_cache_expires(client):
    calls = []

    @guarded(client, policy="readonly", key_fn=lambda q: q)
    def search(q: str) -> dict:
        calls.append(q)
        return {"q": q, "n": len(calls)}

    client.policies["readonly"].cache_ttl = 0.05

    search("x")
    search("x")
    assert len(calls) == 1  # second call is a cache hit

    time.sleep(0.15)
    search("x")
    assert len(calls) == 2  # cache expired, re-executed


def test_singleflight_dedupes_concurrent_identical_calls(client):
    calls = []

    @guarded(client, policy="mutating", key_fn=lambda: "same-key")
    def slow_call():
        calls.append(1)
        time.sleep(0.3)
        return {"done": True}

    threads = [threading.Thread(target=slow_call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(calls) == 1  # exactly one real execution among 20 concurrent callers


def test_destructive_policy_blocks_until_approved(client):
    @guarded(client, policy="destructive", key_fn=lambda cid, amt: f"{cid}:{amt}")
    def refund(cid: str, amt: float) -> dict:
        return {"cid": cid, "amt": amt, "status": "refunded"}

    result_holder = {}
    t = threading.Thread(target=lambda: result_holder.update(result=refund("cust1", 10.0)))
    t.start()

    execution_id = _wait_for_status(client, "refund", "WAITING_APPROVAL")
    client.backend.resolve_approval(execution_id, approved=True, resolver="test")
    t.join(timeout=5)

    assert result_holder["result"] == {"cid": "cust1", "amt": 10.0, "status": "refunded"}


def test_destructive_policy_rejection_raises(client):
    @guarded(client, policy="destructive", key_fn=lambda cid, amt: f"reject:{cid}:{amt}")
    def refund(cid: str, amt: float) -> dict:
        return {"cid": cid, "amt": amt}

    error_holder = {}

    def call():
        try:
            refund("cust2", 5.0)
        except ApprovalRejected as exc:
            error_holder["error"] = exc

    t = threading.Thread(target=call)
    t.start()

    execution_id = _wait_for_status(client, "refund", "WAITING_APPROVAL")
    client.backend.resolve_approval(execution_id, approved=False, resolver="test")
    t.join(timeout=5)

    assert "error" in error_holder


def test_failed_execution_reraises_without_retry(client):
    attempts = []

    @guarded(client, policy="mutating", key_fn=lambda: "always-fails")
    def boom():
        attempts.append(1)
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        boom()

    # mutating policy has retry=False -> the stored error is re-raised, no second attempt
    with pytest.raises(ExecutionFailed):
        boom()

    assert len(attempts) == 1


def test_readonly_retries_after_failure(client):
    attempts = []

    client.policies["readonly"].retry_backoff = 0.0  # skip the default backoff so the test runs fast

    @guarded(client, policy="readonly", key_fn=lambda: "flaky")
    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("first attempt fails")
        return {"ok": True}

    with pytest.raises(ValueError):
        flaky()

    # readonly's default max_retries=2 -> the second call re-attempts instead of raising stored error
    assert flaky() == {"ok": True}
    assert len(attempts) == 2


def test_async_guarded_idempotency(client):
    calls = []

    @guarded(client, policy="mutating")
    async def async_create(title: str) -> dict:
        calls.append(title)
        return {"title": title}

    async def run_twice():
        first = await async_create("async ticket")
        second = await async_create("async ticket")
        return first, second

    first, second = asyncio.run(run_twice())
    assert first == second
    assert calls == ["async ticket"]


def test_parse_ttl():
    assert parse_ttl("5m") == 300.0
    assert parse_ttl("30s") == 30.0
    assert parse_ttl("1h") == 3600.0
    assert parse_ttl(0) is None
    assert parse_ttl(None) is None
    assert parse_ttl(120) == 120.0


def _wait_for_status(client, tool_name, status, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for record in client.backend.list_executions(tool_name=tool_name, limit=10):
            if record.status == status:
                return record.id
        time.sleep(0.02)
    raise AssertionError(f"no execution for {tool_name!r} reached status {status!r} within {timeout}s")


def test_clear_wipes_everything(client):
    from tbay import guarded

    @guarded(client, policy="mutating")
    def act(x: int) -> dict:
        return {"x": x}

    act(1)
    act(2)
    assert len(client.backend.list_executions()) == 2
    removed = client.backend.clear()
    assert removed == 2
    assert client.backend.list_executions() == []
    # the same key runs fresh after a clear, since its history is gone
    act(1)
    assert len(client.backend.list_executions()) == 1
