from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests
from app.runtime import sandbox_lifecycle


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the suite SQLite fixture for guarded PostgreSQL integration."""
    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    get_settings.cache_clear()
    await reset_engine_for_tests()
    yield
    await reset_engine_for_tests()
    get_settings.cache_clear()


async def test_postgres_sandbox_janitor_skips_when_advisory_lock_is_held(monkeypatch):
    calls: list[int] = []

    async def fake_cleanup(*, limit: int = 25) -> int:
        calls.append(limit)
        return 9

    monkeypatch.setattr(
        sandbox_lifecycle,
        "_cleanup_expired_session_sandboxes",
        fake_cleanup,
    )
    engine = get_engine()

    async with engine.connect() as lock_connection:
        acquired = (
            await lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": sandbox_lifecycle._JANITOR_LOCK_KEY},
            )
        ).scalar()
        assert acquired is True
        try:
            result = await sandbox_lifecycle.cleanup_expired_session_sandboxes(limit=11)
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": sandbox_lifecycle._JANITOR_LOCK_KEY},
            )

    assert result == 0
    assert calls == []
    assert await sandbox_lifecycle.cleanup_expired_session_sandboxes(limit=13) == 9
    assert calls == [13]
