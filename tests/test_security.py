"""Agent identity and signed approvals.

Signed approvals separate "can write the database" from "may authorize a
destructive action": with an approval secret configured on the executing
client, flipping the approvals row via raw database access is not enough,
because the client verifies an HMAC signature before running the function.
"""
import threading
import time

import pytest

from tbay import ApprovalRejected, TbayClient, agent, guarded, sign_approval


# -- agent identity ------------------------------------------------------------


def test_agent_context_recorded(client):
    @guarded(client, policy="mutating")
    def act(x: int) -> dict:
        return {"x": x}

    with agent("billing-agent-7"):
        act(1)

    records = client.backend.list_executions(tool_name="act")
    assert records[0].agent_id == "billing-agent-7"


def test_client_level_agent_id_and_context_override(tmp_path):
    client = TbayClient(f"sqlite:///{tmp_path}/tbay.sqlite", poll_interval=0.02, agent_id="proc-agent")

    @guarded(client, policy="mutating")
    def act(x: int) -> dict:
        return {"x": x}

    act(1)  # falls back to the client-level identity
    with agent("turn-agent"):  # a surrounding block wins over the client default
        act(2)

    by_args = {r.args_json: r.agent_id for r in client.backend.list_executions(tool_name="act")}
    assert by_args['{"x": 1}'] == "proc-agent"
    assert by_args['{"x": 2}'] == "turn-agent"


def test_no_agent_is_none(client):
    @guarded(client, policy="mutating")
    def act(x: int) -> dict:
        return {"x": x}

    act(1)
    assert client.backend.list_executions(tool_name="act")[0].agent_id is None


# -- signed approvals ----------------------------------------------------------

SECRET = "test-approval-secret"


def _secured_client(tmp_path):
    client = TbayClient(f"sqlite:///{tmp_path}/tbay.sqlite", poll_interval=0.02, approval_secret=SECRET)
    client.policies["destructive"].approval_timeout = 10.0
    return client


def _start_blocked_call(client, outcome):
    @guarded(client, policy="destructive")
    def dangerous(x: int) -> dict:
        return {"ran": x}

    def call():
        try:
            outcome["result"] = dangerous(1)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        waiting = client.backend.list_executions(status="WAITING_APPROVAL")
        if waiting:
            return thread, waiting[0].id
        time.sleep(0.02)
    pytest.fail("call never reached WAITING_APPROVAL")


def test_unsigned_approval_is_refused_when_secret_configured(tmp_path):
    client = _secured_client(tmp_path)
    outcome = {}
    thread, execution_id = _start_blocked_call(client, outcome)

    # what an attacker with only database credentials can do: flip the row
    client.backend.resolve_approval(execution_id, approved=True, resolver="db-attacker")
    thread.join(timeout=5)

    assert "result" not in outcome, "the function ran despite an unsigned approval"
    assert isinstance(outcome.get("error"), ApprovalRejected)
    assert "signature" in str(outcome["error"])


def test_wrong_secret_signature_is_refused(tmp_path):
    client = _secured_client(tmp_path)
    outcome = {}
    thread, execution_id = _start_blocked_call(client, outcome)

    forged = sign_approval("not-the-real-secret", execution_id, True)
    client.backend.resolve_approval(execution_id, approved=True, resolver="forger", signature=forged)
    thread.join(timeout=5)

    assert "result" not in outcome
    assert isinstance(outcome.get("error"), ApprovalRejected)


def test_correctly_signed_approval_runs(tmp_path):
    client = _secured_client(tmp_path)
    outcome = {}
    thread, execution_id = _start_blocked_call(client, outcome)

    signature = sign_approval(SECRET, execution_id, True)
    client.backend.resolve_approval(execution_id, approved=True, resolver="operator", signature=signature)
    thread.join(timeout=5)

    assert outcome.get("result") == {"ran": 1}


def test_unsigned_approval_still_works_without_secret(client):
    # Backward compatible: no secret configured means the pre-signing behavior.
    client.policies["destructive"].approval_timeout = 10.0
    outcome = {}
    thread, execution_id = _start_blocked_call(client, outcome)

    client.backend.resolve_approval(execution_id, approved=True, resolver="cli")
    thread.join(timeout=5)

    assert outcome.get("result") == {"ran": 1}
