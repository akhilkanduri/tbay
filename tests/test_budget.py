"""Budget caps: the SUM of a metered argument over a rolling window may not
pass budget_max, whatever the size of each individual call."""
import time

import pytest

from tbay import BudgetExceeded, guarded
from tbay.events import BUDGET_EXCEEDED
from tbay.policy import Policy


def budget_policy(client, *, budget_max=100.0, window=60.0, name="budgeted"):
    client.policies[name] = Policy(
        name=name,
        idempotent=False,  # each refund is its own execution
        singleflight=False,
        budget_arg="amount",
        budget_max=budget_max,
        budget_window=window,
    )
    return name


def make_refund(client, policy):
    calls = []

    @guarded(client, policy=policy)
    def refund(customer_id: str, amount: float) -> dict:
        calls.append(amount)
        return {"refunded": amount}

    return refund, calls


def test_budget_allows_up_to_the_cap_then_blocks(client):
    policy = budget_policy(client, budget_max=100.0)
    refund, calls = make_refund(client, policy)

    assert refund("c1", 60.0) == {"refunded": 60.0}
    assert refund("c2", 40.0) == {"refunded": 40.0}  # exactly on budget: still allowed
    with pytest.raises(BudgetExceeded) as exc:
        refund("c3", 0.5)
    assert "amount" in str(exc.value)
    assert calls == [60.0, 40.0]  # the blocked call never ran


def test_budget_emits_event_and_marks_failed(client):
    seen = []
    client.on(lambda e: seen.append(e), events=[BUDGET_EXCEEDED])
    policy = budget_policy(client, budget_max=10.0)
    refund, _ = make_refund(client, policy)
    refund("c1", 10.0)
    with pytest.raises(BudgetExceeded):
        refund("c2", 1.0)
    assert seen and seen[0].data["budget_max"] == 10.0
    failed = client.backend.list_executions(status="FAILED")
    assert any("budget" in (r.error or "") for r in failed)


def test_budget_window_rolls_over(client):
    policy = budget_policy(client, budget_max=10.0, window=0.2)
    refund, _ = make_refund(client, policy)
    refund("c1", 10.0)
    with pytest.raises(BudgetExceeded):
        refund("c2", 1.0)
    time.sleep(0.25)  # the earlier spend ages out of the window
    assert refund("c3", 8.0) == {"refunded": 8.0}


def test_unmeterable_call_is_refused(client):
    """A budgeted tool called without a numeric metered arg must not run:
    letting it through would mean unmetered spend."""
    policy = budget_policy(client)
    calls = []

    @guarded(client, policy=policy)
    def refund(customer_id: str, amount=None) -> dict:
        calls.append(amount)
        return {"refunded": amount}

    with pytest.raises(BudgetExceeded):
        refund("c1")  # amount missing
    with pytest.raises(BudgetExceeded):
        refund("c1", amount="lots")  # amount not numeric
    assert calls == []


def test_budget_on_idempotent_policy_ignores_cache_hits(client):
    """Serving a cached result spends nothing: only real executions count."""
    name = "budgeted_idem"
    client.policies[name] = Policy(
        name=name,
        cache_ttl=60.0,
        budget_arg="amount",
        budget_max=50.0,
        budget_window=60.0,
    )
    refund, calls = make_refund(client, name)
    assert refund("c1", 50.0) == {"refunded": 50.0}
    assert refund("c1", 50.0) == {"refunded": 50.0}  # identical call: cache hit, no new spend
    assert calls == [50.0]
    with pytest.raises(BudgetExceeded):
        refund("c2", 1.0)  # a NEW call past the cap still blocks


def test_budget_pg(pg_client):
    policy = budget_policy(pg_client, budget_max=25.0, name=f"budgeted-{time.time()}")
    tool_name = f"pg_refund_{int(time.time() * 1000)}"

    @guarded(pg_client, policy=policy, tool_name=tool_name)
    def refund(customer_id: str, amount: float) -> dict:
        return {"refunded": amount}

    assert refund("c1", 25.0) == {"refunded": 25.0}
    with pytest.raises(BudgetExceeded):
        refund("c2", 1.0)


def test_budget_redis(redis_client):
    policy = budget_policy(redis_client, budget_max=25.0)

    @guarded(redis_client, policy=policy)
    def refund(customer_id: str, amount: float) -> dict:
        return {"refunded": amount}

    assert refund("c1", 20.0) == {"refunded": 20.0}
    assert refund("c2", 5.0) == {"refunded": 5.0}
    with pytest.raises(BudgetExceeded):
        refund("c3", 0.01)
