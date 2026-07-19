"""Recursive argument redaction for the audit log.

Everything a guarded call's arguments contain is written to the audit log
(and shown to whoever approves a paused call), so anything sensitive has to
be masked *before* it is stored. Three complementary controls, all set on
the policy:

    redact_args:      ["card_number", "customer.email"]   # names or dotted paths
    redact_patterns:  ["(?i)internal_"]                    # regexes matched against key names
    redact_auto:      true                                 # mask well-known secret-ish key names

- A bare name in `redact_args` masks that key wherever it appears, at any
  depth, including inside lists of objects.
- A dotted path ("card.number") masks only that exact path. List elements
  are transparent to paths, so "cards.number" matches every card in a list
  under "cards".
- `redact_patterns` are regular expressions tested (re.search) against each
  key name at any depth.
- `redact_auto: true` applies AUTO_PATTERN, a curated pattern for key names
  that almost always hold secrets (password, token, api_key, authorization,
  credential, private_key, cvv, ...). It is off by default so a policy's
  behavior never changes silently, but turning it on for every policy that
  handles third-party input is cheap insurance.

Only argument *names* decide what gets masked; values are never inspected.
Masked values are replaced with MASK ("***REDACTED***") wholesale, so
nothing about their shape or length leaks either.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Pattern, Sequence, Tuple

MASK = "***REDACTED***"

AUTO_PATTERN = re.compile(
    r"(?i)("
    r"password|passwd|passphrase|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|authorization|bearer|credential|private[_-]?key|"
    r"client[_-]?secret|card[_-]?number|cvv|cvc|ssn|cookie|session[_-]?id"
    r")"
)


def _compile(patterns: Iterable[str]) -> List[Pattern[str]]:
    return [re.compile(p) for p in patterns]


def redact_structure(
    value: Any,
    fields: Sequence[str] = (),
    patterns: Sequence[str] = (),
    auto: bool = False,
) -> Any:
    """Return a copy of `value` (a JSON-ish structure of dicts/lists/scalars)
    with sensitive entries replaced by MASK. See the module docstring for
    what `fields`, `patterns`, and `auto` each match."""
    names = {f for f in fields if "." not in f}
    paths = [tuple(f.split(".")) for f in fields if "." in f]
    compiled = _compile(patterns)

    def should_mask(key: str, path: Tuple[str, ...]) -> bool:
        if key in names:
            return True
        if any(path == p for p in paths):
            return True
        if any(rx.search(key) for rx in compiled):
            return True
        if auto and AUTO_PATTERN.search(key):
            return True
        return False

    def walk(node: Any, path: Tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            return {
                key: (MASK if should_mask(str(key), path + (str(key),)) else walk(child, path + (str(key),)))
                for key, child in node.items()
            }
        if isinstance(node, (list, tuple)):
            # Lists are transparent to paths: "cards.number" masks the
            # number of every card in a list stored under "cards".
            return [walk(child, path) for child in node]
        return node

    return walk(value, ())
