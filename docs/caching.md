# Caching and idempotency

## How a guarded call executes

Every `@guarded` call computes an idempotency key (tool name + normalized
args + tenant), then atomically claims a row in your database:

- **First caller** for a key becomes the owner and runs the real function.
- **Concurrent callers** with the identical key block and receive the same
  result when the owner finishes (singleflight). No daemon is involved;
  coordination happens through the database's own atomicity.
- **Later callers** get the stored result instead of re-running:
  permanently for `mutating`/`destructive` policies (true idempotency), or
  until `cache_ttl` expires for `readonly` policies.

Positional and keyword spellings normalize to the same key, so
`create_ticket("outage")` and `create_ticket(title="outage")` dedupe
against each other.

## Custom idempotency keys

By default the key covers *all* arguments. When only some of them define
"the same call", supply `key_fn`:

```python
@guarded(client, policy="mutating", key_fn=lambda customer_id, **_: customer_id)
def upsert_customer(customer_id: str, snapshot: dict) -> dict:
    ...
```

Now two calls for the same customer dedupe even when the snapshot differs.
`key_fn` is ignored for `idempotent: false` policies, which never dedupe.

## Retries

A failed execution stores its error. What happens on the *next* call with
the same key depends on the policy: with `max_retries: 0` the stored error
is replayed (`ExecutionFailed`), so a known-bad mutation is never blindly
re-attempted. With `max_retries: 2, retry_backoff: 1s`, a later call may
reclaim the failed row and try again, at most twice, never sooner than 1
second after the failure.

## Semantic caching

Exact matching misses when an agent rewords its own query:
`"weather in berlin today"` and `"today weather in berlin"` hash to
different keys even though any human calls them the same question. With
`semantic_cache: true`, tbay embeds each call's arguments and serves a
stored result whenever a previous call's embedding is close enough:

```yaml
policies:
  semantic_readonly:
    cache_ttl: 5m
    semantic_cache: true
    semantic_threshold: 0.92
```

Only enable this on read-only tools. A "close enough" answer is fine for a
search; it is not fine for a refund.

The built-in zero-dependency embedder (token hashing) matches queries that
reuse the same words in a different order or shape. For true paraphrase
matching ("weather in berlin" vs "berlin forecast"), plug in a real
embedding model; anything with an `embed(text) -> list[float]` method
works:

```python
class OpenAIEmbedder:
    def __init__(self, client):
        self.client = client

    def embed(self, text):
        out = self.client.embeddings.create(model="text-embedding-3-small", input=text)
        return out.data[0].embedding

client = TbayClient(db_url, embedder=OpenAIEmbedder(openai_client))
```

## Volatile calls

`idempotent: false` (the built-in `volatile` policy) is for calls where
two invocations with the same input are NOT the same call: LLM decisions,
random draws, clock reads. Every call gets its own execution, always runs
for real, and is never cached or deduped. `max_retries` still works, but a
failure simply tries again from scratch rather than replaying a stored
error.

## Throughput guardrails

`rate_limit` and `max_concurrent` protect a tool (and whatever paid or
rate-limited API it calls) from a runaway agent loop. Both are enforced
atomically: on SQLite the RUNNING count and the insert share one
`BEGIN IMMEDIATE` transaction, on Postgres an advisory lock scopes the
check, and on Redis a Lua script runs as one uninterruptible unit, so two
simultaneous callers can never both slip past the cap.
