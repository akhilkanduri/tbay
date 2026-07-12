"""OpenAI Agents SDK demo: @guarded stacks under @function_tool the same way
it stacks under LangChain's @tool. Tbay only ever sees the plain callable
underneath, so the SDK's own tool machinery is none the wiser.

Requires: pip install openai-agents
Run: python examples/openai_agents_demo.py
"""
import os

from agents import Agent, Runner, function_tool

from tbay import TbayClient, guarded

DB_URL = os.environ.get("TBAY_DB_URL", "sqlite:///~/.tbay/demo.sqlite")
client = TbayClient(DB_URL)


@function_tool
@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    """Search GitHub for repositories matching the query."""
    print(f"  -> actually calling GitHub API for {query!r}")
    return {"query": query, "results": ["repo-a", "repo-b"]}


@function_tool
@guarded(client, policy="volatile")
def ask_llm_for_next_step(context: str) -> dict:
    """Ask an LLM what to do next. Uses "volatile" (idempotent=False) so
    tbay always runs it fresh, never caching or deduping identical calls."""
    print(f"  -> actually calling the LLM with context {context!r}")
    return {"decision": "escalate_to_human"}


@function_tool
@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    """Issue a refund to a customer. Destructive: pauses for approval unless
    the amount is small enough to bypass it (see policy.example.yaml)."""
    print(f"  -> actually issuing a ${amount} refund to {customer_id}")
    return {"customer_id": customer_id, "amount": amount, "status": "refunded"}


agent = Agent(
    name="support-agent",
    instructions="Use the available tools to help the user.",
    tools=[github_search, ask_llm_for_next_step, refund_customer],
)


if __name__ == "__main__":
    # Every tool call the model makes goes through tbay's idempotency,
    # cache, and approval logic first. The agent framework never has to know.
    result = Runner.run_sync(agent, "Search GitHub for 'tbay agent execution safety'.")
    print(result.final_output)
