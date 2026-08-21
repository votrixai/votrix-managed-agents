import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import clear_settings_cache


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "b7e1c4d9a620"
REWIRED_RELEASE_HEAD = "b7f2d4a91c53"
CURRENT_HEAD = "c1b7e4d92a60"
PRE_BYOK_HEAD = "f6c8d2a91e74"


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


def test_byok_migration_renames_and_backfills_multi_provider_credentials(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "byok-migration.sqlite"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path}",
    )
    clear_settings_cache()

    config = Config(REPOSITORY_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    timestamp = "2026-08-20 00:00:00+00:00"

    try:
        command.upgrade(config, PRE_BYOK_HEAD)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO organizations (id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                ("org_existing", "Existing", timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO organization_accounts
                    (id, organization_id, name, status, is_default,
                     limit_usd, idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "acct_existing",
                    "org_existing",
                    "Existing Account",
                    "active",
                    1,
                    20,
                    None,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO account_provider_credentials
                    (id, account_id, organization_id, key_hash,
                     provider_key_name, encrypted_key, status, generation,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "acctcred_existing",
                    "acct_existing",
                    "org_existing",
                    "hash-existing",
                    "vma:test:existing",
                    "ciphertext",
                    "active",
                    1,
                    timestamp,
                    timestamp,
                ),
            )

        command.upgrade(config, "head")
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            funding = connection.execute(
                """
                SELECT funding_mode
                FROM organization_accounts WHERE id = 'acct_existing'
                """
            ).fetchone()
            existing_credential = connection.execute(
                """
                SELECT funding_mode, backend, provider_key_name
                FROM account_model_credentials
                WHERE id = 'acctcred_existing'
                """
            ).fetchone()
            credential_columns = {
                row[1]: row
                for row in connection.execute(
                    "PRAGMA table_info(account_model_credentials)"
                )
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            connection.execute(
                """
                INSERT INTO organization_accounts
                    (id, organization_id, name, status, is_default,
                     limit_usd, idempotency_key, funding_mode,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "acct_byok",
                    "org_existing",
                    "BYOK",
                    "active",
                    None,
                    None,
                    None,
                    "byok",
                    timestamp,
                    timestamp,
                ),
            )
            for backend in ("anthropic", "openai"):
                connection.execute(
                    """
                    INSERT INTO account_model_credentials
                        (id, account_id, funding_mode, backend, organization_id,
                         key_hash, provider_key_name, encrypted_key, status,
                         generation, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"acctcred_byok_{backend}",
                        "acct_byok",
                        "byok",
                        backend,
                        "org_existing",
                        f"byok:{backend}:digest",
                        None,
                        "ciphertext",
                        "active",
                        1,
                        timestamp,
                        timestamp,
                    ),
                )

            # Existing Platform rows cannot be reclassified underneath their
            # managed key: the composite FK makes funding mode part of the
            # relationship rather than two columns code merely keeps aligned.
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE organization_accounts
                    SET funding_mode = 'byok', limit_usd = NULL
                    WHERE id = 'acct_existing'
                    """
                )

            # A BYOK Account can have many direct backends, but OpenRouter is
            # Platform-only and a Platform Account still has exactly one key.
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO account_model_credentials
                        (id, account_id, funding_mode, backend, organization_id,
                         key_hash, provider_key_name, encrypted_key, status,
                         generation, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "acctcred_invalid_gateway_byok",
                        "acct_byok",
                        "byok",
                        "openrouter",
                        "org_existing",
                        "byok:openrouter:digest",
                        None,
                        "ciphertext",
                        "active",
                        1,
                        timestamp,
                        timestamp,
                    ),
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO account_model_credentials
                        (id, account_id, funding_mode, backend, organization_id,
                         key_hash, provider_key_name, encrypted_key, status,
                         generation, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "acctcred_second_platform",
                        "acct_existing",
                        "platform",
                        "openrouter",
                        "org_existing",
                        "hash-second-platform",
                        "vma:test:second",
                        "ciphertext",
                        "active",
                        1,
                        timestamp,
                        timestamp,
                    ),
                )

        assert funding == ("platform",)
        assert existing_credential == (
            "platform",
            "openrouter",
            "vma:test:existing",
        )
        assert credential_columns["provider_key_name"][3] == 0
        assert "account_model_credentials" in tables
        assert "account_provider_credentials" not in tables

        with pytest.raises(RuntimeError, match="BYOK Accounts exist"):
            command.downgrade(config, PRE_BYOK_HEAD)
    finally:
        clear_settings_cache()


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
