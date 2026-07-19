"""Duration strings must work on every timeout field, and the shipped
policy.example.yaml must actually load (it once didn't: approval_timeout
was parsed with float(), so '1h' crashed)."""
from pathlib import Path

from tbay.policy import load_policies

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_duration_strings_on_all_timeout_fields(tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "policies:\n"
        "  timed:\n"
        "    approval_required: true\n"
        "    approval_timeout: 1h\n"
        "    concurrency_wait_timeout: 30s\n"
        "    execution_timeout: 10s\n"
        "    lease_timeout: 10m\n"
        "    cache_ttl: 5m\n"
    )
    pol = load_policies(str(policy_file))["timed"]
    assert pol.approval_timeout == 3600.0
    assert pol.concurrency_wait_timeout == 30.0
    assert pol.execution_timeout == 10.0
    assert pol.lease_timeout == 600.0
    assert pol.cache_ttl == 300.0


def test_shipped_example_policy_file_loads():
    pols = load_policies(str(REPO_ROOT / "policy.example.yaml"))
    assert pols["destructive"].budget_max == 1000.0
    assert pols["destructive"].approval_timeout == 3600.0
    assert pols["readonly"].lease_timeout == 600.0


def _load_snippet(tmp_path, body: str):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("policies:\n  p:\n" + body)
    return load_policies(str(policy_file))


def test_half_configured_rate_limit_is_rejected(tmp_path):
    """rate_limit needs BOTH max_calls and per: half of one used to crash
    at call time with a bare TypeError instead of failing at load."""
    import pytest

    with pytest.raises(ValueError, match="rate_limit"):
        _load_snippet(tmp_path, "    rate_limit: {max_calls: 30}\n")
    with pytest.raises(ValueError, match="rate_limit"):
        _load_snippet(tmp_path, "    rate_limit: {per: 1m}\n")


def test_half_configured_approval_bypass_is_rejected(tmp_path):
    """approval_bypass_arg without approval_bypass_max used to silently
    require approval for every call: the configured bypass never fired."""
    import pytest

    with pytest.raises(ValueError, match="approval_bypass"):
        _load_snippet(tmp_path, "    approval_required: true\n    approval_bypass_arg: amount\n")
    with pytest.raises(ValueError, match="approval_bypass"):
        _load_snippet(tmp_path, "    approval_required: true\n    approval_bypass_max: 50\n")
    # both together is fine
    pols = _load_snippet(
        tmp_path, "    approval_required: true\n    approval_bypass_arg: amount\n    approval_bypass_max: 50\n"
    )
    assert pols["p"].approval_bypass_max == 50.0


def test_code_level_half_rate_limit_fails_loud(tmp_path):
    """Even when a Policy is built in code (bypassing YAML validation), a
    half-configured limit raises at call time instead of silently not
    limiting."""
    import pytest

    from tbay import TbayClient, guarded
    from tbay.policy import Policy

    client = TbayClient(f"sqlite:///{tmp_path / 'x.sqlite'}", poll_interval=0.02)
    client.policies["half"] = Policy(name="half", idempotent=False, singleflight=False,
                                     rate_limit_max_calls=3)

    @guarded(client, policy="half")
    def ping() -> dict:
        return {}

    with pytest.raises(ValueError, match="rate_limit_window"):
        ping()
