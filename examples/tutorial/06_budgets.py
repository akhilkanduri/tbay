"""Tutorial 06: budgets — metering magnitude, not call counts.

A rate limit answers "how OFTEN may this tool run?". It cannot answer
"how MUCH may this tool do?": ten perfectly-paced calls that each refund
$9,999 sail straight through any rate limit. A budget caps the SUM of a
numeric argument across a rolling window:

    budget:
      arg: amount     # which argument to meter
      max: 100        # its total may not pass this...
      per: 1d         # ...within this rolling window

The sum is computed in the shared database, so the cap holds across
every process and host at once, however the amounts are sliced.

Run it:  python examples/tutorial/06_budgets.py
"""
import time

from _tutorial_helpers import banner, fresh_client, step

from tbay import BudgetExceeded, guarded
from tbay.policy import Policy

banner("06: budgets")
client = fresh_client()

# In YAML this is `budget: {arg: amount, max: 100, per: 1d}`; here we
# build the Policy in code, with a short window so the tutorial can show
# rollover without waiting a day.
client.policies["spend"] = Policy(
    name="spend",
    idempotent=False, singleflight=False,     # each refund is its own execution
    budget_arg="amount",
    budget_max=100.0,
    budget_window=1.0,                        # 1 second = tutorial-sized "1d"
)

executed = []


@guarded(client, policy="spend")
def refund(customer_id: str, amount: float) -> dict:
    executed.append(amount)
    return {"refunded": amount}


# ---------------------------------------------------------------------------
# Step 1: spend up to the cap. The cap is INCLUSIVE: a call landing
# exactly on max still runs; the first call PAST it is refused.
# ---------------------------------------------------------------------------
step("1. Spending 60 + 40 = exactly the 100 cap: both run")
refund("c1", 60.0)
refund("c2", 40.0)
print(f"    executed amounts: {executed}")

step("2. Even a 0.50 refund is now over budget: BudgetExceeded, tool never runs")
try:
    refund("c3", 0.5)
    raise AssertionError("should have been blocked")
except BudgetExceeded as exc:
    print(f"    BudgetExceeded: {exc}")
assert executed == [60.0, 40.0]

# The refusal is also on the record: the blocked call's execution row is
# marked FAILED with the budget error, so the audit log shows the denial.
denied = [r for r in client.backend.list_executions(tool_name="refund", status="FAILED")]
print(f"    audit log shows the denial: {denied[0].error[:60]}...")

# ---------------------------------------------------------------------------
# Step 3: the window ROLLS. Old spend ages out continuously; there is no
# midnight reset to game.
# ---------------------------------------------------------------------------
step("3. After the window passes, spend ages out and refunds flow again")
time.sleep(1.1)
print(f"    {refund('c4', 80.0)}")
assert executed == [60.0, 40.0, 80.0]

# ---------------------------------------------------------------------------
# Step 4: unmeterable calls are refused. If the metered argument is
# missing or not numeric, tbay cannot count the call — and an uncounted
# call would be unmetered spend. Safe default: refuse.
# ---------------------------------------------------------------------------
step("4. A call whose 'amount' is missing or non-numeric is refused outright")
for bad_kwargs in [{}, {"amount": "lots"}]:
    try:
        refund("c5", **bad_kwargs)
        raise AssertionError("unmeterable call must not run")
    except (BudgetExceeded, TypeError) as exc:
        print(f"    {bad_kwargs!r:20} -> {type(exc).__name__}: {str(exc)[:70]}...")

# ---------------------------------------------------------------------------
# Step 5: budgets compose with everything else. The production pattern
# for money-moving tools stacks three layers:
#
#   refunds:
#     approval_bypass_arg: amount     # small ones: automatic
#     approval_bypass_max: 50
#     approval_required: true         # big ones: a human decides each
#     budget: {arg: amount, max: 1000, per: 1d}   # and NEVER >1000/day total
#
# Layer 1 keeps humans out of trivia, layer 2 puts them on big calls,
# layer 3 bounds the worst case even if every single call looked fine.
# ---------------------------------------------------------------------------
step("5. (pattern) approval bypass + approval + budget stack — see docs/controls.md")
print("    small -> auto, large -> human, total -> hard cap. Three independent nets.")

print("""
WHAT JUST HAPPENED
  - budget.arg/max/per cap the SUM of an argument per tool per tenant,
    inclusively, over a rolling window, across all processes.
  - Cache hits spend nothing (nothing ran); unmeterable calls are refused;
    denials are audit-logged AND emitted as `limit.budget` events (tut. 09).

NEXT: 07_redaction.py — keeping secrets out of that audit log.
""")
