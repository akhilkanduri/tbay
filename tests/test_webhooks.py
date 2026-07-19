"""Approval webhooks: rich signed payloads, scheme allowlist, best-effort."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tbay import guarded, verify_webhook
from tbay.policy import Policy

SECRET = "webhook-secret"


@pytest.fixture
def webhook_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append({"body": body, "headers": dict(self.headers)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", received
    server.shutdown()


def approving_thread(client, tool_name, signature_secret=None):
    """Approve the first WAITING_APPROVAL execution for tool_name, signing if asked."""
    from tbay.security import sign_approval

    def approve():
        import time

        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = client.backend.list_executions(tool_name=tool_name, status="WAITING_APPROVAL")
            if waiting:
                execution_id = waiting[0].id
                signature = sign_approval(signature_secret, execution_id, True) if signature_secret else None
                client.backend.resolve_approval(execution_id, approved=True, signature=signature)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=approve, daemon=True)
    thread.start()
    return thread


def test_webhook_payload_is_rich_and_signed(client, webhook_server):
    url, received = webhook_server
    client.approval_secret = SECRET
    client.policies["gated"] = Policy(
        name="gated", approval_required=True, approval_webhook=url, approval_timeout=5.0,
        redact_args=["card_number"],
    )

    import tbay

    @guarded(client, policy="gated")
    def charge(card_number: str, amount: float) -> dict:
        return {"ok": True}

    approving_thread(client, "charge", signature_secret=SECRET)
    with tbay.agent("billing-agent"), tbay.reasoning("customer asked"):
        charge("4111-1111", 25.0)

    assert len(received) == 1
    body = received[0]["body"]
    payload = json.loads(body)
    assert payload["event"] == "approval.requested"
    assert payload["tool_name"] == "charge"
    assert payload["agent_id"] == "billing-agent"
    assert payload["reasoning"] == "customer asked"
    args = json.loads(payload["args"])
    assert args["card_number"] == "***REDACTED***"  # redaction applies to webhooks too
    assert args["amount"] == 25.0
    # signature verifies against the raw body with the shared secret
    header = received[0]["headers"].get("X-Tbay-Signature")
    assert verify_webhook(SECRET, body, header)
    assert not verify_webhook("wrong-secret", body, header)


def test_non_http_webhook_urls_are_ignored(client, tmp_path):
    """file://, ftp:// and friends never fire: a policy file is config, and
    config must not be able to turn the client into a local file writer."""
    client.policies["gated"] = Policy(
        name="gated", approval_required=True, approval_timeout=1.0,
        approval_webhook=f"file://{tmp_path}/owned",
    )

    @guarded(client, policy="gated")
    def act() -> dict:
        return {"ok": True}

    approving_thread(client, "act")
    assert act() == {"ok": True}  # approval still works without the webhook
    assert not (tmp_path / "owned").exists()


def test_webhook_failure_never_blocks_approval(client):
    client.policies["gated"] = Policy(
        name="gated", approval_required=True, approval_timeout=5.0,
        approval_webhook="http://127.0.0.1:1/unreachable",
    )

    @guarded(client, policy="gated")
    def act() -> dict:
        return {"ok": True}

    approving_thread(client, "act")
    assert act() == {"ok": True}
