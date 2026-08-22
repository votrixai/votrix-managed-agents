#!/usr/bin/env python3
"""Rename the legacy environment-specific VMA schema before Alembic runs.

The hosted staging and production projects use separate databases, so both can
share the stable ``vma`` schema name.  This helper is deliberately guarded and
idempotent: it only renames the known legacy schema when the deployment already
targets ``vma`` and refuses to continue if both names exist.
"""

from __future__ import annotations

import os
from collections.abc import Collection

import psycopg
from psycopg import sql


TARGET_SCHEMA = "vma"
LEGACY_SCHEMA_BY_ENV = {
    "staging": "vma_rewrite_staging",
    "production": "vma_rewrite_production",
}


def schema_rename_action(
    *,
    app_env: str,
    configured_schema: str,
    existing_schemas: Collection[str],
) -> str:
    """Return the safe action for the observed schema state."""

    legacy_schema = LEGACY_SCHEMA_BY_ENV.get(app_env)
    if legacy_schema is None or configured_schema != TARGET_SCHEMA:
        return "skip"

    existing = set(existing_schemas)
    if legacy_schema in existing and TARGET_SCHEMA in existing:
        raise RuntimeError(
            f"refusing schema migration because both {legacy_schema!r} "
            f"and {TARGET_SCHEMA!r} exist"
        )
    if TARGET_SCHEMA in existing:
        return "already"
    if legacy_schema in existing:
        return "rename"
    return "fresh"


def _database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if not configured:
        raise RuntimeError("DATABASE_URL is required for the schema migration")
    return configured.replace("postgresql+asyncpg://", "postgresql://", 1)


def rename_legacy_schema() -> str:
    app_env = os.environ.get("APP_ENV", "").strip()
    configured_schema = os.environ.get("DATABASE_SCHEMA", "").strip()
    legacy_schema = LEGACY_SCHEMA_BY_ENV.get(app_env)

    if legacy_schema is None or configured_schema != TARGET_SCHEMA:
        print("Legacy VMA schema rename is not applicable.")
        return "skip"

    with psycopg.connect(_database_url(), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT nspname
                FROM pg_catalog.pg_namespace
                WHERE nspname IN (%s, %s)
                """,
                (legacy_schema, TARGET_SCHEMA),
            )
            existing_schemas = {row[0] for row in cursor.fetchall()}
            action = schema_rename_action(
                app_env=app_env,
                configured_schema=configured_schema,
                existing_schemas=existing_schemas,
            )

            if action == "rename":
                cursor.execute(
                    sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                        sql.Identifier(legacy_schema),
                        sql.Identifier(TARGET_SCHEMA),
                    )
                )

    if action == "rename":
        print(f"Renamed database schema {legacy_schema!r} to {TARGET_SCHEMA!r}.")
    elif action == "already":
        print(f"Database schema {TARGET_SCHEMA!r} is already active.")
    else:
        print(
            f"Neither {legacy_schema!r} nor {TARGET_SCHEMA!r} exists; "
            "Alembic will initialize a fresh schema."
        )
    return action


if __name__ == "__main__":
    rename_legacy_schema()
