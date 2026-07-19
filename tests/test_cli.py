"""The tbay CLI: pending, show, stats, pause/resume, export, policies."""
import json

import pytest
from click.testing import CliRunner

from tbay import TbayClient, guarded
from tbay.cli import main
from tbay.exceptions import ToolPaused


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'cli.sqlite'}"


@pytest.fixture
def seeded(db_url):
    client = TbayClient(db_url, poll_interval=0.02)

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    lookup("hello")
    return client


def run_cli(db_url, *args):
    return CliRunner().invoke(main, ["--db-url", db_url, *args])


def test_stats_counts_and_log(db_url, seeded):
    result = run_cli(db_url, "stats")
    assert result.exit_code == 0, result.output
    assert "SUCCEEDED" in result.output
    assert "lookup" in result.output

    result = run_cli(db_url, "log")
    assert "lookup" in result.output


def test_show_full_record(db_url, seeded):
    record = seeded.backend.list_executions(limit=1)[0]
    result = run_cli(db_url, "show", record.id)
    assert result.exit_code == 0, result.output
    assert record.id in result.output
    assert "SUCCEEDED" in result.output

    result = run_cli(db_url, "show", "nonexistent")
    assert result.exit_code != 0


def test_pause_resume_roundtrip(db_url, seeded):
    result = run_cli(db_url, "pause", "--reason", "incident")
    assert "paused ALL tools" in result.output

    result = run_cli(db_url, "stats")
    assert "PAUSED" in result.output and "incident" in result.output

    @guarded(seeded, policy="readonly")
    def blocked(q: str) -> dict:
        return {}

    with pytest.raises(ToolPaused):
        blocked("x")

    result = run_cli(db_url, "resume")
    assert "resumed ALL tools" in result.output
    assert seeded.paused() == {}


def test_pause_single_tool(db_url, seeded):
    run_cli(db_url, "pause", "--tool", "lookup", "--reason", "flaky upstream")
    assert "lookup" in seeded.paused()
    run_cli(db_url, "resume", "--tool", "lookup")
    assert seeded.paused() == {}


def test_pending_lists_waiting_approvals(db_url):
    client = TbayClient(db_url, poll_interval=0.02)
    acq = client.backend.acquire_or_get(
        execution_id="e1", tool_name="refund", idempotency_key="k", tenant="",
        policy_name="destructive", args_hash="h", args_json='{"amount": 500}',
        max_retries=0, retry_backoff=0.0, reasoning="customer 42 asked",
    )
    client.backend.mark_waiting_approval(acq.record.id)

    result = run_cli(db_url, "pending")
    assert result.exit_code == 0, result.output
    assert "refund" in result.output
    assert "customer 42 asked" in result.output

    result = run_cli(db_url, "approve", "e1")
    assert "approved e1" in result.output
    assert client.backend.get_approval_status("e1") == "approved"


def test_export_jsonl(db_url, seeded):
    result = run_cli(db_url, "export")
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["tool_name"] == "lookup"
    assert row["status"] == "SUCCEEDED"


def test_policies_listing(db_url, tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "policies:\n"
        "  spendy:\n"
        "    budget: {arg: amount, max: 1000, per: 1d}\n"
        "    approval_required: true\n"
    )
    result = CliRunner().invoke(main, ["--db-url", db_url, "--policy-file", str(policy_file), "policies"])
    assert result.exit_code == 0, result.output
    assert "spendy" in result.output
    assert "budget[amount]" in result.output
    assert "readonly" in result.output  # defaults still listed


def test_policy_typo_is_rejected(db_url, tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("policies:\n  risky:\n    aproval_required: true\n")
    result = CliRunner().invoke(main, ["--db-url", db_url, "--policy-file", str(policy_file), "policies"])
    assert result.exit_code != 0
    assert "aproval_required" in str(result.exception)
