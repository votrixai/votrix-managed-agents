from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# LangGraph keeps the agent's own memory in this database and creates these
# tables itself, on first use. They are not in our models, so autogenerate sees
# them as tables it should drop — which would erase every conversation the
# agents have had. They are not ours to migrate either way.
FOREIGN_TABLES = {
    "alembic_version",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


def include_name(name, type_, parent_names) -> bool:
    if type_ == "table":
        return name not in FOREIGN_TABLES
    return True


def run_migrations_offline() -> None:
    settings = get_settings()
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        version_table_schema=settings.database_schema or None,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    schema = get_settings().database_schema or None
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        version_table_schema=schema,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = get_settings()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url
    connect_args = {}
    if settings.database_schema and settings.database_url.startswith("postgresql+asyncpg"):
        connect_args["server_settings"] = {"search_path": settings.database_schema}
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async with connectable.begin() as connection:
        if settings.database_schema:
            await connection.execute(
                text(f'CREATE SCHEMA IF NOT EXISTS "{settings.database_schema}"')
            )
            await connection.execute(
                text(f'SET search_path TO "{settings.database_schema}"')
            )
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
