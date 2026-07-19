<h1 align="center">tbay</h1>

<p align="center"><b>the toolbay &mdash; execution safety for AI agent tool calls</b></p>

<p align="center">
  <a href="https://github.com/akhilkanduri/tbay/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/akhilkanduri/tbay/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://pypi.org/project/tbay/"><img alt="PyPI" src="https://img.shields.io/pypi/v/tbay?color=blue"></a>
  <a href="https://pypi.org/project/tbay/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/tbay"></a>
  <a href="https://pepy.tech/projects/tbay"><img alt="downloads" src="https://static.pepy.tech/personalized-badge/tbay?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=yellow&left_text=Downloads"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="typed" src="https://img.shields.io/badge/types-py.typed-informational">
</p>

Your agent framework decides **what** to do. tbay decides whether it
actually **happens** &mdash; exactly once, within budget, with a human in the
loop when it matters, stoppable with one command, and with a paper trail
that says who asked and why.

A *toolbay* is where your agent's tools dock before they touch the real
world. It's a library you `pip install`, not a service you depend on:
state lives in a database **you** own (SQLite, Postgres, or Redis), and
nothing calls home.

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

That's the whole integration. `github_search` now caches and collapses
concurrent duplicates; `refund_customer` can never double-run and pauses
for a human before executing. `@guarded` wraps a plain callable, so it
drops in under LangChain's `@tool`, the OpenAI Agents SDK's
`@function_tool`, CrewAI tools, an MCP server's handlers, or bare
functions &mdash; [recipes here](docs/integrations.md).

```text
   agent framework  (plans)      LangChain · OpenAI Agents SDK · CrewAI · MCP · plain Python
         │
         ▼
   tbay  (decides)               dedupe · cache · rate/concurrency limits · budgets
         │                       approvals · kill switch · audit log · events
         ▼
   your tools  (execute)         APIs · databases · payments · infrastructure
```

## What it stops

| Your agent... | tbay's answer |
|---|---|
| retries and **double-charges** a customer | idempotency: one execution per unique call, atomically, across processes and hosts |
| fires the same expensive query from 10 workers at once | singleflight: 1 real call, 9 followers share the result |
| hammers a paid API in a loop | `rate_limit` + `max_concurrent`, enforced in the database, not per-process |
| moves **too much money** in perfectly-paced small calls | [budgets](docs/controls.md): cap the *sum* of an argument per rolling window &mdash; `budget: {arg: amount, max: 1000, per: 1d}` |
| does something a human should sign off on | [approvals](docs/approvals.md): the call parks *before* running; `tbay approve` / `tbay reject --reason` resumes or refuses it |
| gets its approval forged by anyone with the database password | signed approvals: HMAC-verified decisions, so storage access &ne; approval authority |
| goes completely off the rails **right now** | the [kill switch](docs/controls.md): `tbay pause --reason "..."` stops every process on the database, instantly |
| leaves a call wedged when a worker dies mid-execution | stale-lease recovery: the next caller reclaims it, atomically, exactly once |
| writes card numbers into logs an approver reads | [deep redaction](docs/policies.md): names at any depth, dotted paths, regexes, auto secret detection |
| acts with no accountability | audit log with **which agent** asked and **its stated reasoning**, per call &mdash; plus live [events](docs/observability.md) and optional OpenTelemetry spans |

Behavior lives in **policies** &mdash; named risk tiers in a YAML file, not in
your tool code. Misspell a safety setting and the file refuses to load;
a guardrail you believe exists but doesn't is the worst kind of bug.

## Install

```
pip install tbay               # SQLite backend, stdlib only
pip install tbay[postgres]     # + Postgres
pip install tbay[redis]        # + Redis
pip install tbay[otel]         # + OpenTelemetry spans
```

## 60 seconds to your first guardrail

