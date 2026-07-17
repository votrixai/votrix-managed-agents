"""Durable LangGraph checkpointer selection.

Postgres is used in production when DATABASE_URL points at Postgres. SQLite gets a
separate checkpoint database so LangGraph migrations do not mix with SQLAlchemy's
application schema. An in-memory saver is available only through an explicit URL.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import get_settings


_pg_saver: Any | None = None
_pg_pool: Any | None = None
_pg_dsn: str | None = None
_pg_lock = asyncio.Lock()


@asynccontextmanager
async def checkpoint_saver() -> AsyncIterator[Any]:
    settings = get_settings()
    configured = getattr(settings, "vma_checkpoint_database_url", "")
    database_url = str(configured or getattr(settings, "database_url", ""))

    if database_url in {"memory", "memory://", ":memory:"}:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if database_url.startswith(("postgres://", "postgresql://", "postgresql+")):
        dsn = _postgres_dsn(database_url)
        saver = await _shared_postgres_saver(dsn)
        yield saver
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = _sqlite_checkpoint_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


async def _shared_postgres_saver(dsn: str) -> Any:
    global _pg_saver, _pg_pool, _pg_dsn

    if _pg_saver is not None and _pg_dsn == dsn:
        return _pg_saver

    async with _pg_lock:
        if _pg_saver is not None and _pg_dsn == dsn:
            return _pg_saver

        # A changed DSN is primarily useful in tests, but closing before
        # replacing the process singleton also prevents leaking the old pool.
        await close_checkpoint_saver()

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=int(get_settings().vma_checkpoint_pool_max_size),
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
        except BaseException:
            await pool.close()
            raise

        _pg_pool = pool
        _pg_saver = saver
        _pg_dsn = dsn

    return _pg_saver


async def close_checkpoint_saver() -> None:
    global _pg_saver, _pg_pool, _pg_dsn

    pool = _pg_pool
    _pg_saver = None
    _pg_pool = None
    _pg_dsn = None
    if pool is not None:
        await pool.close()


def _postgres_dsn(value: str) -> str:
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    for driver in ("+asyncpg", "+psycopg", "+psycopg_async"):
        value = value.replace(driver, "")
    return value


def _sqlite_checkpoint_path(value: str) -> Path:
    if value.startswith("sqlite+") or value.startswith("sqlite:"):
        raw = value.split("///", 1)[-1]
        app_path = Path("/" + raw) if value.startswith("sqlite") and ":////" in value else Path(raw)
        if app_path.name:
            return app_path.with_name(f"{app_path.stem}.checkpoints.sqlite3")
    return Path("./votrix_managed_agents.checkpoints.sqlite3")
