"""Recursive, pattern-based, and automatic redaction of audit-log arguments."""
import json

from tbay import guarded
from tbay.policy import Policy
from tbay.redaction import MASK, redact_structure


def test_bare_name_masks_at_any_depth():
    data = {"token": "t0p", "nested": {"token": "s3cret", "keep": 1}, "items": [{"token": "x"}]}
    out = redact_structure(data, fields=["token"])
    assert out == {"token": MASK, "nested": {"token": MASK, "keep": 1}, "items": [{"token": MASK}]}


def test_dotted_path_masks_only_that_path():
    data = {"card": {"number": "4111", "brand": "visa"}, "number": "keep-me"}
    out = redact_structure(data, fields=["card.number"])
    assert out["card"]["number"] == MASK
    assert out["card"]["brand"] == "visa"
    assert out["number"] == "keep-me"


def test_paths_see_through_lists():
    data = {"cards": [{"number": "4111"}, {"number": "5500"}]}
    out = redact_structure(data, fields=["cards.number"])
    assert [c["number"] for c in out["cards"]] == [MASK, MASK]


def test_patterns_match_key_names():
    data = {"internal_ref": "x", "public_ref": "y"}
    out = redact_structure(data, patterns=[r"^internal_"])
    assert out == {"internal_ref": MASK, "public_ref": "y"}


def test_auto_masks_secret_looking_keys_only_when_enabled():
    data = {"api_key": "k", "password": "p", "author": "fine", "query": "fine"}
    assert redact_structure(data) == data  # off by default: nothing changes
    out = redact_structure(data, auto=True)
    assert out == {"api_key": MASK, "password": MASK, "author": "fine", "query": "fine"}


def test_original_is_never_mutated():
    data = {"nested": {"token": "s3cret"}}
    redact_structure(data, fields=["token"])
    assert data["nested"]["token"] == "s3cret"


def test_policy_wires_redaction_into_the_audit_log(client):
    client.policies["careful"] = Policy(
        name="careful", idempotent=False, singleflight=False,
        redact_args=["card.number"], redact_auto=True,
    )

    @guarded(client, policy="careful")
    def charge(card: dict, api_key: str, amount: float) -> dict:
        return {"charged": amount}

    charge({"number": "4111-1111", "brand": "visa"}, api_key="sk_live_xyz", amount=5.0)
    record = client.backend.list_executions(tool_name="charge", limit=1)[0]
    stored = json.loads(record.args_json)
    assert stored["card"]["number"] == MASK
    assert stored["card"]["brand"] == "visa"
    assert stored["api_key"] == MASK
    assert stored["amount"] == 5.0
