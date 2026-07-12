"""Approval walkthrough: see every moving part of the approval flow in one run.

What this demonstrates, in order:

1. The bypass threshold: a $20 refund runs immediately, no human involved,
   because the policy says amounts of $50 or less skip approval.
2. The pause: a $500 refund does NOT run. tbay writes a WAITING_APPROVAL
   row to the database and the calling code blocks, polling that row.
3. The webhook: the moment the call pauses, tbay fires one HTTP POST with
   {"execution_id": ..., "tool_name": ...} at the policy's approval_webhook
   URL. This script runs a tiny local HTTP server standing in for Slack, so
   you'll see the exact payload printed when it arrives. The webhook is
   best-effort notification only: if it fails, nothing breaks, the call
   just keeps waiting and you'd find it via `tbay log` or the dashboard.
4. The decision: YOU approve or reject from a second terminal (or from the
   dashboard's Approve/Reject buttons). The blocked call notices the
   changed row on its next poll and either runs the real function and
   returns its result, or raises ApprovalRejected without ever running it.

Run: python examples/approval_demo.py
Then, in a second terminal, run the approve command it prints.

Uses $TBAY_DB_URL when set (the dev container sets it to the same database
the dashboard watches), otherwise a local SQLite demo file.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from tbay import ApprovalRejected, TbayClient, guarded

DB_URL = os.environ.get("TBAY_DB_URL", "sqlite:///~/.tbay/demo.sqlite")
WEBHOOK_PORT = 9911

client = TbayClient(DB_URL, poll_interval=0.25)

# Configured in code so the demo is self-contained; normally this lives in
# your policy YAML file (see policy.example.yaml).
policy = client.policies["destructive"]
policy.approval_webhook = f"http://127.0.0.1:{WEBHOOK_PORT}/approval-needed"
policy.approval_bypass_arg = "amount"
policy.approval_bypass_max = 50
policy.approval_timeout = 600  # give the human 10 minutes before giving up


class WebhookStandIn(BaseHTTPRequestHandler):
    """Plays the part of Slack/PagerDuty/your ops tool: whatever is on the
    receiving end of approval_webhook. It just prints what tbay sends it."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body)
        print("\n[webhook stand-in] tbay just POSTed to", self.path)
        print(f"[webhook stand-in] payload: {payload}")
        print("[webhook stand-in] a real integration would now ping a human with:")
        print(f"[webhook stand-in]   tbay --db-url {DB_URL} approve {payload['execution_id']}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    print(f"  -> actually issuing a ${amount} refund to {customer_id}")
    return {"customer_id": customer_id, "amount": amount, "status": "refunded"}


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", WEBHOOK_PORT), WebhookStandIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"webhook stand-in listening on http://127.0.0.1:{WEBHOOK_PORT} (pretend it's Slack)\n")

    print("1. $20 refund: at or under the $50 approval_bypass_max, so it runs immediately:")
    print(refund_customer("cust_1", 20.0))

    print("\n2. $500 refund: over the threshold, so it pauses BEFORE running.")
    print("   The function will not execute until someone approves. Watch for the")
    print("   webhook arriving below, then approve (or reject) from a second terminal:")
    print(f"     tbay --db-url {DB_URL} log --status WAITING_APPROVAL")
    print(f"     tbay --db-url {DB_URL} approve <execution_id>")
    print(f"     tbay --db-url {DB_URL} reject  <execution_id>")
    print("   or click Approve/Reject on the row in the dashboard.\n")

    started = time.time()
    try:
        result = refund_customer("cust_2", 500.0)
        print(f"\napproved after {time.time() - started:.0f}s; the function then ran: {result}")
    except ApprovalRejected:
        print(f"\nrejected after {time.time() - started:.0f}s; the function never ran at all.")
