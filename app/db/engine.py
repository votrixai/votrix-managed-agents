from contextlib import asynccontextmanager
from time import monotonic
from typing import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, PoolProxiedConnection

from app.config import get_settings

logger = structlog.get_logger(__name__)

SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS = 0.250

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class ObservedAsyncAdaptedQueuePool(AsyncAdaptedQueuePool):
    """Report only materially slow end-to-end pool acquisitions.

    The elapsed interval includes queue wait, connection establishment, and
    SQLAlchemy checkout work such as pre-ping. Fast checkouts intentionally
    emit no log entry.
    """

    def connect(self) -> PoolProxiedConnection:
        started_at = monotonic()
        try:
            connection = super().connect()
        except Exception as exc:
            self._report_slow_checkout(
                started_at,
                outcome="error",
                error_type=type(exc).__name__,
            )
            raise
        self._report_slow_checkout(started_at, outcome="acquired")
        return connection

    def _report_slow_checkout(
        self,
        started_at: float,
        *,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        elapsed_seconds = monotonic() - started_at
        if elapsed_seconds < SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS:
            return

        fields = {
            "checkout_latency_ms": round(elapsed_seconds * 1000, 3),
            "threshold_ms": int(SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS * 1000),
            "outcome": outcome,
            "pool_size": self.size(),
            "checked_out": self.checkedout(),
            "overflow_connections": max(self.overflow(), 0),
        }
        if error_type is not None:
            fields["error_type"] = error_type
        logger.warning("database_pool_checkout_slow", **fields)


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        _engine = create_async_engine(
            url,
            connect_args=_connect_args(url),
            **_pool_options(url, settings),
        )
    return _engine


def _connect_args(url: str) -> dict:
    if not url.startswith("postgresql+asyncpg"):
        return {}
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "command_timeout": 30,
        "timeout": 10,
    }


def _pool_options(url: str, settings) -> dict:
    """Return bounded application-pool options for hosted Postgres.

    SQLite and non-Postgres development backends retain their historical
    connection-per-session behavior. Setting ``VMA_DB_POOL_SIZE=0`` provides
    the same explicit escape hatch for Postgres deployments whose external
    pooler should own every connection lifecycle.
    """

    pool_size = int(getattr(settings, "vma_db_pool_size", 0))
    if not url.startswith(("postgres://", "postgresql://", "postgresql+")) or pool_size == 0:
        return {"poolclass": NullPool}
    return {
        "poolclass": ObservedAsyncAdaptedQueuePool,
        "pool_size": pool_size,
        "max_overflow": int(getattr(settings, "vma_db_max_overflow", 0)),
        "pool_timeout": float(getattr(settings, "vma_db_pool_timeout_seconds", 10.0)),
        "pool_recycle": int(getattr(settings, "vma_db_pool_recycle_seconds", 300)),
        "pool_pre_ping": True,
    }


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session


async def reset_engine_for_tests() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
