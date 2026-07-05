"""Plain-Python demo: no agent framework at all, just @guarded functions.

Run: python examples/plain_python_demo.py
"""
from tbay import ApprovalRejected, TbayClient, guarded

client = TbayClient("sqlite:///~/.tbay/demo.sqlite")

# Small refunds go straight through; anything over $50 still pauses for a
# human. This is set here in code just for the demo; normally you'd put it
# in a policy YAML file instead (see policy.example.yaml).
client.policies["destructive"].approval_bypass_arg = "amount"
client.policies["destructive"].approval_bypass_max = 50


@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    print(f"  -> actually calling GitHub API for {query!r}")
    return {"query": query, "results": ["repo-a", "repo-b"]}


@guarded(client, policy="mutating")
def create_ticket(title: str) -> dict:
    print(f"  -> actually creating a ticket titled {title!r}")
    return {"ticket_id": "TCK-1", "title": title}


@guarded(client, policy="volatile")
def ask_llm_for_next_step(context: str) -> dict:
    print(f"  -> actually calling the LLM with context {context!r}")
    return {"decision": "escalate_to_human"}


@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    print(f"  -> actually issuing a ${amount} refund to {customer_id}")
    return {"customer_id": customer_id, "amount": amount, "status": "refunded"}


if __name__ == "__main__":
    print("1. calling github_search twice with the same query (readonly: cached):")
    print(github_search("tbay agent execution safety"))
    print(github_search("tbay agent execution safety"))  # cache hit, no real API call

    print("\n2. calling create_ticket twice with the same title (mutating: idempotent):")
    print(create_ticket("fix flaky test"))
    print(create_ticket("fix flaky test"))  # same key, so this returns the stored result and never re-runs

    print("\n3. calling ask_llm_for_next_step twice with the same context (volatile: always fresh):")
    print(ask_llm_for_next_step("customer is angry"))
    print(ask_llm_for_next_step("customer is angry"))  # identical args, but still a real call each time

    print("\n4. a small refund auto-executes (under the $50 approval_bypass_max):")
    print(refund_customer("cust_1", 19.99))

    print("\n5. a large refund still pauses for approval; this call will block until you run,")
    print("   in another terminal:")
    print("     tbay --db-url sqlite:///~/.tbay/demo.sqlite approve <execution_id>")
    print("   (find the execution_id with `tbay --db-url sqlite:///~/.tbay/demo.sqlite log`)")
    try:
        result = refund_customer("cust_2", 500.00)
        print("approved:", result)
    except ApprovalRejected as exc:
        print("rejected:", exc)
