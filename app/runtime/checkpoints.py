"""Durable LangGraph checkpointer selection.

Postgres is used in production when DATABASE_URL points at Postgres. SQLite gets a
separate checkpoint database so LangGraph migrations do not mix with SQLAlchemy's
application schema. An in-memory saver is available only through an explicit URL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import get_settings


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
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = _postgres_dsn(database_url)
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            yield saver
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = _sqlite_checkpoint_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


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
