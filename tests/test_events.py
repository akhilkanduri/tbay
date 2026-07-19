"""Lifecycle events: emitted at every decision point, isolated from the
guarded call, filterable by type."""
import pytest

from tbay import guarded
from tbay.events import (
    CACHE_HIT,
    CALL_FAILED,
    CALL_STARTED,
    CALL_SUCCEEDED,
    RATE_LIMITED,
    SEMANTIC_CACHE_HIT,
)
from tbay.exceptions import RateLimitExceeded
from tbay.policy import Policy


def test_started_succeeded_and_cache_hit(client):
    events = []
    client.on(lambda e: events.append(e))

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    lookup("x")
    lookup("x")  # cache hit
    types = [e.type for e in events]
    assert types == [CALL_STARTED, CALL_SUCCEEDED, CACHE_HIT]
    assert events[1].data["duration_s"] >= 0
    assert events[0].tool_name == "lookup"
    assert events[0].execution_id == events[1].execution_id == events[2].execution_id


def test_failure_event_carries_error(client):
    events = []
    client.on(lambda e: events.append(e), events=[CALL_FAILED])

    @guarded(client, policy="mutating")
    def boom() -> dict:
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError):
        boom()
    assert len(events) == 1
    assert events[0].data["error"] == "kaput"
    assert events[0].data["replayed"] is False

    # calling again replays the stored failure -> another CALL_FAILED, marked replayed
    from tbay.exceptions import ExecutionFailed

    with pytest.raises(ExecutionFailed):
        boom()
    assert events[1].data["replayed"] is True


def test_filtering_and_decorator_form(client):
    hits, everything = [], []

    @client.on(events=[CACHE_HIT])
    def only_hits(e):
        hits.append(e)

    @client.on
    def all_events(e):
        everything.append(e)

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    lookup("x")
    lookup("x")
    assert [e.type for e in hits] == [CACHE_HIT]
    assert len(everything) == 3


def test_handler_exceptions_never_break_the_call(client):
    @client.on
    def bad_handler(e):
        raise ValueError("observability should never take the workload down")

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    assert lookup("x") == {"answer": "x"}  # still works


def test_unknown_event_type_is_rejected_early(client):
    with pytest.raises(ValueError):
        client.on(lambda e: None, events=["cache.hitt"])  # typo caught at subscribe time


def test_rate_limit_event(client):
    events = []
    client.on(lambda e: events.append(e), events=[RATE_LIMITED])
    client.policies["limited"] = Policy(
        name="limited", idempotent=False, singleflight=False,
        rate_limit_max_calls=1, rate_limit_window=60.0,
    )

    @guarded(client, policy="limited")
    def ping() -> dict:
        return {}

    ping()
    with pytest.raises(RateLimitExceeded):
        ping()
    assert len(events) == 1 and events[0].data["count"] == 2


def test_semantic_hit_event(client):
    events = []
    client.on(lambda e: events.append(e), events=[SEMANTIC_CACHE_HIT])
    client.policies["sem"] = Policy(name="sem", cache_ttl=60.0, semantic_cache=True, semantic_threshold=0.9)

    @guarded(client, policy="sem")
    def search(query: str) -> dict:
        return {"result": query}

    search(query="weather berlin today")
    search(query="today weather berlin")  # same tokens, different order
    assert [e.type for e in events] == [SEMANTIC_CACHE_HIT]


def test_agent_and_reasoning_flow_into_events(client):
    import tbay

    events = []
    client.on(lambda e: events.append(e), events=[CALL_STARTED])

    @guarded(client, policy="mutating")
    def act(x: int) -> dict:
        return {"x": x}

    with tbay.agent("agent-7"), tbay.reasoning("because the user asked"):
        act(1)
    assert events[0].agent_id == "agent-7"
    assert events[0].reasoning == "because the user asked"
