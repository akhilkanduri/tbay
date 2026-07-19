"""execution_timeout must give up ON TIME. Historically the sync path used
ThreadPoolExecutor as a context manager, whose __exit__ joins the worker
thread -- so a 0.2s timeout on a 2s hang still blocked for the full 2s."""
import time

import pytest

from tbay import guarded
from tbay.exceptions import ExecutionTimeout
from tbay.policy import Policy


def test_sync_timeout_returns_promptly(client):
    client.policies["quick"] = Policy(name="quick", idempotent=False, singleflight=False,
                                      execution_timeout=0.2)

    @guarded(client, policy="quick")
    def slow() -> dict:
        time.sleep(2.0)
        return {"never": "returned in time"}

    started = time.time()
    with pytest.raises(ExecutionTimeout):
        slow()
    elapsed = time.time() - started
    assert elapsed < 1.0, f"timeout took {elapsed:.2f}s to fire; it must not wait for the hung call"
    # and the execution is on record as FAILED
    record = client.backend.list_executions(tool_name="slow", limit=1)[0]
    assert record.status == "FAILED"
    assert "did not finish" in record.error
