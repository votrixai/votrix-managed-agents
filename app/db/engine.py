from contextlib import asynccontextmanager
from time import monotonic
from typing import AsyncGenerator

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, PoolProxiedConnection

from app.config import get_settings
from app.utils.timing import report

logger = structlog.get_logger(__name__)

SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS = 0.250

# How much of a statement goes into the log. Enough to tell one query from
# another; never the parameters, which are the half that carries user data.
STATEMENT_PREVIEW_CHARS = 120

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class _ReportsCheckout:
    """Timing shared by both pool classes.

    Which pool is in use decides what a checkout costs, and the two are not
    remotely alike: a queue pool usually hands back a connection it already
    had, while `NullPool` opens a new one to Postgres every single time —
    which, over the network, is most of a second. Both are reported through
    here so the logs can be compared without knowing which is configured.
    """

    def connect(self) -> PoolProxiedConnection:
        started_at = monotonic()
        try:
            connection = super().connect()
        except Exception as exc:
            self._report_checkout(
                started_at, outcome="error", error_type=type(exc).__name__
            )
            raise
        self._report_checkout(started_at, outcome="acquired")
        return connection

    def _report_checkout(
        self,
        started_at: float,
        *,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        fields = {"outcome": outcome, "pool": type(self).__name__}
        if error_type is not None:
            fields["error_type"] = error_type
        report("database_connection_acquired", started_at, **fields)

        elapsed_seconds = monotonic() - started_at
        if elapsed_seconds < SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS:
            return
        logger.warning(
            "database_pool_checkout_slow",
            checkout_latency_ms=round(elapsed_seconds * 1000, 3),
            threshold_ms=int(SLOW_POOL_CHECKOUT_THRESHOLD_SECONDS * 1000),
            outcome=outcome,
            **self._pool_state(),
        )

    def _pool_state(self) -> dict:
        return {}


class ObservedAsyncAdaptedQueuePool(_ReportsCheckout, AsyncAdaptedQueuePool):
    """A pooled connection, with the wait to get hold of one reported."""

    def _pool_state(self) -> dict:
        return {
            "pool_size": self.size(),
            "checked_out": self.checkedout(),
            "overflow_connections": max(self.overflow(), 0),
        }


class ObservedNullPool(_ReportsCheckout, NullPool):
    """No pooling at all, with the cost of that reported.

    `NullPool` opens a connection per session and closes it after, so this
    number is not queue wait — it is a TCP connect, a TLS handshake and an
    authentication round trip, every time. On hosted Postgres that is the
    single largest fixed cost in the service, and until now nothing measured
    it: the only instrumentation lived on the queue pool, which this
    deployment never builds.
    """


def _install_statement_timing(engine) -> None:
    """Time every statement, on the way out and the way back.

    A statement's start and end are two different callbacks, so the start is
    parked on the connection — a stack rather than a single value, because
    SQLAlchemy can nest an execution inside another on the same connection.

    Only the statement text is logged, truncated, and never the parameters:
    the text is SQL this repository wrote, the parameters are whatever the
    user typed.
    """

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _started(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("statement_started_at", []).append(monotonic())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _finished(conn, cursor, statement, parameters, context, executemany):
        stack = conn.info.get("statement_started_at")
        if not stack:
            return
        report(
            "database_statement",
            stack.pop(),
            operation=statement.split(maxsplit=1)[0].upper() if statement else "?",
            statement=" ".join(statement.split())[:STATEMENT_PREVIEW_CHARS],
            many=bool(executemany),
        )


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
        _install_statement_timing(_engine)
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

    SQLite and non-Postgres development backends keep their historical
    connection-per-session behaviour. `VMA_DB_POOL_SIZE=0` is the escape hatch
    for a Postgres deployment whose external pooler should own every
    connection lifecycle.

    The settings are read as attributes rather than through `getattr` with a
    default. That is the whole of the previous bug: none of these fields
    existed on `Settings`, so every lookup fell through to its default, the
    size was always zero, and every deployment silently ran with no pool at
    all — including the one place that then logged how slow the pool was.
    """

    pool_size = settings.vma_db_pool_size
    if not url.startswith(("postgres://", "postgresql://", "postgresql+")) or pool_size == 0:
        return {"poolclass": ObservedNullPool}
    return {
        "poolclass": ObservedAsyncAdaptedQueuePool,
        "pool_size": pool_size,
        "max_overflow": settings.vma_db_max_overflow,
        "pool_timeout": settings.vma_db_pool_timeout_seconds,
        "pool_recycle": settings.vma_db_pool_recycle_seconds,
        "pool_pre_ping": settings.vma_db_pool_pre_ping,
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
