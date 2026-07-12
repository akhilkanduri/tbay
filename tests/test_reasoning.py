"""Reasoning-trace audit: `with reasoning("...")` attaches the agent's stated
justification to every guarded call made inside the block, and it lands in
the execution record where `tbay log` can show it."""
import asyncio

from tbay import guarded, reasoning


def test_reasoning_recorded_in_audit_log(client):
    @guarded(client, policy="mutating")
    def create_ticket(title: str) -> dict:
        return {"title": title}

    with reasoning("user asked to escalate the outage"):
        create_ticket("prod outage")

    records = client.backend.list_executions(tool_name="create_ticket")
    assert records[0].reasoning == "user asked to escalate the outage"


def test_no_reasoning_block_stores_none(client):
    @guarded(client, policy="mutating")
    def create_ticket(title: str) -> dict:
        return {"title": title}

    create_ticket("routine task")

    records = client.backend.list_executions(tool_name="create_ticket")
    assert records[0].reasoning is None


def test_reasoning_blocks_nest(client):
    @guarded(client, policy="mutating")
    def act(step: str) -> dict:
        return {"step": step}

    with reasoning("outer plan"):
        act("outer")
        with reasoning("inner detail"):
            act("inner")
        act("outer again")

    by_args = {r.args_json: r.reasoning for r in client.backend.list_executions(tool_name="act")}
    assert by_args['{"step": "outer"}'] == "outer plan"
    assert by_args['{"step": "inner"}'] == "inner detail"
    assert by_args['{"step": "outer again"}'] == "outer plan"


def test_reasoning_applies_to_volatile_calls(client):
    @guarded(client, policy="volatile")
    def llm_decide(prompt: str) -> dict:
        return {"decision": "yes"}

    with reasoning("choosing next action"):
        llm_decide("should we retry?")

    records = client.backend.list_executions(tool_name="llm_decide")
    assert records[0].reasoning == "choosing next action"


def test_reasoning_isolated_across_async_tasks(client):
    @guarded(client, policy="volatile")
    async def act(task: str) -> dict:
        await asyncio.sleep(0.01)
        return {"task": task}

    async def worker(task):
        with reasoning(f"reason for {task}"):
            await act(task)

    async def main():
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(main())

    reasons = {r.args_json: r.reasoning for r in client.backend.list_executions(tool_name="act")}
    assert reasons['{"task": "a"}'] == "reason for a"
    assert reasons['{"task": "b"}'] == "reason for b"
