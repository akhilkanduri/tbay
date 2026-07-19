"""Tutorial 04: human approval — pausing a tool call until someone says yes.

For genuinely risky actions (refunds, deploys, deletions, email blasts)
the right amount of automation is "prepare everything, then wait for a
human". A policy with approval_required: true makes @guarded do exactly
that:

  agent calls tool -> execution parks as WAITING_APPROVAL (function has
  NOT run) -> a human runs `tbay approve <id>` or `tbay reject <id>`
  (or clicks the dashboard, or your Slack bot writes the decision) ->
  the blocked call resumes and runs, or raises ApprovalRejected.

In this script the "human" is a background thread so everything is
self-contained; in real life it's the CLI/dashboard/webhook receiver.

Run it:  python examples/tutorial/04_approvals.py
"""
import threading
import time

from _tutorial_helpers import banner, fresh_client, step

from tbay import ApprovalRejected, guarded, sign_approval
from tbay.policy import Policy

banner("04: approvals")
client = fresh_client()

client.policies["refunds"] = Policy(
    name="refunds",
    approval_required=True,
    approval_timeout=10.0,          # tutorial-sized; 1h is a sane production value
    approval_bypass_arg="amount",   # small refunds skip the human entirely...
    approval_bypass_max=50,         # ...when amount <= 50
)

executed = []


@guarded(client, policy="refunds")
def refund(customer_id: str, amount: float) -> dict:
    executed.append((customer_id, amount))
    return {"refunded": customer_id, "amount": amount}


def human(decision: bool, note=None, signature_secret=None):
    """A pretend human: waits for something to hit WAITING_APPROVAL, then
    decides. This is literally what `tbay approve`/`tbay reject` do."""
    def act():
        while True:
            waiting = client.backend.list_executions(status="WAITING_APPROVAL", limit=1)
            if waiting:
                execution_id = waiting[0].id
                print(f"    [human] sees {execution_id[:8]} args={waiting[0].args_json} -> "
                      f"{'APPROVE' if decision else 'REJECT'}")
                sig = sign_approval(signature_secret, execution_id, decision) if signature_secret else None
                client.backend.resolve_approval(execution_id, approved=decision,
                                                resolver="tutorial-human", signature=sig, note=note)
                return
            time.sleep(0.02)
    t = threading.Thread(target=act, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Step 1: the bypass. amount <= 50 runs immediately; no human involved.
# ---------------------------------------------------------------------------
step("1. Bypass: a $20 refund is below approval_bypass_max, runs instantly")
print(f"    {refund('cust_1', 20.0)}")
assert executed == [("cust_1", 20.0)]

# ---------------------------------------------------------------------------
# Step 2: approval. A $500 refund parks; the call BLOCKS until the human
# decides; then it runs and returns normally. The caller's code doesn't
# change at all — the wait is inside the guarded call.
# ---------------------------------------------------------------------------
step("2. A $500 refund parks as WAITING_APPROVAL; human approves; call resumes")
human(decision=True)
print(f"    {refund('cust_2', 500.0)}")
assert ("cust_2", 500.0) in executed

# ---------------------------------------------------------------------------
# Step 3: rejection, with a reason. The function NEVER runs; the caller
# gets ApprovalRejected carrying the human's stated reason, so the agent
# can learn why and adapt instead of blindly retrying.
# ---------------------------------------------------------------------------
step("3. Rejection with a reason: the tool never runs, the agent learns why")
human(decision=False, note="over the daily refund allowance")
try:
    refund("cust_3", 900.0)
    raise AssertionError("should have been rejected")
except ApprovalRejected as exc:
    print(f"    ApprovalRejected: {exc}")
assert not any(c == "cust_3" for c, _ in executed)

# ---------------------------------------------------------------------------
# Step 4: SIGNED approvals. Problem: by default an approval is just a
# database row, so database credentials == approval authority. Configure
# an approval secret and the executing client verifies an HMAC signature
# over (execution_id, decision) BEFORE running. A row flipped to
# "approved" straight in the database fails verification.
# ---------------------------------------------------------------------------
step("4a. With a secret, a signed approval works normally")
client.approval_secret = "demo-secret"          # normally TBAY_APPROVAL_SECRET
human(decision=True, signature_secret="demo-secret")
print(f"    {refund('cust_4', 300.0)}")
assert ("cust_4", 300.0) in executed

step("4b. Tamper demo: an UNSIGNED 'approved' row is refused")
human(decision=True, signature_secret=None)     # attacker with DB creds, no secret
try:
    refund("cust_5", 300.0)
    raise AssertionError("unsigned approval must not execute")
except ApprovalRejected as exc:
    print(f"    ApprovalRejected: {str(exc)[:90]}...")
assert not any(c == "cust_5" for c, _ in executed)

# ---------------------------------------------------------------------------
# Step 5: what the audit log shows afterwards — including the rejection
# reason on the failed rows.
# ---------------------------------------------------------------------------
step("5. The paper trail")
for r in client.backend.list_executions(tool_name="refund"):
    print(f"    {r.id[:8]}  {r.status:9s}  args={r.args_json}  error={r.error}")

print("""
WHAT JUST HAPPENED
  - approval_required parks the call BEFORE the function runs; that
    ordering is the entire guarantee.
  - Bypass thresholds keep small stuff automatic; signing splits "can
    write the database" from "may authorize an action".
  - Also available (not shown here, needs a URL): approval_webhook fires
    a signed HTTP POST with the full redacted context when a call parks
    — see docs/approvals.md#webhooks-in-detail.

NEXT: 05_kill_switch.py — when you need to stop EVERYTHING, now.
""")
