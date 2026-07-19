"""Tutorial 05: the kill switch — stop everything, now, everywhere.

An agent is stuck in a loop. A prompt injection is driving weird tool
calls. A bad deploy is refunding the wrong customers. You do not want to
find and restart every worker; you want ONE command that makes every
guarded call, in every process on every host, stop dead:

    $ tbay pause --reason "agent runaway, investigating"

The pause is a row in the shared database. Every TbayClient checks it
BEFORE doing anything else, so blocked calls raise ToolPaused
immediately — no acquiring, no waiting, and definitely no executing.

Run it:  python examples/tutorial/05_kill_switch.py
"""
from _tutorial_helpers import banner, fresh_client, step

from tbay import TbayClient, ToolPaused, guarded

banner("05: the kill switch")
client = fresh_client()

sent = []


@guarded(client, policy="mutating")
def send_email(to: str) -> dict:
    sent.append(to)
    return {"sent": to}


@guarded(client, policy="readonly")
def lookup(q: str) -> dict:
    return {"answer": q}


# ---------------------------------------------------------------------------
# Step 1: global pause. Everything stops; the reason travels to callers.
# ---------------------------------------------------------------------------
step("1. Global pause: every guarded call raises ToolPaused instantly")
client.pause(reason="incident 4711: agent runaway", by="oncall@example.com")
for tool, arg in [(send_email, "a@example.com"), (lookup, "anything")]:
    try:
        tool(arg)
    except ToolPaused as exc:
        print(f"    {tool.__name__}: ToolPaused: {exc}")
assert sent == []

# ---------------------------------------------------------------------------
# Step 2: the pause is CROSS-PROCESS. A second client (imagine: another
# worker, another host) on the same database is equally stopped. That's
# the point — `tbay pause` in a terminal stops your whole fleet.
# We prove it the other way around: a second client LIFTS the pause and
# the first client immediately unblocks.
# ---------------------------------------------------------------------------
step("2. A different client on the same database lifts the pause")
other_process = TbayClient(f"sqlite:///{client.backend._path}", poll_interval=0.02)
print(f"    other client sees paused() = {other_process.paused()}")
other_process.resume()
print(f"    after other client's resume(): {send_email('a@example.com')}")
assert sent == ["a@example.com"]

# ---------------------------------------------------------------------------
# Step 3: per-tool pause. Scope the brake to the misbehaving tool and
# leave the rest of the agent working.
# ---------------------------------------------------------------------------
step("3. Per-tool pause: only send_email is stopped")
client.pause("send_email", reason="spamming customers")
try:
    send_email("b@example.com")
except ToolPaused as exc:
    print(f"    send_email blocked: {exc}")
print(f"    lookup still works: {lookup('still fine')}")
assert sent == ["a@example.com"]

step("4. paused() lists active pauses (this backs `tbay stats`)")
print(f"    {client.paused()}")
assert "send_email" in client.paused()
client.resume("send_email")

# ---------------------------------------------------------------------------
# Step 5: pauses survive `tbay clear`. Wiping executions to reset a demo
# must not silently release an emergency brake someone pulled.
# ---------------------------------------------------------------------------
step("5. A pause survives clearing all executions")
client.pause(reason="hold everything")
removed = client.backend.clear()
print(f"    cleared {removed} executions; paused() still = {client.paused()}")
assert "*" in client.paused()
client.resume()

print("""
WHAT JUST HAPPENED
  - pause()/resume() write a control row every client checks first;
    scope is global or per-tool; reasons reach the blocked caller.
  - CLI equivalents: tbay pause [--tool X] [--reason ...] / tbay resume.
  - Combine with events (tutorial 09) to build automatic circuit
    breakers: a handler that sees repeated failures can call
    client.pause() itself.
  - Trust model and details: docs/controls.md.

NEXT: 06_budgets.py — capping how MUCH an agent can do, not just how often.
""")
