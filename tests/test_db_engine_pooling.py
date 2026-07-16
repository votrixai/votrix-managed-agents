from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.engine import _pool_options


def test_hosted_postgres_pool_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.vma_db_pool_size == 10
    assert settings.vma_db_max_overflow == 5
    assert settings.vma_db_pool_timeout_seconds == 10.0
    assert settings.vma_db_pool_recycle_seconds == 300

    assert _pool_options("postgresql+asyncpg://user:pass@db.example/vma", settings) == {
        "pool_size": 10,
        "max_overflow": 5,
        "pool_timeout": 10.0,
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }


def test_postgres_pool_settings_are_configurable() -> None:
    settings = Settings(
        _env_file=None,
        vma_db_pool_size=16,
        vma_db_max_overflow=4,
        vma_db_pool_timeout_seconds=7.5,
        vma_db_pool_recycle_seconds=600,
    )

    assert _pool_options("postgresql://user:pass@db.example/vma", settings) == {
        "pool_size": 16,
        "max_overflow": 4,
        "pool_timeout": 7.5,
        "pool_recycle": 600,
        "pool_pre_ping": True,
    }


def test_postgres_pool_settings_load_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("VMA_DB_POOL_SIZE", "12")
    monkeypatch.setenv("VMA_DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("VMA_DB_POOL_TIMEOUT_SECONDS", "8.5")
    monkeypatch.setenv("VMA_DB_POOL_RECYCLE_SECONDS", "450")

    settings = Settings(_env_file=None)

    assert settings.vma_db_pool_size == 12
    assert settings.vma_db_max_overflow == 3
    assert settings.vma_db_pool_timeout_seconds == 8.5
    assert settings.vma_db_pool_recycle_seconds == 450


def test_zero_postgres_pool_size_opts_into_null_pool() -> None:
    settings = Settings(_env_file=None, vma_db_pool_size=0)

    assert _pool_options("postgresql+asyncpg://user:pass@db.example/vma", settings) == {
        "poolclass": NullPool,
    }


def test_sqlite_keeps_null_pool_even_with_hosted_defaults() -> None:
    settings = Settings(_env_file=None)

    assert _pool_options("sqlite+aiosqlite:///./test.db", settings) == {
        "poolclass": NullPool,
    }
