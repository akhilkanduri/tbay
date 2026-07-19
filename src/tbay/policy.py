from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger("tbay")

_TTL_RE = re.compile(r"^(\d+)\s*(s|m|h|d)?$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_ttl(value) -> Optional[float]:
    """Turn a duration into seconds. Accepts '5m', '30s', '1h', '2d', a plain
    number of seconds (300), or None. 0 and None both mean "no expiry"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    match = _TTL_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"invalid duration {value!r}, try something like '5m', '30s', or '1h'")
    amount, unit = match.groups()
    return float(int(amount) * _UNITS[unit]) or None


@dataclass
class Policy:
    """One named risk tier. Attach it to a tool with @guarded(client, policy="name").

    Every field has a sensible default, so a policy file only needs to spell
    out the fields it wants to change from DEFAULT_POLICIES.
    """

    name: str

    # caching and idempotency
    cache_ttl: Optional[float] = None  # how long a stored result satisfies later calls; None means forever
    idempotent: bool = True  # once a call with this key succeeds, never silently re-run it.
    # Set this to False for tools that should run fresh every time even with identical
    # arguments: an LLM call used to make a decision, "roll a die", "get the current time".
    # When False, tbay ignores key_fn/caching/singleflight entirely and always executes.
    singleflight: bool = True  # let concurrent identical calls share one execution instead of racing

    # semantic caching: serve a stored result when a new call's arguments are
    # merely *similar* (by embedding cosine similarity), not byte-identical.
    # Only enable this on read-only tools; serving a "close enough" answer for
    # a mutating call would be wrong. See src/tbay/embedders.py for how the
    # vectors are produced and how to plug in a real embedding model.
    semantic_cache: bool = False
    semantic_threshold: float = 0.92  # cosine similarity a candidate must reach to count as a hit

    # retrying a failed call
    max_retries: int = 0  # how many times a new call may re-attempt a previously failed key
    retry_backoff: float = 0.0  # seconds to wait after a failure before a retry is allowed

    # human approval for risky calls
    approval_required: bool = False
    approval_webhook: Optional[str] = None  # fired, best effort, when a call starts waiting for approval
    approval_timeout: float = 3600.0  # give up waiting for a human after this many seconds
    approval_bypass_arg: Optional[str] = None  # e.g. "amount", skip approval when this arg is small enough
    approval_bypass_max: Optional[float] = None  # the threshold that arg must stay at or under to bypass

    # throughput guardrails, so a runaway agent loop can't hammer a tool
    rate_limit_max_calls: Optional[int] = None  # max calls, any args, allowed per rate_limit_window
    rate_limit_window: Optional[float] = None  # the window those calls are counted over, in seconds
    max_concurrent: Optional[int] = None  # at most this many calls to this tool running at once
    concurrency_wait_timeout: float = 300.0  # how long a caller waits for a free slot before giving up

    # spend/budget cap: the SUM of one numeric argument across every call in
    # a rolling window may not exceed budget_max. Rate limits count calls;
    # budgets meter magnitude, which is what actually matters for tools that
    # move money, send messages, or provision resources ("an agent may refund
    # at most $1000 per day in total, whatever the size of each refund").
    budget_arg: Optional[str] = None  # the numeric argument to meter, e.g. "amount"
    budget_max: Optional[float] = None  # ceiling for the window's running total
    budget_window: Optional[float] = None  # rolling window in seconds the total is summed over

    # crash recovery: a process that dies mid-call leaves its execution
    # RUNNING forever, which blocks every later caller with the same key and
    # permanently eats a max_concurrent slot. With lease_timeout set, a
    # RUNNING execution older than this many seconds is treated as abandoned
    # and reclaimed by the next caller (atomically, so exactly one caller
    # wins). Only enable it where a rare double-run is acceptable, and keep
    # it comfortably longer than the slowest legitimate call; the default
    # "readonly" policy has it on, "mutating"/"destructive" leave it off.
    lease_timeout: Optional[float] = None

    # the actual function call
    execution_timeout: Optional[float] = None  # give up on a hung tool call after this many seconds

    # audit log redaction (see src/tbay/redaction.py for exact matching rules)
    redact_args: List[str] = field(default_factory=list)  # names or dotted paths to mask, at any depth
    redact_patterns: List[str] = field(default_factory=list)  # regexes matched against argument key names
    redact_auto: bool = False  # also mask well-known secret-ish key names (password, token, api_key, ...)


DEFAULT_POLICIES: Dict[str, Policy] = {
    "readonly": Policy(
        name="readonly",
        cache_ttl=300.0,
        singleflight=True,
        max_retries=2,
        retry_backoff=1.0,
        lease_timeout=600.0,  # re-running a read is harmless, so recover from crashed owners
    ),
    "mutating": Policy(
        name="mutating",
        idempotent=True,
        cache_ttl=None,
        max_retries=0,
    ),
    "destructive": Policy(
        name="destructive",
        idempotent=True,
        approval_required=True,
        max_retries=0,
    ),
    "volatile": Policy(
        name="volatile",
        idempotent=False,  # always run for real; never dedupe, cache, or singleflight
        cache_ttl=None,
        singleflight=False,
        max_retries=0,
    ),
}

# Every key a policy YAML entry may contain. Anything else is almost
# certainly a typo ("aproval_required") that would otherwise be silently
# ignored -- and a silently ignored safety setting is the worst kind of bug.
_KNOWN_KEYS = {
    "cache_ttl",
    "idempotent",
    "singleflight",
    "semantic_cache",
    "semantic_threshold",
    "max_retries",
    "retry",
    "retry_backoff",
    "approval_required",
    "approval_webhook",
    "approval_timeout",
    "approval_bypass_arg",
    "approval_bypass_max",
    "rate_limit",
    "max_concurrent",
    "concurrency_wait_timeout",
    "budget",
    "lease_timeout",
    "execution_timeout",
    "redact_args",
    "redact_patterns",
    "redact_auto",
}


def _build_policy(name: str, cfg: dict) -> Policy:
    unknown = set(cfg) - _KNOWN_KEYS
    if unknown:
        raise ValueError(
            f"policy {name!r} has unknown key(s) {sorted(unknown)}; "
            f"known keys: {sorted(_KNOWN_KEYS)}. Refusing to load, because a "
            f"misspelled safety setting would otherwise be silently ignored."
        )

    rate_limit = cfg.get("rate_limit") or {}
    budget = cfg.get("budget") or {}
    if budget and not ({"arg", "max", "per"} <= set(budget)):
        raise ValueError(
            f"policy {name!r}: a budget needs all of `arg` (the numeric argument "
            f"to meter), `max` (the cap), and `per` (the window, e.g. '1d'); got {sorted(budget)}"
        )

    max_retries = cfg.get("max_retries")
    if max_retries is None:
        # shorthand from the original plan doc: `retry: true` means "allow one retry"
        max_retries = 1 if cfg.get("retry") else 0

    return Policy(
        name=name,
        cache_ttl=parse_ttl(cfg.get("cache_ttl")),
        idempotent=bool(cfg.get("idempotent", True)),
        singleflight=bool(cfg.get("singleflight", True)),
        semantic_cache=bool(cfg.get("semantic_cache", False)),
        semantic_threshold=float(cfg.get("semantic_threshold", 0.92)),
        max_retries=int(max_retries),
        retry_backoff=parse_ttl(cfg.get("retry_backoff")) or 0.0,
        approval_required=bool(cfg.get("approval_required", False)),
        approval_webhook=cfg.get("approval_webhook"),
        approval_timeout=parse_ttl(cfg.get("approval_timeout")) or 3600.0,
        approval_bypass_arg=cfg.get("approval_bypass_arg"),
        approval_bypass_max=(
            float(cfg["approval_bypass_max"]) if cfg.get("approval_bypass_max") is not None else None
        ),
        rate_limit_max_calls=(int(rate_limit["max_calls"]) if rate_limit.get("max_calls") else None),
        rate_limit_window=parse_ttl(rate_limit.get("per")),
        max_concurrent=(int(cfg["max_concurrent"]) if cfg.get("max_concurrent") else None),
        concurrency_wait_timeout=parse_ttl(cfg.get("concurrency_wait_timeout")) or 300.0,
        budget_arg=budget.get("arg"),
        budget_max=(float(budget["max"]) if budget.get("max") is not None else None),
        budget_window=parse_ttl(budget.get("per")),
        lease_timeout=parse_ttl(cfg.get("lease_timeout")),
        execution_timeout=parse_ttl(cfg.get("execution_timeout")),
        redact_args=list(cfg.get("redact_args") or []),
        redact_patterns=list(cfg.get("redact_patterns") or []),
        redact_auto=bool(cfg.get("redact_auto", False)),
    )


def load_policies(path: Optional[str] = None) -> Dict[str, Policy]:
    """Start from DEFAULT_POLICIES, then let a YAML file override or add policies by name.

    Returns fresh Policy objects, deep-copied from the defaults, not shared
    references. Every TbayClient (and every test) mutates its own copy of
    `client.policies[...]`; none of them can accidentally corrupt the module-
    level DEFAULT_POLICIES or another client's policies by doing so.
    """
    policies = {name: copy.deepcopy(pol) for name, pol in DEFAULT_POLICIES.items()}
    if path is None:
        return policies
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    for name, cfg in (data.get("policies") or {}).items():
        policies[name] = _build_policy(name, cfg or {})
    return policies
