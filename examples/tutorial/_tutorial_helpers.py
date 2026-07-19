"""Shared plumbing for the tutorial scripts, kept deliberately tiny.

Every tutorial gets a fresh throwaway SQLite database so runs never
interfere with each other or with anything real, and a `banner()` helper
so the output reads like a narrated walkthrough.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from tbay import TbayClient


def fresh_client(**kwargs) -> TbayClient:
    """A TbayClient on a brand-new SQLite file in a temp directory.

    poll_interval is turned way down (default 0.25s) so the tutorials'
    approval waits and singleflight follows feel instant.
    """
    db = Path(tempfile.mkdtemp(prefix="tbay-tutorial-")) / "tutorial.sqlite"
    return TbayClient(f"sqlite:///{db}", poll_interval=0.02, **kwargs)


def banner(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def step(text: str) -> None:
    print(f"\n--- {text}")
