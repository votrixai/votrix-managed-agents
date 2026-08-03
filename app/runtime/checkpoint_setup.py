"""Initialize LangGraph's PostgreSQL checkpoint schema during deployment."""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.runtime.engine import _configure_checkpoint_schema, _postgres_dsn


async def setup_checkpoint_database() -> None:
    """Run LangGraph's idempotent checkpoint migrations before serving turns."""

    settings = get_settings()
    url = settings.vma_checkpoint_database_url.strip() or str(settings.database_url)
    if not url.startswith(("postgres://", "postgresql://", "postgresql+")):
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    connection = AsyncPostgresSaver.from_conn_string(
        _postgres_dsn(url, settings.database_schema)
    )
    saver = await connection.__aenter__()
    try:
        await _configure_checkpoint_schema(saver, settings.database_schema)
        await saver.setup()
    finally:
        await connection.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(setup_checkpoint_database())
