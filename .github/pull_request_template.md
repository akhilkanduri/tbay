## What does this PR do?

<!-- One or two sentences. Link the issue it closes: "Closes #123" -->

## Why?

<!-- The problem/motivation. For safety-behavior changes, describe the
     failure mode this prevents (or the false positive it removes). -->

## How was it tested?

<!-- `uv run pytest` output summary. Postgres/Redis-gated tests run when
     TBAY_TEST_PG_DSN / TBAY_TEST_REDIS_URL are set (the dev container
     and CI provide both). -->

## Checklist

- [ ] Tests cover the change — **on all three backends** if it touches
      `src/tbay/backends/` or any atomicity/coordination behavior
- [ ] No **silent** behavior changes: anything that alters what runs,
      what's blocked, or what's stored is loud (new policy field, error,
      or changelog entry) — never a quiet default flip
- [ ] Anything written to storage that could be sensitive respects
      redaction (`_redacted_args_json` path)
- [ ] New policy fields: added to `_KNOWN_KEYS`, `policy.example.yaml`,
      `docs/policies.md`, and validated fail-loud if half-configured
- [ ] Docs updated (`docs/`, and the tutorial in `examples/tutorial/` if
      the feature is user-facing)
- [ ] `CHANGELOG.md` entry added
- [ ] `uv run ruff check src tests examples` passes
