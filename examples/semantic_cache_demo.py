"""Semantic caching and reasoning-trace demo.

Semantic caching serves a stored result when a new call's arguments are
merely similar (by embedding cosine similarity), not byte-identical. The
reasoning context records WHY the agent made each call, right in the audit
log next to the call itself.

Run: python examples/semantic_cache_demo.py
Then inspect the audit trail: tbay --db-url sqlite:///~/.tbay/demo.sqlite log
"""
import time

from tbay import TbayClient, guarded, reasoning

client = TbayClient("sqlite:///~/.tbay/demo.sqlite")

# Enabled in code for the demo; normally this lives in a policy YAML file:
#   readonly:
#     semantic_cache: true
#     semantic_threshold: 0.92
# Only enable semantic caching on read-only tools. A "close enough" answer
# is fine for a search; it is not fine for a refund.
client.policies["readonly"].semantic_cache = True


@guarded(client, policy="readonly")
def web_search(query: str) -> dict:
    print(f"  -> actually running the search for {query!r}")
    return {"query": query, "top_result": "https://example.com/weather-berlin"}


@guarded(client, policy="mutating")
def create_ticket(title: str) -> dict:
    print(f"  -> actually creating a ticket titled {title!r}")
    return {"ticket_id": "TCK-9", "title": title}


if __name__ == "__main__":
    print("1. first search executes for real:")
    print(web_search("weather in berlin today"))

    print("\n2. same words, different order: a different idempotency key, but a semantic HIT.")
    print("   The default HashingEmbedder matches reworded-same-tokens queries; plug in a")
    print("   real embedding model via TbayClient(embedder=...) for true paraphrase matching:")
    print(web_search("today weather in berlin"))  # no real search happens

    print("\n3. genuinely different query: semantic miss, executes for real:")
    print(web_search("nvidia stock price"))

    print("\n4. reasoning-linked audit: record WHY the agent acted, next to the action.")
    with reasoning("user reported the checkout page is down, escalating"):
        print(create_ticket("checkout page outage"))

    print("\nNow run `tbay --db-url sqlite:///~/.tbay/demo.sqlite log` and look for the")
    print("reason=... field on the create_ticket row.")
    time.sleep(0.1)
