"""Tutorial 10: agent identity and reasoning traces — who asked, and why.

When a human reviews a paused refund, or does a post-incident review of
last night's tool calls, the two questions are always the same: WHICH
agent asked for this, and WHY did it think this was a good idea? tbay
records both on every execution:

  with agent("billing-agent-7", model="gpt-5", team="payments"):
      with reasoning("customer 42 reported item damaged in transit"):
          refund_customer("cust_42", 30.0)

Both are contextvars: blocks nest (innermost wins), concurrent async
tasks are fully isolated from each other, and there is zero threading of
extra parameters through your tool signatures.

Run it:  python examples/tutorial/10_agents_and_reasoning.py
"""
import asyncio

from _tutorial_helpers import banner, fresh_client, step

import tbay
from tbay import guarded

banner("10: agent identity and reasoning")
client = fresh_client()


@guarded(client, policy="mutating")
def create_ticket(title: str) -> dict:
    return {"ticket": title}


@guarded(client, policy="mutating")
async def send_alert(channel: str, agent_no: int) -> dict:
    await asyncio.sleep(0.05)   # interleave the two agents on purpose
    return {"alerted": channel}


# ---------------------------------------------------------------------------
# Step 1: the blocks. Everything called inside carries the context.
# ---------------------------------------------------------------------------
step("1. Wrap calls in agent() and reasoning() blocks")
with tbay.agent("support-agent-3", model="gpt-5", team="cx", version="1.4"):
    with tbay.reasoning("user reported checkout is broken on mobile"):
        create_ticket("checkout broken on mobile")

record = client.backend.list_executions(tool_name="create_ticket", limit=1)[0]
print(f"    stored agent_id   = {record.agent_id}")
print(f"    stored agent_meta = {record.agent_meta}")
print(f"    stored reasoning  = {record.reasoning!r}")
assert record.agent_id == "support-agent-3"
assert "cx" in record.agent_meta   # the metadata keywords are stored as JSON
assert record.reasoning == "user reported checkout is broken on mobile"

# ---------------------------------------------------------------------------
# Step 2: nesting — the innermost block wins, and pops back on exit.
# ---------------------------------------------------------------------------
step("2. Blocks nest; innermost wins")
with tbay.reasoning("outer: routine maintenance sweep"):
    with tbay.reasoning("inner: disk 87% full on db-2"):
        create_ticket("expand db-2 volume")
    create_ticket("schedule next sweep")

# list_executions is newest-first: the outer-block call came last
newest, older = client.backend.list_executions(tool_name="create_ticket", limit=2)
print(f"    'schedule next sweep'  reasoning={newest.reasoning!r}")
print(f"    'expand db-2 volume'   reasoning={older.reasoning!r}")
assert newest.reasoning == "outer: routine maintenance sweep"
assert older.reasoning == "inner: disk 87% full on db-2"

# ---------------------------------------------------------------------------
# Step 3: concurrent async agents DON'T bleed into each other. Two agents
# run interleaved on one event loop; each call is attributed to the agent
# whose block it ran in. This is why contextvars, not globals.
# ---------------------------------------------------------------------------
step("3. Two interleaved async agents keep separate identities")


async def agent_turn(n: int):
    with tbay.agent(f"agent-{n}", model="gpt-5"):
        with tbay.reasoning(f"agent {n}'s own reason"):
            await send_alert(f"#alerts-{n}", n)


async def main():
    await asyncio.gather(agent_turn(1), agent_turn(2))


asyncio.run(main())
for r in client.backend.list_executions(tool_name="send_alert"):
    print(f"    {r.args_json}  <- agent={r.agent_id} reasoning={r.reasoning!r}")
    n = r.agent_id.split("-")[1]
    assert f"#alerts-{n}" in r.args_json and f"agent {n}" in r.reasoning

# ---------------------------------------------------------------------------
# Step 4: process-level defaults, for when the whole process IS one agent.
# Precedence: agent() block > TbayClient(agent_id=...) > $TBAY_AGENT_ID.
# ---------------------------------------------------------------------------
step("4. Client-level default identity (agent() blocks override it)")
defaulted = fresh_client(agent_id="batch-worker", agent_meta={"deploy": "blue"})


@guarded(defaulted, policy="mutating")
def nightly_job(name: str) -> dict:
    return {"ran": name}


nightly_job("cleanup")
r = defaulted.backend.list_executions(tool_name="nightly_job", limit=1)[0]
print(f"    agent_id={r.agent_id} meta={r.agent_meta} (no block needed)")
assert r.agent_id == "batch-worker"

print("""
WHAT JUST HAPPENED
  - agent()/reasoning() stamp WHO and WHY onto every execution, with
    context-local isolation that survives async interleaving.
  - The same attribution rides on every event (tutorial 09) and shows up
    in `tbay log`, `tbay pending`, and the dashboard — an approver sees
    who's asking and their stated justification before saying yes.

NEXT: 11_observability_otel.py — the same story, as OpenTelemetry spans.
""")
