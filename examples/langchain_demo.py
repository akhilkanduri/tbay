"""LangChain demo: @guarded stacks directly under LangChain's own @tool
decorator. Tbay never touches LangChain's Tool class at all, it only ever
wraps the plain function underneath it, so this same pattern works with any
other framework's tool decorator too.

Requires: pip install langchain-core
Run: python examples/langchain_demo.py
"""
from langchain_core.tools import tool

from tbay import TbayClient, guarded

client = TbayClient("sqlite:///~/.tbay/demo.sqlite")


@tool
@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    """Search GitHub for repositories matching the query."""
    print(f"  -> actually calling GitHub API for {query!r}")
    return {"query": query, "results": ["repo-a", "repo-b"]}


@tool
@guarded(client, policy="volatile")
def ask_llm_for_next_step(context: str) -> dict:
    """Ask an LLM what to do next. Every call should get a fresh answer,
    even with the exact same context, so this uses "volatile" instead of
    "readonly": idempotent=False means tbay never caches or dedupes it."""
    print(f"  -> actually calling the LLM with context {context!r}")
    return {"decision": "escalate_to_human"}


@tool
@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    """Issue a refund to a customer. Destructive: pauses for approval unless
    policy.example.yaml's approval_bypass_arg/approval_bypass_max lets a
    small enough amount through automatically."""
    print(f"  -> actually issuing a ${amount} refund to {customer_id}")
    return {"customer_id": customer_id, "amount": amount, "status": "refunded"}


if __name__ == "__main__":
    # LangChain tools are invoked with .invoke({...}); tbay's idempotency
    # and cache logic runs underneath, completely invisible to the agent.
    print(github_search.invoke({"query": "tbay agent execution safety"}))
    print(github_search.invoke({"query": "tbay agent execution safety"}))  # cache hit, no real call

    print(ask_llm_for_next_step.invoke({"context": "customer is angry"}))
    print(ask_llm_for_next_step.invoke({"context": "customer is angry"}))  # same args, still a real call

    print("\nrefund_customer is destructive and will block for approval; run:")
    print("  tbay --db-url sqlite:///~/.tbay/demo.sqlite log")
    print("  tbay --db-url sqlite:///~/.tbay/demo.sqlite approve <execution_id>")
    print(refund_customer.invoke({"customer_id": "cust_42", "amount": 19.99}))
