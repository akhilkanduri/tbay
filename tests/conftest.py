import os

import pytest

from tbay import TbayClient

PG_DSN = os.environ.get("TBAY_TEST_PG_DSN")
REDIS_URL = os.environ.get("TBAY_TEST_REDIS_URL")


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "tbay.sqlite"
    return TbayClient(f"sqlite:///{db_path}", poll_interval=0.02)


@pytest.fixture
def pg_client():
    """Same as `client`, but backed by a real Postgres database instead of a
    fresh SQLite file. Set TBAY_TEST_PG_DSN to run these; CI does this with
    a postgres service container. Skipped otherwise, since not everyone has
    a Postgres server sitting around for local test runs."""
    if not PG_DSN:
        pytest.skip("set TBAY_TEST_PG_DSN to run Postgres-backed tests")
    return TbayClient(PG_DSN, poll_interval=0.02)


@pytest.fixture
def redis_client():
    """Same as `client`, but backed by a real Redis instead of a fresh SQLite
    file. Set TBAY_TEST_REDIS_URL to run these; CI does this with a redis
    service container. Each test gets a unique key prefix, so runs never
    collide with each other or with anything else in that Redis."""
    if not REDIS_URL:
        pytest.skip("set TBAY_TEST_REDIS_URL to run Redis-backed tests")
    import uuid

    client = TbayClient(REDIS_URL, poll_interval=0.02)
    client.backend._p = f"tbay-test-{uuid.uuid4().hex[:8]}:"
    return client