```python
from tbay import TbayClient, guarded

client = TbayClient()                      # zero-config local SQLite

@guarded(client, policy="mutating")
def send_invoice(order_id: str) -> dict:
    print("charging", order_id)            # runs ONCE
    return {"sent": order_id}

send_invoice("order_42")
send_invoice("order_42")                   # deduped: no second charge
```

```console
$ tbay log                # what happened, with args and results
$ tbay pending            # what's waiting for a human
$ tbay stats              # the fleet at a glance
$ tbay pause --reason "incident"   # the emergency brake
```

Then take the **[tutorial](examples/tutorial/README.md)**: 13 runnable,
narrated scripts &mdash; one per feature, each proving its behavior with
assertions, from first guarded call to a self-pausing circuit breaker
built on the event system.

## When it hits the fan

```yaml
# policy.yaml — the whole safety posture for refunds, reviewable in a PR
policies:
  refunds:
    approval_required: true
    approval_bypass_arg: amount      # ≤ $50 runs without a human
    approval_bypass_max: 50
    budget: {arg: amount, max: 1000, per: 1d}   # hard daily total, all processes
    redact_args: [card_number]
    redact_auto: true
```

```console
$ tbay pause --tool refund_customer --reason "chargeback spike"
$ tbay resume --tool refund_customer
```

Small refunds: automatic. Big ones: a human decides, and the decision is
cryptographically signed. The daily total: hard-capped even if every
individual call looked fine. And when in doubt &mdash; one command stops
everything, everywhere, with no redeploy.

## Documentation

| Guide | What it covers |
|---|---|
| [Tutorial](examples/tutorial/README.md) | 13 runnable, narrated scripts: every feature, step by step, SQLite-only |
| [Design rationale](docs/design.md) | *Why* each mechanism works the way it does &mdash; the trade-offs, honestly |
| [Quickstart](docs/quickstart.md) | First guarded tool, the dev container, running the demo |
| [Policies](docs/policies.md) | The four risk tiers, the YAML file, every policy field |
| [Caching and idempotency](docs/caching.md) | Idempotency keys, TTL, singleflight, semantic caching, volatile calls |
| [Approvals](docs/approvals.md) | Pause/approve flow, signed webhooks, bypass thresholds, signed approvals |
| [Runtime controls](docs/controls.md) | The kill switch, spend budgets, stale-lease crash recovery |
| [Observability](docs/observability.md) | Audit log, lifecycle events, OpenTelemetry, agent identity, the CLI, the dashboard |
| [Integrations](docs/integrations.md) | LangChain, OpenAI Agents SDK, CrewAI, MCP servers, `guard_tools` |
| [Storage backends](docs/backends.md) | SQLite, Postgres, Redis: guarantees, schema, migrations |
| [API reference](docs/api.md) | Every public function and class, with examples |

## Try the full demo in two minutes

Open the repo in VS Code, run "Dev Containers: Reopen in Container"
(bundles Python + Postgres + Redis, nothing to install), then:

```
uv run python dashboard/app.py       # terminal 1: live dashboard on port 8787
uv run python examples/demo.py       # terminal 2: every feature, one run
```

The demo ends blocked on a $500 refund; approve or reject it from the
**toolbay monitor** dashboard and watch the blocked call resume &mdash; or
learn why it was refused. Details in the [Quickstart](docs/quickstart.md).

## Development

```
uv sync --extra dev
uv run pytest            # 113 tests; Postgres/Redis-gated ones need
                         # TBAY_TEST_PG_DSN / TBAY_TEST_REDIS_URL
uv run ruff check src tests examples
```

Contributions welcome &mdash; the [roadmap issues](https://github.com/akhilkanduri/tbay/issues)
are written to be picked up, several as first issues. Please read the
[PR checklist](.github/pull_request_template.md) (short version: all
three backends, nothing silent, docs updated) and report security issues
via [SECURITY.md](.github/SECURITY.md), not the public tracker.

## License

MIT
