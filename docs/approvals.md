# Approvals

## The flow, step by step

Using `refund_customer("cust_2", 500.0)` under the `destructive` policy:

1. **Intercept.** `@guarded` gets control before anything executes.
2. **Bypass check.** With `approval_bypass_arg: amount` and
   `approval_bypass_max: 50`, a $20 refund skips straight to execution;
   a $500 one continues.
3. **Park.** tbay writes the execution as `WAITING_APPROVAL` and a
   `pending` approvals row. The function has not run; that is the entire
   guarantee, since tbay pauses while it still has control.
4. **Notify.** If the policy has `approval_webhook`, one HTTP POST fires
   carrying everything an approval surface needs to render a decision:
   the execution id, tool, tenant, policy, (redacted) arguments, the
   requesting agent, and its stated reasoning. Best effort only: a dead
   webhook never strands a call, and the decision never travels through
   it.
5. **Block and poll.** The caller blocks, re-reading the approvals row
   every `poll_interval` for up to `approval_timeout`.
6. **A human decides**, through any surface that writes the row:

   ```
   tbay approve <execution_id>
   tbay reject  <execution_id> --reason "too large for auto-refund"
   ```

   or the dashboard's Approve/Reject buttons, or your own integration.
7. **Execute or refuse.** Approved: the function finally runs and the
   blocked call returns its result. Rejected: the function never runs and
   the caller gets `ApprovalRejected`; with a `--reason`, the exception
   message carries it, so the agent learns *why*. Timeout:
   `ApprovalTimeout`, and it also never runs.

Run `examples/demo.py` to watch all seven steps live, including a bundled
local webhook receiver that prints the exact payload.

## Rejection reasons

Give a reason when rejecting and it travels everywhere the decision does:

```
$ tbay reject 87a2a853 --reason "amount exceeds daily refund budget"
```

```python
try:
    refund_customer("cust_2", 500.0)
except ApprovalRejected as exc:
    print(exc)   # execution 87a2a853... was rejected: amount exceeds daily refund budget
```

The audit log's error field records it too, and the dashboard's Reject
button prompts for one.

## Signed approvals: separating storage access from approval authority

By default an approval is just a database row, so anyone holding the
database password can approve anything. For anything sensitive, configure
an approval secret:

```python
client = TbayClient(db_url, approval_secret="...")   # or TBAY_APPROVAL_SECRET
```

With a secret configured, approvers attach an HMAC-SHA256 signature over
`(execution_id, decision)`, and the executing client verifies it BEFORE
running the function. A row flipped to "approved" straight in the database
fails verification, and the call is rejected with a clear error instead of
running:

```
execution ... was marked approved WITHOUT a valid signature; refusing to run
```

`tbay approve`/`tbay reject` and the dashboard sign automatically when
`TBAY_APPROVAL_SECRET` is set in their environment. For your own approval
surface:

```python
from tbay import sign_approval

signature = sign_approval(secret, execution_id, approved=True)
backend.resolve_approval(execution_id, approved=True, resolver="slack-bot", signature=signature)
```

Give the secret only to approval surfaces (an operator's CLI, the
dashboard process, your Slack bot); give agents and services only database
credentials. No secret configured keeps the pre-signing behavior, so this
is fully backward compatible.

**Honest limits.** Someone with full database access can still delete rows
or mark executions failed, and someone who can edit the executing
process's code or environment is past any guardrail. Signing protects the
*approve* decision specifically; pair it with least-privilege database
roles for the rest.

## Webhooks in detail

The POST body is JSON:

```json
{
  "event": "approval.requested",
  "execution_id": "87a2a853-...",
  "tool_name": "refund_customer",
  "tenant": "",
  "policy": "destructive",
  "args": "{\"amount\": 500.0, \"card_number\": \"***REDACTED***\"}",
  "reasoning": "customer 42 reported item damaged in transit",
  "agent_id": "billing-agent-7",
  "ts": 1752900000.0
}
```

`args` is the audit-log form, **post-redaction**, so nothing a policy
masks can leak through a webhook either. The request has a 5-second
timeout and all failures are logged and ignored; approval and rejection
work identically whether or not the webhook ever arrived.

Two hardening properties worth knowing:

- **Scheme allowlist.** Only `http://` and `https://` URLs ever fire.
  A policy file is configuration, and configuration must not be able to
  turn the executing client into a `file://` writer or similar.
- **Signatures.** With an approval secret configured, the request carries
  `X-Tbay-Signature: sha256=<hmac>` over the exact body. Anyone can POST
  to your webhook endpoint; only a secret holder can sign. Verify on the
  receiving end:

  ```python
  from tbay import verify_webhook

  if not verify_webhook(secret, raw_body, request.headers.get("X-Tbay-Signature")):
      return 403
  ```
