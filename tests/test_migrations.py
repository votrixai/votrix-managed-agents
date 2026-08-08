import sqlite3
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import clear_settings_cache


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "b7e1c4d9a620"
REWIRED_RELEASE_HEAD = "b7f2d4a91c53"
CURRENT_HEAD = "e3a14b7c9d52"


def test_member_migration_preserves_existing_owner_and_has_one_head(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "migration.sqlite"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    clear_settings_cache()

    config = Config(REPOSITORY_ROOT / "alembic.ini")
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "alembic"),
    )
    assert ScriptDirectory.from_config(config).get_heads() == [CURRENT_HEAD]

    try:
        command.upgrade(config, PREVIOUS_HEAD)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO organizations
                    (id, slug, name, created_at, updated_at)
                VALUES
                    (?, ?, ?, ?, ?)
                """,
                (
                    "org_votrix_staging",
                    "votrix-staging",
                    "Votrix Staging",
                    "2026-08-02 01:02:03+00:00",
                    "2026-08-02 04:05:06+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO organization_owners
                    (id, organization_id, user_id, email, created_at, updated_at)
                VALUES
                    (?, ?, ?, ?, ?, ?)
                """,
                (
                    "owner_cc5829abf6074f59a8af4347cbe90a8d",
                    "org_votrix_staging",
                    "632f9024-65f8-4749-b151-e628de33f2b0",
                    "cosmobiosis@gmail.com",
                    "2026-08-02 07:08:09+00:00",
                    "2026-08-02 10:11:12+00:00",
                ),
            )

        command.upgrade(config, "head")
        with sqlite3.connect(database_path) as connection:
            member = connection.execute(
                """
                SELECT id, organization_id, user_id, email, role,
                       created_at, updated_at
                FROM organization_members
                """
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
        assert member == (
            "owner_cc5829abf6074f59a8af4347cbe90a8d",
            "org_votrix_staging",
            "632f9024-65f8-4749-b151-e628de33f2b0",
            "cosmobiosis@gmail.com",
            "owner",
            "2026-08-02 07:08:09+00:00",
            "2026-08-02 10:11:12+00:00",
        )
        assert "organization_owners" not in tables
        assert {"organization_members", "vma_api_keys"} <= tables

        command.downgrade(config, PREVIOUS_HEAD)
        with sqlite3.connect(database_path) as connection:
            restored_owner = connection.execute(
                """
                SELECT id, organization_id, user_id, email,
                       created_at, updated_at
                FROM organization_owners
                """
            ).fetchone()
        assert restored_owner == member[:4] + member[5:]
    finally:
        clear_settings_cache()


def test_postgres_offline_downgrade_emits_role_guard(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://unused:unused@localhost/unused",
    )
    monkeypatch.setenv("DATABASE_SCHEMA", "vma_rewrite_staging")
    clear_settings_cache()

    output = StringIO()
    config = Config(REPOSITORY_ROOT / "alembic.ini", output_buffer=output)
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "alembic"),
    )
    try:
        command.downgrade(
            config,
            f"{CURRENT_HEAD}:{PREVIOUS_HEAD}",
            sql=True,
        )
    finally:
        clear_settings_cache()

    sql = output.getvalue()
    assert 'SET search_path TO "vma_rewrite_staging"' in sql
    assert "DO $$" in sql
    assert "WHERE role <> 'owner'" in sql
    assert "RENAME CONSTRAINT organization_members_pkey" in sql


def test_reconciliation_repairs_database_stamped_by_rewired_history(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "rewired-release.sqlite"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    clear_settings_cache()

    config = Config(REPOSITORY_ROOT / "alembic.ini")
    config.set_main_option(
        "script_location",
        str(REPOSITORY_ROOT / "alembic"),
    )

    try:
        # The old release reached this schema, then recorded the later revision
        # IDs through a graph that did not contain the three inserted migrations.
        command.upgrade(config, "d4f7a9c2e106")
        command.stamp(config, REWIRED_RELEASE_HEAD)
        command.upgrade(config, "head")

        with sqlite3.connect(database_path) as connection:
            session_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            unique_environment_indexes = set()
            for index in connection.execute("PRAGMA index_list(environments)"):
                if not index[2]:
                    continue
                columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f'PRAGMA index_info("{index[1]}")'
                    )
                )
                unique_environment_indexes.add(columns)
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()

        assert revision == (CURRENT_HEAD,)
        assert "model" in session_columns
        # The repair must not turn the skipped cleanup migration into an
        # unannounced destructive operation on an already-running database.
        assert "memories" in tables
        assert "memory_versions" in tables
        assert ("organization_id", "name") not in unique_environment_indexes
    finally:
        clear_settings_cache()
