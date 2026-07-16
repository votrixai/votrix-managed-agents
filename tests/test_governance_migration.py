from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_governance_migration_upgrades_enforces_ledgers_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "governance-migration.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "organization_quotas",
            "organization_quota_counters",
            "organization_quota_reservations",
            "audit_ledger",
            "usage_ledger",
            "tenant_idempotency",
        } <= tables
        assert "organizations" in tables
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone() == (0,)
        assert {
            "trg_audit_ledger_append_only_update",
            "trg_audit_ledger_append_only_delete",
            "trg_usage_ledger_append_only_update",
            "trg_usage_ledger_append_only_delete",
        } <= triggers
        assert "ix_tenant_idempotency_state_updated" in indexes

        connection.execute(
            """
            INSERT INTO audit_ledger
                (id, organization_id, actor_type, action, outcome, data, occurred_at)
            VALUES
                ('audit_1', 'org_test', 'system', 'migration.test', 'success', '{}', CURRENT_TIMESTAMP)
            """
        )
        try:
            connection.execute(
                "UPDATE audit_ledger SET outcome = 'mutated' WHERE id = 'audit_1'"
            )
        except sqlite3.IntegrityError as error:
            assert "append-only" in str(error)
        else:  # pragma: no cover - a missing trigger is the assertion failure
            raise AssertionError("audit_ledger UPDATE was not rejected")
        connection.rollback()
        connection.execute(
            """
            INSERT INTO usage_ledger
                (id, organization_id, metric, quantity, unit, dimensions, data, occurred_at)
            VALUES
                ('usage_1', 'org_test', 'tokens', 1, 'token', '{}', '{}', CURRENT_TIMESTAMP)
            """
        )
        try:
            connection.execute("DELETE FROM usage_ledger WHERE id = 'usage_1'")
        except sqlite3.IntegrityError as error:
            assert "append-only" in str(error)
        else:  # pragma: no cover - a missing trigger is the assertion failure
            raise AssertionError("usage_ledger DELETE was not rejected")

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260714_0014"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
    assert "organization_quotas" not in remaining
    assert "audit_ledger" not in remaining
    assert not any(name.startswith("trg_audit_ledger") for name in remaining)

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        tables_after_base = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "organizations" not in tables_after_base
    assert "managed_resources" not in tables_after_base
