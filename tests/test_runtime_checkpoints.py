from app.config import get_settings
from app.runtime.checkpoints import checkpoint_saver


async def test_sqlite_checkpoint_saver_initializes_separate_database(tmp_path, monkeypatch):
    app_database = tmp_path / "control-plane.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{app_database}")
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "")
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "AsyncSqliteSaver"

    assert (tmp_path / "control-plane.checkpoints.sqlite3").exists()


async def test_in_memory_checkpoint_saver_is_explicit(monkeypatch):
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "memory://")
    get_settings.cache_clear()

    async with checkpoint_saver() as saver:
        assert saver.__class__.__name__ == "InMemorySaver"
