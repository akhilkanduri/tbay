"""Tutorial 12: the CLI — operating tbay from a terminal.

Everything an operator does day-to-day goes through the `tbay` command,
pointed at the SAME database your agents use (--db-url or $TBAY_DB_URL):

  tbay log         the audit log, newest first
  tbay pending     everything waiting for a human, oldest first
  tbay approve     / tbay reject --reason "..."
  tbay show        every stored detail of one execution
  tbay stats       counts by status/tool + active pauses
  tbay pause       / tbay resume        (the kill switch)
  tbay export      the audit log as JSON Lines
  tbay policies    every effective policy and its key settings
  tbay clear       wipe executions/approvals (asks first)

This script seeds a database with the Python API, then drives the real
CLI against it via subprocess, printing each command and its output —
copy any of these lines straight into your own terminal.

Run it:  python examples/tutorial/12_cli_tour.py
"""
import subprocess
import sys

from _tutorial_helpers import banner, fresh_client, step

from tbay import guarded

banner("12: the CLI")

# ---------------------------------------------------------------------------
# Seed: one succeeded call, one failed call, one waiting for approval.
# ---------------------------------------------------------------------------
client = fresh_client()
db_url = f"sqlite:///{client.backend._path}"


@guarded(client, policy="readonly")
def lookup(q: str) -> dict:
    return {"answer": q}


@guarded(client, policy="mutating")
def flaky(job: str) -> dict:
    raise RuntimeError("upstream 500")


lookup("hello world")
try:
    flaky("job-1")
except RuntimeError:
    pass

# Park one call in WAITING_APPROVAL by hand (tutorial 04 shows the real
# flow; here we just need a pending row for the CLI to act on).
acq = client.backend.acquire_or_get(
    execution_id="demo-pending-1", tool_name="refund", idempotency_key="k1", tenant="",
    policy_name="destructive", args_hash="h", args_json='{"customer": "c42", "amount": 500.0}',
    max_retries=0, retry_backoff=0.0, reasoning="customer reported damaged item",
    agent_id="billing-agent-7",
)
client.backend.mark_waiting_approval(acq.record.id)


def cli(*args):
    """Run `tbay <args>` against our database and echo everything."""
    cmd = ["tbay", "--db-url", db_url, *args]
    print(f"\n$ {' '.join(cmd[:1] + ['--db-url', 'sqlite:///...', *args])}")
    result = subprocess.run(
        [sys.executable, "-m", "tbay.cli", "--db-url", db_url, *args],
        capture_output=True, text=True, timeout=30,
    )
    output = (result.stdout + result.stderr).rstrip()
    print("\n".join(f"  {line}" for line in output.splitlines()))
    assert result.returncode == 0, output
    return output


step("1. tbay log — what has tbay seen?")
out = cli("log", "--limit", "10")
assert "lookup" in out and "FAILED" in out

step("2. tbay pending — what needs a human? (args + agent + reasoning on one line)")
out = cli("pending")
assert "refund" in out and "billing-agent-7" in out and "damaged item" in out

step("3. tbay approve — greenlight it (add TBAY_APPROVAL_SECRET to sign)")
out = cli("approve", "demo-pending-1", "--resolver", "akhil@example.com")
assert "approved demo-pending-1" in out

step("4. tbay show — the full record of any execution")
out = cli("show", "demo-pending-1")
assert "WAITING_APPROVAL" in out and "approval:" in out

step("5. tbay stats — the fleet at a glance")
out = cli("stats")
assert "SUCCEEDED" in out

step("6. tbay pause / resume — the kill switch from a terminal")
out = cli("pause", "--tool", "refund", "--reason", "investigating chargebacks")
assert "paused refund" in out
out = cli("stats")
assert "PAUSED" in out and "investigating chargebacks" in out
out = cli("resume", "--tool", "refund")
assert "resumed refund" in out

step("7. tbay export — the audit log as JSON Lines (jq/warehouse-ready)")
out = cli("export", "--limit", "5")
assert '"tool_name"' in out

step("8. tbay policies — every effective tier and its key settings")
out = cli("policies")
assert "readonly" in out and "destructive" in out

print("""
WHAT JUST HAPPENED
  - The CLI is a thin veneer over the same backend your code uses, so it
    sees and controls the same executions — from any machine that can
    reach the database.
  - `tbay reject <id> --reason "..."` (not shown) works like approve;
    the blocked agent's ApprovalRejected error carries the reason.
  - Set TBAY_DB_URL and TBAY_APPROVAL_SECRET in your shell profile so
    `tbay pending` / `tbay approve` are always one keystroke away.

NEXT: 13_integrations.py — dropping all of this under the framework you
already use.
""")
