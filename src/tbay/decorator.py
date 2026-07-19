from __future__ import annotations

import functools
import inspect
from typing import Callable, Dict, Iterable, List, Optional, Union

from .client import TbayClient


def guarded(
    client: TbayClient,
    *,
    policy: str = "mutating",
    key_fn: Optional[Callable] = None,
    tenant: str = "",
    tool_name: Optional[str] = None,
):
    """Wrap a tool function with the idempotency, caching, singleflight, and
    approval behavior defined by `policy`.

    Works on both sync and async functions, and never inspects the caller's
    framework: it only ever sees a plain callable, so it drops straight into
    LangChain tools, OpenAI Agents SDK function tools, CrewAI tools, or bare
    functions, with no adapter code needed.

    `key_fn`, if given, computes the idempotency key from the call's args
    (e.g. `key_fn=lambda customer_id, **_: customer_id`). It's ignored for
    policies with `idempotent=False` (see the "volatile" policy), since
    those always run fresh and never need a dedup key at all.
    """

    def decorator(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                return await client.run_async(
                    fn, policy=policy, args=args, kwargs=kwargs, tenant=tenant, key_fn=key_fn, tool_name=name
                )

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            return client.run(
                fn, policy=policy, args=args, kwargs=kwargs, tenant=tenant, key_fn=key_fn, tool_name=name
            )

        return sync_wrapper

    return decorator


def guard_tools(
    client: TbayClient,
    tools: Union[Iterable[Callable], Dict[str, Callable]],
    *,
    policy: str = "mutating",
    tenant: str = "",
    key_fn: Optional[Callable] = None,
) -> Union[List[Callable], Dict[str, Callable]]:
    """Wrap a whole collection of tool functions under one policy at once.

    Handy when a framework hands you a list of tools (or you keep them in a
    registry dict) and they all share a risk tier:

        safe_tools = guard_tools(client, [search, fetch_page], policy="readonly")
        actions = guard_tools(client, {"refund": refund, "cancel": cancel}, policy="destructive")

    A dict input returns a dict wrapped under the same keys, and each key
    becomes that tool's recorded tool_name; an iterable input returns a list
    in the same order, using each function's own name. Tools that need
    *different* policies should keep using @guarded individually -- one
    policy for everything is exactly the kind of blunt setting this helper
    is for."""
    if isinstance(tools, dict):
        return {
            name: guarded(client, policy=policy, tenant=tenant, key_fn=key_fn, tool_name=name)(fn)
            for name, fn in tools.items()
        }
    return [guarded(client, policy=policy, tenant=tenant, key_fn=key_fn)(fn) for fn in tools]
