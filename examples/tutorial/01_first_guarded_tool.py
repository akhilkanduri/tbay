"""Tutorial 01: your first guarded tool.

THE PROBLEM. An AI agent decides to call `send_invoice(order_42)`. Then,
because agents retry, because two workers picked up the same job, or
because the model simply emitted the same tool call twice, it decides to
call it *again*. Nothing in your agent framework stops the second call:
frameworks solve planning, not execution safety. tbay is the layer that
stands between "the agent chose a tool" and "the tool actually ran".

THE MECHANIC. You wrap the tool function with @guarded. Every call then
goes through TbayClient.run(), which:

  1. normalizes the arguments (so f(1, 2) and f(a=1, b=2) are the SAME call),
  2. derives an idempotency key = hash(tool name + args + tenant),
  3. atomically claims that key in the database — first caller wins,
  4. runs the real function ONLY if it won the claim,
  5. records everything (args, result/error, timing) in an audit log.

Run it:  python examples/tutorial/01_first_guarded_tool.py
"""
from _tutorial_helpers import banner, fresh_client, step

from tbay import guarded

banner("01: your first guarded tool")

# ---------------------------------------------------------------------------
# Step 1: a client. This is the only object you configure. No daemon, no
# server: state lives in the database the URL points at (SQLite here;
# postgresql:// or redis:// in production — nothing else changes).
# ---------------------------------------------------------------------------
client = fresh_client()   # = TbayClient("sqlite:///<tempdir>/tutorial.sqlite")

# ---------------------------------------------------------------------------
# Step 2: a tool. `calls` counts how often the REAL function body runs,
# which is the whole point of this tutorial: watching when it doesn't.
# ---------------------------------------------------------------------------
calls = []


@guarded(client, policy="mutating")   # "mutating": has an effect, must never double-run
def send_invoice(order_id: str, amount: float) -> dict:
    calls.append(order_id)
    print(f"    [the real function is executing for {order_id}]")
    return {"invoiced": order_id, "amount": amount}


# ---------------------------------------------------------------------------
# Step 3: call it twice with identical arguments.
# ---------------------------------------------------------------------------
step("First call: tbay claims the idempotency key, the function runs")
result1 = send_invoice("order_42", 99.0)
print(f"    returned {result1}")

step("Second identical call: the key is already claimed and SUCCEEDED")
result2 = send_invoice("order_42", 99.0)
print(f"    returned {result2}  (same result, function body did NOT run)")

assert calls == ["order_42"], "the function must have run exactly once"
assert result1 == result2

# ---------------------------------------------------------------------------
# Step 4: argument normalization. Positional vs keyword spelling of the
# SAME call produces the SAME idempotency key, because tbay binds the
# arguments against the function signature before hashing them.
# ---------------------------------------------------------------------------
step("Same call spelled with keywords: still deduplicated")
send_invoice(order_id="order_42", amount=99.0)
assert calls == ["order_42"], "keyword spelling is the same logical call"
print("    order_id='order_42', amount=99.0 hit the same key as ('order_42', 99.0)")

# ---------------------------------------------------------------------------
# Step 5: different arguments are a different call, of course.
# ---------------------------------------------------------------------------
step("A different order is a different key and runs for real")
send_invoice("order_43", 12.5)
assert calls == ["order_42", "order_43"]

# ---------------------------------------------------------------------------
# Step 6: the audit log. Every decision above is a queryable record:
# status, (redacted) arguments, result, timing. This is what `tbay log`,
# `tbay stats`, and the dashboard read. Note there are only TWO rows for
# our three "order_42" calls: cache hits don't create executions, they
# resolve against the winner's row.
# ---------------------------------------------------------------------------
step("What the audit log recorded")
for record in client.backend.list_executions(tool_name="send_invoice"):
    print(f"    {record.id[:8]}  {record.status:10s}  args={record.args_json}  result={record.result_json}")

records = client.backend.list_executions(tool_name="send_invoice")
assert len(records) == 2 and all(r.status == "SUCCEEDED" for r in records)

print("""
WHAT JUST HAPPENED
  - @guarded wrapped a plain function; no framework anywhere in sight.
  - The first call claimed (tool_name, hash(args), tenant) atomically in
    the database and ran. Identical calls resolved from the stored result.
  - This works ACROSS processes and machines: the claim is a database
    insert with a uniqueness constraint, not in-process memory.

NEXT: 02_policies.py — the "mutating" string above is a policy; policies
are where all behavior lives.
""")
