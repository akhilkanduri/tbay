"""The tbay demo: every feature, one file, one run.

    uv run python examples/demo.py

Where it stores state: Postgres, always.
  - Inside the dev container, TBAY_DB_URL is already set to the bundled
    Postgres and TBAY_TEST_REDIS_URL to the bundled Redis, so this demo
    uses both automatically and its calls show up live on the dashboard
    (`uv run python dashboard/app.py`, port 8787).
  - Outside the container the same default DSN
    (postgresql://postgres:tbay@localhost:5432/tbay) still works while the
    dev container is open, because it forwards port 5432 to your machine.
    Point TBAY_DB_URL at any other Postgres to use that instead. There is
    no SQLite fallback here; if Postgres isn't reachable, the demo says so
    and exits instead of silently writing somewhere else.

What it walks through, in order:
  1. readonly caching        an identical call is served from cache
  2. semantic caching        a REWORDED call is served from cache
  3. mutating idempotency    the same mutation never double-runs
  4. volatile calls          LLM-style calls always run fresh
  5. reasoning audit         record WHY the agent acted, next to the action
  6. redis backend           the same guarantees over Redis (if configured)
  7. framework stacking      LangChain / OpenAI Agents SDK (if installed)
  8. approval flow           bypass, pause, webhook, and YOUR approval

Step 8 blocks on purpose: the script waits until you approve or reject the
$500 refund from a second terminal (`tbay approve <id>`) or the dashboard.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from tbay import ApprovalRejected, TbayClient, agent, guarded, reasoning

DB_URL = os.environ.get("TBAY_DB_URL", "postgresql://postgres:tbay@localhost:5432/tbay")
REDIS_URL = os.environ.get("TBAY_TEST_REDIS_URL")  # set by the dev container
WEBHOOK_PORT = 9911

try:
    client = TbayClient(DB_URL, poll_interval=0.25)
except Exception as exc:
    raise SystemExit(
        f"could not connect to Postgres at {DB_URL}\n"
        f"  ({exc})\n"
        "open the repo's dev container (it bundles Postgres and forwards port 5432),\n"
        "or point TBAY_DB_URL at a Postgres you run."
    )

# Tuned in code so the demo is self-contained; in a real project these
# live in your policy YAML file (see policy.example.yaml).
client.policies["readonly"].semantic_cache = True
destructive = client.policies["destructive"]
destructive.approval_webhook = f"http://127.0.0.1:{WEBHOOK_PORT}/approval-needed"
destructive.approval_bypass_arg = "amount"
destructive.approval_bypass_max = 50
destructive.approval_timeout = 600


# -- the guarded tools ---------------------------------------------------------

@guarded(client, policy="readonly")
def web_search(query: str) -> dict:
    print(f"      -> actually running the search for {query!r}")
    return {"query": query, "top": "https://example.com/result"}


@guarded(client, policy="mutating")
def create_ticket(title: str) -> dict:
    print(f"      -> actually creating a ticket titled {title!r}")
    return {"ticket_id": "TCK-1", "title": title}


@guarded(client, policy="volatile")
def ask_llm_for_next_step(context: str) -> dict:
    print(f"      -> actually calling the LLM with context {context!r}")
    return {"decision": "escalate_to_human"}


@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    print(f"      -> actually issuing a ${amount} refund to {customer_id}")
    return {"customer_id": customer_id, "amount": amount, "status": "refunded"}


# -- step 8's webhook stand-in -------------------------------------------------

class WebhookStandIn(BaseHTTPRequestHandler):
    """Plays the part of Slack/PagerDuty: whatever is on the receiving end
    of approval_webhook. It just prints what tbay POSTs to it."""

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        print(f"\n      [webhook stand-in] tbay POSTed: {payload}")
        print("      [webhook stand-in] a real integration would now ping a human with:")
        print(f"      [webhook stand-in]   tbay --db-url {DB_URL} approve {payload['execution_id']}")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def step_frameworks():
    """@guarded stacks under any framework's tool decorator, because it only
    ever wraps the plain callable underneath. Shown for whichever of
    LangChain / OpenAI Agents SDK is installed; skipped otherwise."""
    try:
        from langchain_core.tools import tool as langchain_tool
    except ImportError:
        print("      langchain-core not installed; `uv add langchain-core` to see this part")
    else:
        @langchain_tool
        @guarded(client, policy="readonly")
        def github_search(query: str) -> dict:
            """Search GitHub for repositories matching the query."""
            print(f"      -> actually calling the GitHub API for {query!r}")
            return {"query": query, "results": ["repo-a", "repo-b"]}

        print("      LangChain: invoking the same @tool twice, second is a cache hit:")
        print("     ", github_search.invoke({"query": "tbay execution safety"}))
        print("     ", github_search.invoke({"query": "tbay execution safety"}))

    try:
        from agents import function_tool
    except ImportError:
        print("      openai-agents not installed; `uv add openai-agents` to see this part")
    else:
        @function_tool
        @guarded(client, policy="readonly")
        def repo_lookup(name: str) -> dict:
            """Look up a repository by name."""
            return {"name": name}

        print("      OpenAI Agents SDK: repo_lookup registered as a guarded @function_tool")
        print("      (running a real agent with it additionally needs OPENAI_API_KEY)")


if __name__ == "__main__":
    print(f"using database: {DB_URL}")
    print("open the dashboard in another terminal to watch all of this live:")
    print("  uv run python dashboard/app.py\n")

    print("1. readonly caching: the second identical call never executes")
    print("  ", web_search("weather in berlin today"))
    print("  ", web_search("weather in berlin today"))

    print("\n2. semantic caching: same words REORDERED, still served from cache")
    print("  ", web_search("today weather in berlin"))

    print("\n3. mutating idempotency: the same mutation never double-runs")
    print("  ", create_ticket("fix flaky test"))
    print("  ", create_ticket("fix flaky test"))

    print("\n4. volatile: identical args, but every call runs fresh")
    print("  ", ask_llm_for_next_step("customer is angry"))
    print("  ", ask_llm_for_next_step("customer is angry"))

    print("\n5. reasoning + agent identity: WHO acted and WHY, stored next to the action")
    with agent("support-agent-1", model="gpt-5", team="support", version="1.4"), \
            reasoning("user reported the checkout page is down, escalating"):
        print("  ", create_ticket("checkout page outage"))
    print(f"   see both with: tbay --db-url {DB_URL} log --tool create_ticket")

    print("\n6. redis backend: the same guarantees, coordinated through Redis")
    if REDIS_URL:
        redis_client = TbayClient(REDIS_URL, poll_interval=0.25)

        @guarded(redis_client, policy="mutating")
        def send_email(to: str) -> dict:
            print(f"      -> actually sending an email to {to}")
            return {"sent_to": to}

        print("  ", send_email("ops@example.com"))
        print("  ", send_email("ops@example.com"))  # idempotent over Redis too
    else:
        print("      TBAY_TEST_REDIS_URL not set; skipped (the dev container sets it)")

    print("\n7. framework stacking:")
    step_frameworks()

    print("\n8. approval flow:")
    server = HTTPServer(("127.0.0.1", WEBHOOK_PORT), WebhookStandIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("   a) $20 refund: at or under the $50 bypass threshold, runs with no human")
    print("  ", refund_customer("cust_1", 20.0))

    print("\n   b) $500 refund: pauses BEFORE running. Watch the webhook fire below,")
    print("      then approve or reject from a second terminal:")
    print(f"        tbay --db-url {DB_URL} log --status WAITING_APPROVAL")
    print(f"        tbay --db-url {DB_URL} approve <execution_id>")
    print(f'        tbay --db-url {DB_URL} reject  <execution_id> --reason "too large for auto-refund"')
    print("      or click Approve/Reject on the dashboard. This blocks until you do.")

    started = time.time()
    try:
        result = refund_customer("cust_2", 500.0)
        print(f"\n   approved after {time.time() - started:.0f}s; only then did the function run:")
        print("  ", result)
    except ApprovalRejected as exc:
        print(f"\n   rejected after {time.time() - started:.0f}s; the function never ran at all.")
        print(f"   the caller learns why: {exc}")

    print("\ndone. inspect the full audit trail:")
    print(f"  tbay --db-url {DB_URL} log")
