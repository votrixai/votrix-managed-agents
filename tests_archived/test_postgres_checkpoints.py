from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.runtime.checkpoints import checkpoint_saver, close_checkpoint_saver


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the default SQLite fixture for guarded checkpoint tests."""
    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", POSTGRES_URL)
    get_settings.cache_clear()
    await close_checkpoint_saver()
    yield
    await close_checkpoint_saver()
    get_settings.cache_clear()


async def test_postgres_checkpoint_saver_is_shared_and_setup_runs_once(monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    setup_calls = 0
    original_setup = AsyncPostgresSaver.setup

    async def counted_setup(self):
        nonlocal setup_calls
        setup_calls += 1
        return await original_setup(self)

    monkeypatch.setattr(AsyncPostgresSaver, "setup", counted_setup)

    async with checkpoint_saver() as first:
        async with checkpoint_saver() as second:
            assert second is first

    assert setup_calls == 1

    await close_checkpoint_saver()
    async with checkpoint_saver() as reopened:
        assert reopened is not first

    assert setup_calls == 2
