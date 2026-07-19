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
