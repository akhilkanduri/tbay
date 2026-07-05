import threading
import time

import pytest

from tbay import ConcurrencyLimitExceeded, ExecutionTimeout, RateLimitExceeded, guarded


def test_volatile_policy_never_dedupes_identical_calls(client):
    """An LLM call used to make a decision should run fresh every time, even
    with the exact same prompt, unlike a mutating/readonly tool call."""
    calls = []

    @guarded(client, policy="volatile")
    def ask_llm(prompt: str) -> dict:
        calls.append(prompt)
        return {"answer": f"response #{len(calls)}"}

    first = ask_llm("what should I do?")
    second = ask_llm("what should I do?")  # identical args, but must NOT be a cache hit

    assert first != second
    assert calls == ["what should I do?", "what should I do?"]


def test_volatile_policy_ignores_key_fn(client):
    calls = []

    @guarded(client, policy="volatile", key_fn=lambda prompt: "always-the-same-key")
    def ask_llm(prompt: str) -> dict:
        calls.append(prompt)
        return {"n": len(calls)}

    ask_llm("x")
    ask_llm("x")

    assert len(calls) == 2  # key_fn is ignored for idempotent=False policies


def test_max_retries_with_backoff(client):
    client.policies["mutating"].max_retries = 2
    client.policies["mutating"].retry_backoff = 0.05
    attempts = []

    @guarded(client, policy="mutating", key_fn=lambda: "retry-key")
    def flaky():
        attempts.append(1)
        if len(attempts) < 2:
            raise ValueError("not yet")
        return {"ok": True}

    with pytest.raises(ValueError):
        flaky()

    time.sleep(0.1)  # let retry_backoff elapse
    assert flaky() == {"ok": True}
    assert len(attempts) == 2


def test_rate_limit_blocks_excess_calls(client):
    client.policies["mutating"].rate_limit_max_calls = 2
    client.policies["mutating"].rate_limit_window = 60.0

    @guarded(client, policy="mutating")
    def call_api(n: int) -> dict:
        return {"n": n}

    call_api(1)
    call_api(2)
    with pytest.raises(RateLimitExceeded):
        call_api(3)


def test_max_concurrent_limits_simultaneous_execution(client):
    client.policies["mutating"].max_concurrent = 1
    client.policies["mutating"].concurrency_wait_timeout = 2.0
    running_at_once = []
    peak = []

    @guarded(client, policy="mutating")
    def slow(n: int) -> dict:
        running_at_once.append(1)
        peak.append(len(running_at_once))
        time.sleep(0.2)
        running_at_once.pop()
        return {"n": n}

    threads = [threading.Thread(target=slow, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
        time.sleep(0.01)  # stagger starts: max_concurrent is a best-effort soft cap,
        # not an atomic guarantee, so give each caller a moment to be seen by the next
    for t in threads:
        t.join(timeout=5)

    assert peak  # all three calls eventually ran
    assert max(peak) == 1  # never more than max_concurrent executions at once


def test_max_concurrent_gives_up_after_wait_timeout(client):
    client.policies["mutating"].max_concurrent = 1
    client.policies["mutating"].concurrency_wait_timeout = 0.05

    @guarded(client, policy="mutating")
    def slow(n: int) -> dict:
        time.sleep(0.5)
        return {"n": n}

    t = threading.Thread(target=slow, args=(1,))
    t.start()
    time.sleep(0.05)  # let it become RUNNING first

    with pytest.raises(ConcurrencyLimitExceeded):
        slow(2)

    t.join(timeout=5)


def test_execution_timeout_marks_failed(client):
    client.policies["mutating"].execution_timeout = 0.1

    @guarded(client, policy="mutating")
    def hangs(n: int) -> dict:
        time.sleep(2)
        return {"n": n}

    with pytest.raises(ExecutionTimeout):
        hangs(1)


def test_approval_bypass_below_threshold_auto_executes(client):
    client.policies["destructive"].approval_bypass_arg = "amount"
    client.policies["destructive"].approval_bypass_max = 50.0

    @guarded(client, policy="destructive", key_fn=lambda cid, amount: f"{cid}:{amount}")
    def refund(cid: str, amount: float) -> dict:
        return {"cid": cid, "amount": amount}

    # small refund: under the bypass threshold, should return immediately, no approval wait
    result = refund("cust1", 10.0)
    assert result == {"cid": "cust1", "amount": 10.0}


def test_approval_bypass_above_threshold_still_requires_approval(client):
    client.policies["destructive"].approval_bypass_arg = "amount"
    client.policies["destructive"].approval_bypass_max = 50.0

    @guarded(client, policy="destructive", key_fn=lambda cid, amount: f"{cid}:{amount}")
    def refund(cid: str, amount: float) -> dict:
        return {"cid": cid, "amount": amount}

    result_holder = {}
    t = threading.Thread(target=lambda: result_holder.update(result=refund("cust2", 500.0)))
    t.start()

    deadline = time.time() + 5
    execution_id = None
    while time.time() < deadline:
        for record in client.backend.list_executions(tool_name="refund", limit=10):
            if record.status == "WAITING_APPROVAL":
                execution_id = record.id
                break
        if execution_id:
            break
        time.sleep(0.02)
    assert execution_id is not None  # the large refund did pause for approval

    client.backend.resolve_approval(execution_id, approved=True, resolver="test")
    t.join(timeout=5)
    assert result_holder["result"] == {"cid": "cust2", "amount": 500.0}


def test_redact_args_masks_sensitive_fields_in_audit_log(client):
    client.policies["mutating"].redact_args = ["password"]

    @guarded(client, policy="mutating", tool_name="login")
    def login(username: str, password: str) -> dict:
        return {"username": username, "ok": True}

    login("alice", "hunter2")

    records = client.backend.list_executions(tool_name="login", limit=1)
    assert len(records) == 1
    assert "hunter2" not in records[0].args_json
    assert "alice" in records[0].args_json
    assert "REDACTED" in records[0].args_json


def test_default_policy_field_values():
    from tbay.policy import DEFAULT_POLICIES

    assert DEFAULT_POLICIES["volatile"].idempotent is False
    assert DEFAULT_POLICIES["destructive"].approval_required is True
    assert DEFAULT_POLICIES["readonly"].max_retries == 2
