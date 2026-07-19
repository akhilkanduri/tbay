"""Tutorial 13: integrations — tbay under any framework (or none).

The design rule that makes every integration trivial: @guarded wraps a
PLAIN CALLABLE and never inspects the caller. Frameworks see an ordinary
function; tbay sits underneath. So the recipe is always:

    guard the function first, THEN hand it to the framework.

    @tool                                   # LangChain / CrewAI / FastMCP / ...
    @guarded(client, policy="destructive")  # closest to the function
    def refund_customer(...): ...

The framework plans; tbay decides whether execution actually happens.
This script shows every integration surface that doesn't need external
packages: guard_tools for bulk wrapping, client.run for functions you
don't own, async tools, and tenants. (Framework-specific snippets that
DO need packages installed live in docs/integrations.md.)

Run it:  python examples/tutorial/13_integrations.py
"""
import asyncio

from _tutorial_helpers import banner, fresh_client, step

from tbay import guard_tools, guarded

banner("13: integrations")
client = fresh_client()

# ---------------------------------------------------------------------------
# 1. guard_tools: a whole toolbox under one policy in one call. A list
# returns a list (each tool keeps its function name); a dict returns a
# dict wrapped under the same keys, and each KEY becomes the recorded
# tool_name — handy when your registry names differ from function names.
# ---------------------------------------------------------------------------
def search(q: str) -> dict:
    return {"hits": [q]}


def fetch_page(url: str) -> dict:
    return {"html": f"<page {url}>"}


def do_refund(customer_id: str, amount: float) -> dict:
    return {"refunded": amount}


step("1. guard_tools: bulk-wrap by risk tier")
readonly_tools = guard_tools(client, [search, fetch_page], policy="readonly")
actions = guard_tools(client, {"refund": do_refund}, policy="mutating")

print(f"    list in  -> list out:  {[t.__name__ for t in readonly_tools]}")
print(f"    dict in  -> dict out:  {list(actions)}")
readonly_tools[0]("agent safety")
readonly_tools[0]("agent safety")          # cache hit, of course
actions["refund"]("c1", 10.0)
record = client.backend.list_executions(tool_name="refund", limit=1)[0]
print(f"    dict key became tool_name: {record.tool_name!r} (function was 'do_refund')")
assert record.tool_name == "refund"

# ---------------------------------------------------------------------------
# 2. client.run: guarding a function you DON'T own (a third-party SDK
# method, something you can't decorate). Same engine as @guarded, called
# inline; tool_name is explicit since fn.__name__ may be meaningless.
# ---------------------------------------------------------------------------
step("2. client.run for functions you can't decorate")
import math

result = client.run(math.pow, policy="readonly", args=(2, 10), kwargs={}, tool_name="math_pow")
again = client.run(math.pow, policy="readonly", args=(2, 10), kwargs={}, tool_name="math_pow")
print(f"    math.pow(2, 10) guarded twice -> {result} (second was a cache hit)")
assert result == again == 1024.0

# ---------------------------------------------------------------------------
# 3. Async tools: decorate a coroutine and @guarded returns a coroutine;
# every wait inside (approvals, singleflight follows) awaits instead of
# blocking, so one event loop can host many guarded agents.
# ---------------------------------------------------------------------------
step("3. Async tools work identically")
async_runs = []


@guarded(client, policy="readonly")
async def fetch_profile(user_id: str) -> dict:
    async_runs.append(user_id)
    await asyncio.sleep(0.01)
    return {"user": user_id}


async def main():
    a = await fetch_profile("u1")
    b = await fetch_profile("u1")          # async cache hit
    return a, b


a, b = asyncio.run(main())
print(f"    two awaits, {len(async_runs)} execution(s), equal results: {a == b}")
assert len(async_runs) == 1

# ---------------------------------------------------------------------------
# 4. Tenants: one shared database, isolated dedup/cache/limit/budget
# spaces. Same tool + same args under different tenants = different keys.
# Use it for per-customer, per-environment, or per-agent-fleet isolation.
# ---------------------------------------------------------------------------
step("4. Tenants partition everything")
tenant_runs = []


def report(day: str) -> dict:
    tenant_runs.append(day)
    return {"day": day}


acme_report = guarded(client, policy="readonly", tenant="acme")(report)
globex_report = guarded(client, policy="readonly", tenant="globex")(report)

acme_report("2026-07-19")
globex_report("2026-07-19")    # same args, different tenant -> runs again
acme_report("2026-07-19")      # same tenant -> cache hit
print(f"    executions for 3 calls across 2 tenants: {len(tenant_runs)}")
assert len(tenant_runs) == 2

# ---------------------------------------------------------------------------
# 5. The framework recipes (need the packages; shown here as the exact
# shape, runnable snippets in docs/integrations.md):
#
#   LangChain:            @tool            over @guarded(...)
#   OpenAI Agents SDK:    @function_tool   over @guarded(...)
#   CrewAI:               @tool("name")    over @guarded(...)
#   MCP server (FastMCP): @mcp.tool()      over @guarded(...)
#
# The MCP case is the sleeper hit: guard the SERVER's handlers and every
# client that connects — whatever model, whatever app — inherits
# idempotency, budgets, approvals, and the kill switch. The server is
# the one chokepoint all callers share, which is exactly where an
# execution-safety layer belongs.
# ---------------------------------------------------------------------------
step("5. Framework recipes -> docs/integrations.md (decorator order is the only rule)")
print("    @framework_tool")
print("    @guarded(client, policy=...)   # <- always closest to the function")
print("    def my_tool(...): ...")

print("""
WHAT JUST HAPPENED
  - One wrapping rule covers every framework; guard_tools scales it to
    whole toolboxes; client.run covers undecoratable functions; async
    and tenants come for free.
  - You have now seen every feature in tbay. The reference docs (docs/)
    cover the same ground organized for lookup rather than learning.
""")
