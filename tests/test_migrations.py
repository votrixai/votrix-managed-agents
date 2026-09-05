import sqlite3

import pytest
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import clear_settings_cache


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "b7e1c4d9a620"
REWIRED_RELEASE_HEAD = "b7f2d4a91c53"
CURRENT_HEAD = "d2c8a41f5e90"


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
            organization_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(organizations)")
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
        assert "slug" not in organization_columns

        command.downgrade(config, PREVIOUS_HEAD)
        with sqlite3.connect(database_path) as connection:
            restored_owner = connection.execute(
                """
                SELECT id, organization_id, user_id, email,
                       created_at, updated_at
                FROM organization_owners
                """
            ).fetchone()
            restored_slug = connection.execute(
                "SELECT slug FROM organizations WHERE id = ?",
                ("org_votrix_staging",),
            ).fetchone()
        assert restored_owner == member[:4] + member[5:]
        assert restored_slug == ("org_votrix_staging",)
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


MERGE_HEAD = "b8d4f1a92c73"
BEFORE_MERGE = "f6c8d2a91e74"


def test_the_sandbox_merge_keeps_live_rows_and_fills_in_what_is_new(
    tmp_path, monkeypatch
):
    """The rows in `session_sandboxes` are live, so they are carried, not rebuilt.

    `environment_id` is filled in from the session here because it cannot be
    filled in later: the session may be gone by then, and the point of the
    column is to answer which image a container ran on after the fact.
    """
    database_path = tmp_path / "merge.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    clear_settings_cache()

    config = Config(REPOSITORY_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))

    try:
        command.upgrade(config, BEFORE_MERGE)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "INSERT INTO organizations (id, name, created_at, updated_at)"
                " VALUES ('org_1', 'Acme', '2026-08-01', '2026-08-01')"
            )
            connection.execute(
                "INSERT INTO environments"
                " (id, organization_id, name, config, build_state,"
                "  created_at, updated_at)"
                " VALUES ('env_1', 'org_1', 'base', '{}', 'ready',"
                "         '2026-08-01', '2026-08-01')"
            )
            connection.execute(
                "INSERT INTO agents (id, organization_id, name, active_version,"
                " metadata, created_at, updated_at)"
                " VALUES ('agt_1', 'org_1', 'Victor', 1, '{}',"
                "         '2026-08-01', '2026-08-01')"
            )
            connection.execute(
                "INSERT INTO sessions (id, organization_id, agent_id, agent_version,"
                " environment_id, status, lock_version, last_event_seq,"
                " created_at, updated_at)"
                " VALUES ('sess_1', 'org_1', 'agt_1', 1, 'env_1', 'idle', 0, 0,"
                "         '2026-08-01', '2026-08-01')"
            )
            connection.execute(
                "INSERT INTO session_sandboxes (id, organization_id, session_id,"
                " provider, external_sandbox_id, state, lock_version,"
                " created_at, updated_at)"
                " VALUES ('sbx_1', 'org_1', 'sess_1', 'e2b', 'e2b-abc', 'running',"
                "         3, '2026-08-01', '2026-08-01')"
            )

        command.upgrade(config, MERGE_HEAD)

        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT id, session_id, environment_id, ttl_seconds,"
                " network_access, state, lock_version, external_sandbox_id"
                " FROM sandboxes"
            ).fetchone()
            assert row == ("sbx_1", "sess_1", "env_1", 900, 1, "running", 3, "e2b-abc")

            # The release still running when this lands keeps working.
            through_view = connection.execute(
                "SELECT id, state FROM session_sandboxes"
            ).fetchone()
            assert through_view == ("sbx_1", "running")

            # A container held through the API has no session, and any number
            # of those coexist — a plain unique constraint would have allowed
            # exactly one in the whole table.
            for suffix in ("a", "b"):
                connection.execute(
                    "INSERT INTO sandboxes (id, organization_id, session_id,"
                    " environment_id, provider, state, ttl_seconds,"
                    " network_access, lock_version, created_at, updated_at)"
                    f" VALUES ('sbx_{suffix}', 'org_1', NULL, 'env_1', 'e2b',"
                    "         'running', 300, 1, 0, '2026-08-01', '2026-08-01')"
                )

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO sandboxes (id, organization_id, session_id,"
                    " environment_id, provider, state, ttl_seconds,"
                    " network_access, lock_version, created_at, updated_at)"
                    " VALUES ('sbx_2', 'org_1', 'sess_1', 'env_1', 'e2b',"
                    "         'running', 300, 1, 0, '2026-08-01', '2026-08-01')"
                )

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO sandboxes (id, organization_id, session_id,"
                    " environment_id, provider, state, ttl_seconds,"
                    " network_access, lock_version, created_at, updated_at)"
                    " VALUES ('sbx_3', 'org_1', NULL, 'env_1', 'e2b',"
                    "         'expired', 300, 1, 0, '2026-08-01', '2026-08-01')"
                )
    finally:
        clear_settings_cache()
