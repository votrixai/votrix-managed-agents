from structlog.testing import capture_logs
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.config import Settings, get_settings
from app.db import engine as engine_module
from app.db.engine import (
    ObservedAsyncAdaptedQueuePool,
    _pool_options,
    session_scoped_database_url,
)


def test_hosted_postgres_pool_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.vma_db_pool_size == 10
    assert settings.vma_db_max_overflow == 5
    assert settings.vma_db_pool_timeout_seconds == 10.0
    assert settings.vma_db_pool_recycle_seconds == 300

    assert _pool_options("postgresql+asyncpg://user:pass@db.example/vma", settings) == {
        "poolclass": ObservedAsyncAdaptedQueuePool,
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
        "poolclass": ObservedAsyncAdaptedQueuePool,
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


def test_observed_pool_does_not_log_fast_checkout(monkeypatch) -> None:
    pool = ObservedAsyncAdaptedQueuePool(lambda: object(), pool_size=1)
    sentinel = object()
    timestamps = iter((10.0, 10.249))
    monkeypatch.setattr(AsyncAdaptedQueuePool, "connect", lambda _self: sentinel)
    monkeypatch.setattr(engine_module, "monotonic", lambda: next(timestamps))

    with capture_logs() as logs:
        assert pool.connect() is sentinel

    assert logs == []


def test_observed_pool_logs_structured_slow_checkout(monkeypatch) -> None:
    pool = ObservedAsyncAdaptedQueuePool(lambda: object(), pool_size=1)
    sentinel = object()
    timestamps = iter((20.0, 20.375))
    monkeypatch.setattr(AsyncAdaptedQueuePool, "connect", lambda _self: sentinel)
    monkeypatch.setattr(engine_module, "monotonic", lambda: next(timestamps))

    with capture_logs() as logs:
        assert pool.connect() is sentinel

    assert logs == [
        {
            "event": "database_pool_checkout_slow",
            "checkout_latency_ms": 375.0,
            "threshold_ms": 250,
            "outcome": "acquired",
            "pool_size": 1,
            "checked_out": 0,
            "overflow_connections": 0,
            "log_level": "warning",
        }
    ]


def test_observed_pool_logs_slow_checkout_error_and_reraises(monkeypatch) -> None:
    pool = ObservedAsyncAdaptedQueuePool(lambda: object(), pool_size=1)
    timestamps = iter((30.0, 30.5))

    def fail_checkout(_self):
        raise TimeoutError("pool unavailable")

    monkeypatch.setattr(AsyncAdaptedQueuePool, "connect", fail_checkout)
    monkeypatch.setattr(engine_module, "monotonic", lambda: next(timestamps))

    with capture_logs() as logs:
        try:
            pool.connect()
        except TimeoutError as exc:
            assert str(exc) == "pool unavailable"
        else:
            raise AssertionError("checkout error was not reraised")

    assert logs[0]["event"] == "database_pool_checkout_slow"
    assert logs[0]["checkout_latency_ms"] == 500.0
    assert logs[0]["outcome"] == "error"
    assert logs[0]["error_type"] == "TimeoutError"
    assert logs[0]["log_level"] == "warning"


def test_session_scoped_database_url_prefers_listen_dsn(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://transaction.example:6543/vma",
    )
    monkeypatch.setenv(
        "VMA_LISTEN_DATABASE_URL",
        "postgresql+asyncpg://session.example:5432/vma",
    )
    get_settings.cache_clear()

    assert session_scoped_database_url() == "postgresql+asyncpg://session.example:5432/vma"


def test_session_scoped_database_url_falls_back_to_main_dsn(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://local.example:5432/vma",
    )
    monkeypatch.setenv("VMA_LISTEN_DATABASE_URL", "")
    get_settings.cache_clear()

    assert session_scoped_database_url() == "postgresql+asyncpg://local.example:5432/vma"
