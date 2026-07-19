"""Stale-lease recovery: a RUNNING execution whose owner crashed is reclaimed
by the next caller once it is older than the policy's lease_timeout."""
import time

import pytest

from tbay import guarded
from tbay.exceptions import ExecutionTimeout
from tbay.policy import Policy


def simulate_crashed_owner(client, tool_name="fetch", key="k1", policy_name="leased"):
    """Insert a RUNNING execution directly, as if a process acquired it and
    then died before completing or failing it."""
    acq = client.backend.acquire_or_get(
        execution_id="crashed-owner",
        tool_name=tool_name,
        idempotency_key=key,
        tenant="",
        policy_name=policy_name,
        args_hash="h",
        args_json="{}",
        max_retries=0,
        retry_backoff=0.0,
    )
    assert acq.owner
    return acq.record


def test_stale_running_row_is_reclaimed(client):
    client.policies["leased"] = Policy(name="leased", lease_timeout=0.1, cache_ttl=None)
    simulate_crashed_owner(client)
    time.sleep(0.15)  # let the lease expire

    calls = []

    @guarded(client, policy="leased", key_fn=lambda: "k1", tool_name="fetch")
    def fetch() -> dict:
        calls.append(1)
        return {"ok": True}

    assert fetch() == {"ok": True}  # reclaimed and executed, instead of hanging
    assert calls == [1]


def test_fresh_running_row_is_not_reclaimed(client):
    """Within the lease the second caller must follow, not steal: stealing a
    live execution would double-run it."""
    client.policies["leased"] = Policy(name="leased", lease_timeout=30.0, cache_ttl=None)
    simulate_crashed_owner(client)

    calls = []
    client.poll_interval = 0.02

    @guarded(client, policy="leased", key_fn=lambda: "k1", tool_name="fetch")
    def fetch() -> dict:
        calls.append(1)
        return {"ok": True}

    # The owner is "alive" as far as tbay can tell, so this caller waits for
    # its result; our fake owner never finishes, so waiting times out.
    client.backend.wait_for_result = lambda *a, **k: (_ for _ in ()).throw(
        ExecutionTimeout("still running")
    )
    with pytest.raises(ExecutionTimeout):
        fetch()
    assert calls == []


def test_no_lease_timeout_means_no_reclaim(client):
    client.policies["unleased"] = Policy(name="unleased", lease_timeout=None, cache_ttl=None)
    simulate_crashed_owner(client, policy_name="unleased")
    time.sleep(0.05)

    acq = client.backend.acquire_or_get(
        execution_id="second-caller",
        tool_name="fetch",
        idempotency_key="k1",
        tenant="",
        policy_name="unleased",
        args_hash="h",
        args_json="{}",
        max_retries=0,
        retry_backoff=0.0,
        lease_timeout=None,
    )
    assert acq.follow_running and not acq.owner


def test_exactly_one_contender_wins_the_reclaim(client):
    """The created_at CAS: many callers see the same stale row, one becomes
    owner, the rest follow."""
    simulate_crashed_owner(client)
    time.sleep(0.15)

    outcomes = []
    for i in range(5):
        acq = client.backend.acquire_or_get(
            execution_id=f"contender-{i}",
            tool_name="fetch",
            idempotency_key="k1",
            tenant="",
            policy_name="leased",
            args_hash="h",
            args_json="{}",
            max_retries=0,
            retry_backoff=0.0,
            lease_timeout=0.1,
        )
        outcomes.append("owner" if acq.owner else "follow")
    assert outcomes.count("owner") == 1


def test_lease_recovery_pg(pg_client):
    tool = f"leased_fetch_{int(time.time() * 1000)}"
    simulate_crashed_owner(pg_client, tool_name=tool, key="k1")
    time.sleep(0.15)
    acq = pg_client.backend.acquire_or_get(
        execution_id="second-caller",
        tool_name=tool,
        idempotency_key="k1",
        tenant="",
        policy_name="leased",
        args_hash="h",
        args_json="{}",
        max_retries=0,
        retry_backoff=0.0,
        lease_timeout=0.1,
    )
    assert acq.owner


def test_lease_recovery_redis(redis_client):
    simulate_crashed_owner(redis_client, tool_name="fetch", key="k1")
    time.sleep(0.15)
    acq = redis_client.backend.acquire_or_get(
        execution_id="second-caller",
        tool_name="fetch",
        idempotency_key="k1",
        tenant="",
        policy_name="leased",
        args_hash="h",
        args_json="{}",
        max_retries=0,
        retry_backoff=0.0,
        lease_timeout=0.1,
    )
    assert acq.owner
