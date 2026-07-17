# tbay

[![installs](https://static.pepy.tech/personalized-badge/tbay?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=yellow&left_text=Downloads)](https://pepy.tech/projects/tbay) ![GitHub stars](https://img.shields.io/github/stars/akhilkanduri/tbay)


Execution safety for AI agent tool calls: idempotency, TTL and semantic
caching, singleflight deduplication, risk-tiered policy, human approval
gating (optionally cryptographically signed), and a reasoning- and
agent-linked audit log. A library you install, not a service you depend
on.

```python
from tbay import TbayClient, guarded

client = TbayClient("postgresql://postgres:tbay@localhost:5432/tbay")
# also: "sqlite:///~/.tbay/db.sqlite" or "redis://localhost:6379/0"

@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    return real_github_api_call(query)

@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    return stripe_refund(customer_id, amount)
```

`github_search` now caches and collapses concurrent duplicates;
`refund_customer` never double-runs and pauses for a human before
executing. `@guarded` only wraps a plain callable, so it drops in under
LangChain's `@tool`, the OpenAI Agents SDK's `@function_tool`, CrewAI
tools, or bare functions.

Agent frameworks solve planning and orchestration; none of them solve
*execution safety*. Once a tool is selected, nothing stops it from being
called twice, called too often, or fired on a destructive action without
a human in the loop. tbay sits underneath any framework and handles that,
durably, across processes, in whatever database you already run. **Not a
hosted service**: state lives entirely in a database you own; nothing
calls home.

## Install

```
pip install tbay               # SQLite backend, stdlib only
pip install tbay[postgres]     # + Postgres
pip install tbay[redis]        # + Redis

# or: uv add tbay / "tbay[postgres]" / "tbay[redis]"
```

## Documentation

| Guide | What it covers |
|---|---|
| [Quickstart](docs/quickstart.md) | First guarded tool, the dev container, running the demo |
| [Policies](docs/policies.md) | The four risk tiers, the YAML file, every policy field |
| [Caching and idempotency](docs/caching.md) | Idempotency keys, TTL, singleflight, semantic caching, volatile calls, rate limits |
| [Approvals](docs/approvals.md) | Pause/approve flow, webhooks, bypass thresholds, signed approvals, rejection reasons |
| [Observability](docs/observability.md) | Audit log, reasoning traces, agent identity, the CLI, the toolbay monitor dashboard |
| [Storage backends](docs/backends.md) | SQLite, Postgres, Redis: guarantees, schema, migrations, clearing data |
| [API reference](docs/api.md) | Every public function and class, with examples |

## Try it in two minutes

Open the repo in VS Code, run "Dev Containers: Reopen in Container"
(bundles Python + Postgres + Redis, nothing to install), then:

```
uv run python dashboard/app.py       # terminal 1: live dashboard on port 8787
uv run python examples/demo.py       # terminal 2: every feature, one run
```

The demo ends blocked on a $500 refund; approve or reject it from the
dashboard and watch the blocked call resume or learn why it was refused.
Details in the [Quickstart](docs/quickstart.md).

## Development

```
uv sync --extra dev
uv run pytest
```

Postgres- and Redis-backed tests need `TBAY_TEST_PG_DSN` /
`TBAY_TEST_REDIS_URL`; CI and the dev container provide both.

## License

MIT
