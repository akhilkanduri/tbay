# Integrations

`@guarded` wraps a plain callable and never inspects the caller's
framework, so it composes with anything that accepts a Python function as
a tool. The pattern is always the same: **guard the function first, then
hand the guarded function to the framework.** The framework plans; tbay
decides whether execution actually happens.

## LangChain

```python
from langchain_core.tools import tool
from tbay import TbayClient, guarded

client = TbayClient("postgresql://postgres:tbay@localhost:5432/tbay")

@tool
@guarded(client, policy="readonly")
def github_search(query: str) -> str:
    """Search GitHub repositories."""
    return real_github_search(query)
```

`@tool` sees an ordinary function; tbay sits underneath it. Decorator
order matters: `@guarded` goes closest to the function.

## OpenAI Agents SDK

```python
from agents import function_tool
from tbay import guarded

@function_tool
@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    """Refund a customer."""
    return stripe_refund(customer_id, amount)
```

## CrewAI

```python
from crewai.tools import tool

@tool("Search the web")
@guarded(client, policy="readonly")
def web_search(query: str) -> str:
    return search_impl(query)
```

## MCP servers

If you expose tools over the Model Context Protocol (e.g. with FastMCP),
guard the handler the same way, and every MCP client that connects to your
server, whatever model or app it is, inherits the safety layer:

```python
from mcp.server.fastmcp import FastMCP
from tbay import TbayClient, guarded

mcp = FastMCP("payments")
client = TbayClient("postgresql://postgres:tbay@localhost:5432/tbay")

@mcp.tool()
@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    """Refund a customer (pauses for human approval over $50)."""
    return stripe_refund(customer_id, amount)
```

This is the interesting deployment for MCP: the *server* is the one place
every caller has to pass through, so it's the right place for idempotency,
budgets, and approval gating, regardless of which agent is on the other
end of the connection.

## Wrapping many tools at once

When a whole set of tools shares a risk tier, `guard_tools` saves the
boilerplate:

```python
from tbay import guard_tools

safe_tools = guard_tools(client, [search, fetch_page, get_weather], policy="readonly")
actions = guard_tools(client, {"refund": refund, "cancel": cancel_order}, policy="destructive")
```

A dict input returns a dict under the same keys (each key becomes the
recorded `tool_name`); an iterable returns a list in order. Tools that
need different policies should keep using `@guarded` individually.

## Attaching agent identity and reasoning

Every framework has somewhere per-turn code runs; wrap it so the audit
log knows who asked and why:

```python
import tbay

with tbay.agent("support-agent", model="gpt-5", team="cx"), \
     tbay.reasoning(plan.justification):
    result = framework.execute_step(step)
```

Both are contextvar-based, so concurrent async agents never mix their
identities up. See [Observability](observability.md).

## Wiring events into your stack

Everything tbay decides is also emitted as an in-process event, so
plugging its decisions into your existing telemetry is a few lines:

```python
@client.on(events=["limit.budget", "killswitch.blocked", "call.failed"])
def to_slack(event):
    post_to_slack(f"tbay: {event.type} on {event.tool_name} ({event.data})")
```

Or, for OpenTelemetry users, `tbay.otel.instrument(client)` turns every
guarded call into a span that nests under your framework's existing
traces. See [Observability](observability.md#lifecycle-events).
