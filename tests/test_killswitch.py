"""The kill switch: `pause()` stops calls before anything runs, across every
process sharing the database, and `resume()` lifts it."""
import pytest

from tbay import TbayClient, ToolPaused, guarded
from tbay.events import KILL_SWITCH_BLOCKED


def make_tool(client, calls, policy="mutating"):
    @guarded(client, policy=policy)
    def send_email(to: str) -> dict:
        calls.append(to)
        return {"sent": to}

    return send_email


def test_global_pause_blocks_everything(client):
    calls = []
    tool = make_tool(client, calls)
    client.pause(reason="incident 4711", by="oncall")
    with pytest.raises(ToolPaused) as exc:
        tool("a@example.com")
    assert "incident 4711" in str(exc.value)
    assert calls == []  # the function never ran


def test_per_tool_pause_blocks_only_that_tool(client):
    calls = []
    tool = make_tool(client, calls)

    @guarded(client, policy="mutating")
    def other_tool(x: str) -> dict:
        return {"ok": x}

    client.pause("send_email")
    with pytest.raises(ToolPaused):
        tool("a@example.com")
    assert other_tool("fine") == {"ok": "fine"}


def test_resume_lifts_the_pause(client):
    calls = []
    tool = make_tool(client, calls)
    client.pause()
    with pytest.raises(ToolPaused):
        tool("a@example.com")
    client.resume()
    assert tool("a@example.com") == {"sent": "a@example.com"}
    assert calls == ["a@example.com"]


def test_pause_is_shared_across_clients(client, tmp_path):
    """A second client on the same database sees the pause: this is what
    makes `tbay pause` from a terminal stop a running agent elsewhere."""
    calls = []
    tool = make_tool(client, calls)
    other = TbayClient(f"sqlite:///{tmp_path / 'tbay.sqlite'}", poll_interval=0.02)
    other.pause(reason="stop everything")
    with pytest.raises(ToolPaused):
        tool("a@example.com")


def test_paused_listing_and_event(client):
    seen = []
    client.on(lambda e: seen.append(e), events=[KILL_SWITCH_BLOCKED])
    client.pause("send_email", reason="too spammy")
    listing = client.paused()
    assert set(listing) == {"send_email"}
    assert listing["send_email"]["reason"] == "too spammy"

    tool = make_tool(client, [])
    with pytest.raises(ToolPaused):
        tool("a@example.com")
    assert len(seen) == 1
    assert seen[0].tool_name == "send_email"
    assert seen[0].data["reason"] == "too spammy"


def test_pause_survives_clear(client):
    client.pause(reason="hold")
    client.backend.clear()
    assert "*" in client.paused()


def test_pause_pg(pg_client):
    tool = make_tool(pg_client, [])
    pg_client.pause("send_email", reason="pg pause")
    try:
        with pytest.raises(ToolPaused):
            tool("a@example.com")
        assert pg_client.paused()["send_email"]["reason"] == "pg pause"
    finally:
        pg_client.resume("send_email")


def test_pause_redis(redis_client):
    tool = make_tool(redis_client, [])
    redis_client.pause(reason="redis pause")
    with pytest.raises(ToolPaused):
        tool("a@example.com")
    redis_client.resume()
    assert tool("a@example.com") == {"sent": "a@example.com"}


def test_pause_blocks_async_calls(client):
    import asyncio

    @guarded(client, policy="mutating")
    async def async_tool(x: str) -> dict:
        return {"ok": x}

    client.pause()
    with pytest.raises(ToolPaused):
        asyncio.run(async_tool("nope"))
