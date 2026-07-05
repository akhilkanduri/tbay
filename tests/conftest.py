import os

import pytest

from tbay import TbayClient

PG_DSN = os.environ.get("TBAY_TEST_PG_DSN")


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
