"""Tutorial 07: redaction — keeping secrets out of the audit log.

Everything a guarded call's arguments contain gets written to the audit
log, shown to approvers, sent in webhook payloads, and exported by
`tbay export`. So anything sensitive must be masked BEFORE storage.
Three policy controls, freely combined:

  redact_args:      ["card_number", "customer.email"]  # names or dotted paths
  redact_patterns:  ["(?i)^internal_"]                 # regexes vs key names
  redact_auto:      true                               # well-known secret names

Masking is by NAME only, recursive to any depth, and replaces the whole
value with ***REDACTED*** so neither content nor shape leaks. The
original arguments are untouched — the real function always receives the
real values; only what gets STORED is masked.

Run it:  python examples/tutorial/07_redaction.py
"""
import json

from _tutorial_helpers import banner, fresh_client, step

from tbay import guarded, redact_structure
from tbay.policy import Policy

banner("07: redaction")
client = fresh_client()

client.policies["payment"] = Policy(
    name="payment",
    idempotent=False, singleflight=False,
    redact_args=["card.number", "cvv"],      # a dotted path + a bare name
    redact_patterns=[r"(?i)^internal_"],     # any key starting with internal_
    redact_auto=True,                        # plus the built-in secret-name list
)

received = {}


@guarded(client, policy="payment")
def charge(card: dict, cvv: str, api_key: str, internal_ref: str, amount: float) -> dict:
    received.update(card=card, cvv=cvv, api_key=api_key)   # the REAL values arrive intact
    return {"charged": amount}


step("1. Call the tool with a nest of sensitive data")
charge(
    card={"number": "4111-1111-1111-1111", "brand": "visa", "holder": "A. Kanduri"},
    cvv="123",
    api_key="sk_live_abcdef",
    internal_ref="risk-batch-7",
    amount=25.0,
)

step("2. The function itself saw the real values (redaction is storage-only)")
print(f"    function received card.number = {received['card']['number']}")
assert received["card"]["number"] == "4111-1111-1111-1111"

step("3. ...but the audit log stored masks")
record = client.backend.list_executions(tool_name="charge", limit=1)[0]
stored = json.loads(record.args_json)
print(json.dumps(stored, indent=4))

assert stored["card"]["number"] == "***REDACTED***"   # dotted path card.number
assert stored["card"]["brand"] == "visa"              # siblings untouched
assert stored["cvv"] == "***REDACTED***"              # bare name
assert stored["api_key"] == "***REDACTED***"          # redact_auto caught it
assert stored["internal_ref"] == "***REDACTED***"     # regex pattern caught it
assert stored["amount"] == 25.0                       # non-sensitive data stays readable

# ---------------------------------------------------------------------------
# Step 4: the matching rules, precisely, via the exported engine
# (tbay.redact_structure — the same function the client uses, so your own
# logging can mask identically).
# ---------------------------------------------------------------------------
step("4. Matching rules on the raw engine")

# 4a. A BARE name masks at ANY depth, including inside lists of objects.
data = {"token": "top", "nested": {"token": "deep"}, "batch": [{"token": "in-list"}]}
print(f"    bare 'token' anywhere:   {redact_structure(data, fields=['token'])}")

# 4b. A DOTTED path masks only that exact path; same key elsewhere survives.
data = {"card": {"number": "4111"}, "number": "not-a-card"}
print(f"    dotted 'card.number':    {redact_structure(data, fields=['card.number'])}")

# 4c. Lists are TRANSPARENT to paths: cards.number matches every card.
data = {"cards": [{"number": "4111"}, {"number": "5500"}]}
print(f"    path through a list:     {redact_structure(data, fields=['cards.number'])}")

# 4d. redact_auto=True applies a curated pattern (password, passwd, secret,
# token, api_key, access_key, authorization, bearer, credential,
# private_key, client_secret, card_number, cvv/cvc, ssn, cookie,
# session_id). Off by default so behavior never changes silently.
data = {"password": "x", "author": "fine", "client_secret": "y"}
print(f"    redact_auto:             {redact_structure(data, auto=True)}")

print("""
WHAT JUST HAPPENED
  - Redaction happens once, at write time, so EVERY downstream surface
    (audit log, approver views, webhooks, exports) sees masks.
  - Bare names: any depth. Dotted paths: exact path, lists transparent.
    Patterns: regex vs key names. redact_auto: curated secret names.
  - The engine is exported as tbay.redact_structure for your own use.

NEXT: 08_limits_timeouts_crash_recovery.py — throughput guardrails and
what happens when a worker dies mid-call.
""")
