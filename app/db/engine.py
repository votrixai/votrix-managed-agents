from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        connect_args = {}
        if url.startswith("postgresql+asyncpg"):
            connect_args = {
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "command_timeout": 30,
                "timeout": 10,
            }
        _engine = create_async_engine(
            url,
            connect_args=connect_args,
            **_pool_options(url, settings),
        )
    return _engine


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
