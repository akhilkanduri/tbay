# Observability

Every execution is a row you can query: what ran, with which (possibly
redacted) arguments, what it returned or how it failed, how long it took,
which agent asked, and why.

## Reasoning traces

`with reasoning(...)` records the agent's stated justification on every
guarded call inside the block, next to the call it explains:

```python
from tbay import reasoning

with reasoning("customer 42 reported item damaged in transit"):
    refund_customer("cust_42", 30.0)
```

Blocks nest (innermost wins), and concurrent async tasks each see their
own text. Note that a `threading.Thread` you spawn does NOT inherit the
surrounding block; each thread starts with a clean context.

## Agent identity

In a multi-agent system, "which tool ran" is half the story; the other
half is "which agent asked". Attach an identity, plus any metadata worth
showing a human, three ways (most specific wins):

```python
from tbay import agent

with agent("billing-agent-7", model="gpt-5", team="payments", version="1.4"):
    refund_customer("cust_42", 30.0)                 # per turn or per call

client = TbayClient(db_url, agent_id="support-bot",  # per client/process
                    agent_meta={"model": "gpt-5"})
# TBAY_AGENT_ID environment variable                 # per deployment
```

The id and metadata are stored on every execution. `tbay log` prints
`agent=billing-agent-7`; the dashboard shows a chip on in-flight cards
(hover for the metadata), an agent column in the table, and the full
metadata JSON in row detail, so an approver sees exactly who is asking
before saying yes.

## Lifecycle events

The audit log answers "what happened" after the fact. Events answer it
live: every decision tbay makes (a call started, a cache hit served, a
budget refused, an approval requested) is emitted as a structured
in-process event you can subscribe to, so tbay plugs into whatever
metrics, logging, or alerting you already run, without depending on any
of it:

```python
from tbay.events import CALL_FAILED, BUDGET_EXCEEDED, KILL_SWITCH_BLOCKED

@client.on
def print_everything(event):
    print(event.type, event.tool_name, event.agent_id, event.data)

@client.on(events=[CALL_FAILED, BUDGET_EXCEEDED, KILL_SWITCH_BLOCKED])
def page_someone(event):
    alerting.notify(f"tbay: {event.type} on {event.tool_name}")
```

Each `Event` carries `type`, `tool_name`, `execution_id`, `tenant`,
`policy`, `agent_id`, `reasoning`, a per-type `data` dict (e.g.
`duration_s` on `call.succeeded`, `spent` on `limit.budget`), and `ts`.
The full list of types is documented in `src/tbay/events.py`. Handlers run
synchronously in subscription order; a handler that raises is logged and
swallowed, never allowed to break the guarded call. Unsubscribe with
`client.off(handler)`.

## OpenTelemetry

If you already trace your agents, two lines put every tbay decision into
those traces:

```python
from tbay.otel import instrument

instrument(client)      # pip install tbay[otel]
```

Every guarded call becomes a span named `tbay.<tool_name>` (parented to
whatever span is current, so it nests under your framework's traces) with
the policy, tenant, agent id, execution id, and outcome as attributes.
Executions this process runs span their real duration, including any wait
for human approval (approval milestones appear as span events); cache
hits and refusals become short spans of their own, because "the call
didn't happen and here's why" is exactly what you want next to the LLM
spans. Failures and blocks get error status.

## The CLI

```
tbay log                                   # the audit log, newest first
tbay log --tool refund_customer --status WAITING_APPROVAL
tbay pending                               # everything waiting for a human, oldest first
tbay show <execution_id>                   # every stored detail of one call
tbay approve <execution_id>
tbay reject <execution_id> --reason "..."
tbay stats                                 # counts by status and tool, active pauses
tbay pause --reason "..."                  # the kill switch (see docs/controls.md)
tbay resume
tbay policies                              # every effective policy and its key settings
tbay export > audit.jsonl                  # the audit log as JSON Lines
tbay clear                                 # wipe ALL executions/approvals (asks first)
```

Point it at the same database as your app with `--db-url` or
`TBAY_DB_URL`. `tbay clear` resets a demo or dev database; on Redis it
deletes only tbay's own keys, never anything else in that database.
`tbay export` writes one JSON object per execution, ready for `jq`, a
warehouse load, or a compliance review; arguments appear exactly as
stored, post-redaction.

## The toolbay monitor dashboard

`dashboard/app.py` is a standalone single-file web app (not part of the
Python package) that watches any mix of Postgres, Redis, and SQLite at
once:

```
uv run python dashboard/app.py --db postgresql://postgres:tbay@localhost:5432/tbay \
                               --db redis://localhost:6379/0
# open http://localhost:8787
```

It shows live in-flight calls with elapsed timers (a call that started a
container or long job stays visible until it returns), paused approvals
with Approve/Reject buttons (Reject prompts for a reason), status counts,
a 10-minute activity sparkline, and every execution's input, output or
error, duration, agent, and reasoning, with expandable syntax-highlighted
detail.

In the dev container it needs zero flags (`TBAY_DASHBOARD_DBS` is
pre-wired and port 8787 is forwarded). With `TBAY_APPROVAL_SECRET` set in
its environment, its decisions are signed. There is no authentication:
bind it to localhost (the default) or put it behind something that
authenticates before exposing it wider. See `dashboard/README.md` for all
options.

## Browsing the raw data from VS Code

The dev container auto-installs SQLTools (with a preconfigured
"tbay postgres" connection for browsing the `executions` and `approvals`
tables) and the official Redis extension (connect to `localhost:6379` and
browse the `tbay:` keys). Both appear in the activity bar after the
container builds.
