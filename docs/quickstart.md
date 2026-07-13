# Quickstart

## Install

```
pip install tbay               # SQLite backend, stdlib only
pip install tbay[postgres]     # + Postgres backend
pip install tbay[redis]        # + Redis backend

# or, with uv:
uv add tbay
uv add "tbay[postgres]"
uv add "tbay[redis]"
```

## Your first guarded tool

```python
from tbay import TbayClient, guarded

client = TbayClient("postgresql://postgres:tbay@localhost:5432/tbay")
# also works: "sqlite:///~/.tbay/db.sqlite" or "redis://localhost:6379/0"

@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    return real_github_api_call(query)

@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    return stripe_refund(customer_id, amount)
```

That's the whole integration. `github_search` now caches for 5 minutes and
collapses concurrent duplicate calls into one; `refund_customer` never
double-runs for the same input and pauses for a human before executing.

`@guarded` only ever wraps a plain callable, so it drops in under
LangChain's `@tool`, the OpenAI Agents SDK's `@function_tool`, CrewAI
tools, or bare functions, with zero framework-specific code:

```python
from langchain_core.tools import tool

@tool
@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    """Search GitHub repositories."""
    return real_github_api_call(query)
```

## Why this exists

Agent frameworks solve planning and orchestration. None of them solve
*execution safety*: once a tool is selected, nothing stops it from being
called twice, cached when it shouldn't be, called too often, or fired on a
destructive action without a human in the loop. tbay sits underneath any
framework and handles that, durably, across processes, in whatever
database you already run.

**This is not a hosted service.** You install it; state (idempotency keys,
cached results, the audit log) lives entirely in a database you own.
Nothing calls home.

## The dev container (recommended way to explore)

The repo ships a [dev container](https://containers.dev/) with Python 3.12
plus uv, a real Postgres, and a real Redis, all wired to the right
environment variables. Open the repo in VS Code and run "Dev Containers:
Reopen in Container", then:

1. Terminal 1: `uv run python dashboard/app.py`, open the forwarded port
   8787 (the [toolbay monitor](observability.md#the-toolbay-monitor-dashboard)).
2. Terminal 2: `uv run python examples/demo.py`. Every feature runs in
   order against the bundled Postgres, and each call appears on the
   dashboard live.
3. The demo ends blocked on a $500 refund. Approve or reject it from the
   dashboard, or with the `tbay approve` command the demo prints.

Postgres and Redis listen on `localhost` inside the container, and VS Code
forwards 5432/6379/8787 to your machine, so the same URLs work in a normal
terminal while the container is open.

## Running the demo outside the container

The demo always uses Postgres (no SQLite fallback). Its default DSN is
exactly what the dev container forwards, so it works unchanged while the
container is open. Against your own servers:

```
export TBAY_DB_URL="postgresql://user:pass@host:5432/dbname"
export TBAY_TEST_REDIS_URL="redis://host:6379/0"   # optional
uv run python examples/demo.py
```

## Development

```
uv sync --extra dev
uv run pytest          # Postgres/Redis tests need TBAY_TEST_PG_DSN / TBAY_TEST_REDIS_URL
```

CI and the dev container provide both databases automatically.
