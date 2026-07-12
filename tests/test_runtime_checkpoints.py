from app.config import get_settings
from app.runtime.checkpoints import _postgres_dsn, checkpoint_saver


async def test_sqlite_checkpoint_saver_initializes_separate_database(tmp_path, monkeypatch):
    app_database = tmp_path / "control-plane.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{app_database}")
    monkeypatch.delenv("VMA_CHECKPOINT_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "AsyncSqliteSaver"

    assert (tmp_path / "control-plane.checkpoints.sqlite3").exists()


def test_postgres_checkpoint_dsn_is_derived_from_sqlalchemy_url():
    database_url = "postgresql+asyncpg://user:password@db.example.com:5432/vma"

    assert _postgres_dsn(database_url) == "postgresql://user:password@db.example.com:5432/vma"


async def test_in_memory_checkpoint_saver_is_explicit(monkeypatch):
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "memory://")
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "InMemorySaver"
