# Runtime safety controls

Policies decide how each call behaves. This page covers the controls that
sit *around* the policies: the kill switch that stops an agent NOW, spend
budgets that cap magnitude rather than call counts, and stale-lease
recovery for executions abandoned by a crashed process. Together with
approvals, these are the controls you'll reach for the day an agent
misbehaves in production.

## The kill switch: `tbay pause`

An agent stuck in a loop, a prompt injection driving strange tool calls, a
bad deploy: sometimes the right response is "stop everything, right now,
without redeploying anything". The pause is a row in the shared database,
so it takes effect for **every process and host** using it, immediately:

```
tbay pause --reason "agent runaway, investigating"       # everything
tbay pause --tool send_email --reason "spamming"         # one tool
tbay resume                                              # lift the global pause
tbay resume --tool send_email
```

Or from code (an anomaly-detecting event handler can slam the brake on
itself):

```python
client.pause(reason="incident 4711", by="oncall")
client.pause("send_email", reason="spamming")
client.resume()
client.paused()    # {"*": {"reason": ..., "by": ..., "at": ...}} while paused
```

While paused, guarded calls raise `ToolPaused` immediately, *before*
acquiring anything, with the reason in the message so the agent (and its
logs) can see why. `tbay stats` shows active pauses. A pause survives
`tbay clear`, and there is no timeout on it: it stays until someone lifts
it.

**Trust model.** The pause lives in the database, so anyone with database
write access can lift it, the same as every other tbay control. It's an
operational brake, not a security boundary against a database-credentialed
attacker.

## Budgets: capping magnitude, not just call counts

A rate limit stops a tool from being called too *often*. It does nothing
about an agent making 10 perfectly-paced calls that each refund $9,999. A
budget caps the **sum of a numeric argument** across a rolling window:

```yaml
policies:
  refunds:
    approval_required: true
    approval_bypass_arg: amount
    approval_bypass_max: 50
    budget:
      arg: amount        # the numeric argument to meter
      max: 1000          # its total may not pass this...
      per: 1d            # ...within this rolling window
```

Now refunds under $50 run without a human, larger ones pause for approval,
and *in total* the tool cannot move more than $1000/day, however the
amounts are sliced, across every process sharing the database. The first
call that would land past the cap raises `BudgetExceeded` and never runs.
A call landing exactly on the cap still runs.

The details, so you can reason about edge cases:

- Budgets are per tool per tenant, like rate limits.
- Cache hits and followed singleflights spend nothing; only real
  executions are metered.
- A budgeted call whose metered argument is missing or not numeric is
  refused outright (`BudgetExceeded`): a call that can't be measured can't
  be safely counted.
- Every recorded execution counts, including ones that later failed: a
  call that died mid-flight may still have had its side effect, so tbay
  never under-counts spend.

## Stale-lease recovery: surviving crashed owners

When a process acquires an execution and then dies (OOM kill, spot
instance reclaim, `kubectl delete`), its row stays `RUNNING` forever. By
default every later call with the same idempotency key waits for a result
that will never come, and the dead row eats a `max_concurrent` slot
permanently.

`lease_timeout` bounds the damage: a `RUNNING` execution older than this
many seconds is treated as abandoned, and the next caller **reclaims** it
atomically (a compare-and-swap on the row's observed `created_at`, so
exactly one contender wins, on every backend) and runs it for real.

```yaml
policies:
  readonly:
    lease_timeout: 10m    # on by default for readonly; re-running a read is harmless
  mutating:
    lease_timeout: null   # off by default: a false reclaim would double-run
```

Turn it on where a rare double-run is acceptable, and keep it comfortably
longer than the slowest legitimate call. It's on by default only for the
`readonly` tier. Executions sitting in `WAITING_APPROVAL` are never
reclaimed; a pending human decision has no deadline other than the
policy's `approval_timeout`.

## Which control for which failure

| Failure mode | Control |
|---|---|
| Agent calls a tool too often | `rate_limit` |
| Agent moves too much money/data in total | `budget` |
| Agent hammers a tool with parallel calls | `max_concurrent` |
| One call is dangerous enough to need a human | `approval_required` |
| Everything is on fire right now | `tbay pause` |
| A worker died mid-call | `lease_timeout` |
| A single call hangs | `execution_timeout` |
