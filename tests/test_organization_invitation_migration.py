from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_invitation_migration_enforces_owner_and_allows_reinvitation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "organization-invitations.db"
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
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'organization_invitations'"
        ).fetchone()[0]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_organization_invitations_pending_email'"
        ).fetchone()[0]
        assert "ck_organization_invitations_role_owner" in table_sql
        assert "CHECK (role = 'owner')" in table_sql
        assert (
            "WHERE accepted_at IS NULL AND revoked_at IS NULL"
            in " ".join(index_sql.split())
        )

        connection.execute(
            "INSERT INTO organizations (id, slug, name, metadata) "
            "VALUES ('org_invite_migration', 'invite-migration', "
            "'Invite migration', '{}')"
        )
        connection.execute(
            """
            INSERT INTO organization_invitations
                (id, organization_id, email, token_hash, invited_by_user_id,
                 expires_at, accepted_at)
            VALUES
                ('invite_accepted', 'org_invite_migration', 'owner@example.com',
                 ?, 'user_superadmin', datetime('now', '+1 day'), CURRENT_TIMESTAMP)
            """,
            ("a" * 64,),
        )
        assert connection.execute(
            "SELECT role FROM organization_invitations WHERE id = 'invite_accepted'"
        ).fetchone() == ("owner",)

        connection.execute(
            """
            INSERT INTO organization_invitations
                (id, organization_id, email, token_hash, invited_by_user_id, expires_at)
            VALUES
                ('invite_pending', 'org_invite_migration', 'owner@example.com',
                 ?, 'user_superadmin', datetime('now', '+1 day'))
            """,
            ("b" * 64,),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM organization_invitations "
            "WHERE organization_id = 'org_invite_migration' "
            "AND email = 'owner@example.com'"
        ).fetchone() == (2,)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO organization_invitations
                    (id, organization_id, email, role, token_hash,
                     invited_by_user_id, expires_at)
                VALUES
                    ('invite_member', 'org_invite_migration', 'member@example.com',
                     'member', ?, 'user_superadmin', datetime('now', '+1 day'))
                """,
                ("c" * 64,),
            )
