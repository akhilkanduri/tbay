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

## The CLI

```
tbay log                                   # the audit log, newest first
tbay log --tool refund_customer --status WAITING_APPROVAL
tbay approve <execution_id>
tbay reject <execution_id> --reason "..."
tbay clear                                 # wipe ALL executions/approvals (asks first)
```

Point it at the same database as your app with `--db-url` or
`TBAY_DB_URL`. `tbay clear` resets a demo or dev database; on Redis it
deletes only tbay's own keys, never anything else in that database.

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
