from app.config import get_settings
from app.runtime.checkpoints import (
    _postgres_dsn,
    _shared_postgres_saver,
    checkpoint_saver,
    close_checkpoint_saver,
)


async def test_sqlite_checkpoint_saver_initializes_separate_database(tmp_path, monkeypatch):
    app_database = tmp_path / "control-plane.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{app_database}")
    monkeypatch.delenv("VMA_CHECKPOINT_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "AsyncSqliteSaver"

    assert (tmp_path / "control-plane.checkpoints.sqlite3").exists()


async def test_sqlite_checkpoint_saver_remains_fresh_per_context(tmp_path, monkeypatch):
    app_database = tmp_path / "control-plane.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{app_database}")
    monkeypatch.delenv("VMA_CHECKPOINT_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    async with checkpoint_saver() as first:
        first_type = first.__class__.__name__
    async with checkpoint_saver() as second:
        second_type = second.__class__.__name__

    assert first_type == second_type == "AsyncSqliteSaver"
    assert first is not second


def test_postgres_checkpoint_dsn_is_derived_from_sqlalchemy_url():
    database_url = "postgresql+asyncpg://user:password@db.example.com:5432/vma"

    assert _postgres_dsn(database_url) == "postgresql://user:password@db.example.com:5432/vma"


async def test_in_memory_checkpoint_saver_is_explicit(monkeypatch):
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "memory://")
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "InMemorySaver"


async def test_postgres_checkpoint_pool_disables_server_prepared_statements(monkeypatch):
    from langgraph.checkpoint.postgres import aio as checkpoint_aio
    import psycopg_pool

    captured: dict = {}

    class FakePool:
        def __init__(self, dsn, **kwargs) -> None:
            captured["dsn"] = dsn
            captured.update(kwargs)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeSaver:
        def __init__(self, pool) -> None:
            self.pool = pool

        async def setup(self) -> None:
            return None

    await close_checkpoint_saver()
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(checkpoint_aio, "AsyncPostgresSaver", FakeSaver)
    try:
        await _shared_postgres_saver("postgresql://checkpoint.example:6543/vma")
    finally:
        await close_checkpoint_saver()

    assert captured["kwargs"]["prepare_threshold"] is None
