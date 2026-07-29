"""add runtime query indexes

Revision ID: 20260619_0004
Revises: 20260619_0003
Create Date: 2026-06-19
"""

from alembic import op

revision = "20260619_0004"
down_revision = "20260619_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_managed_resources_type_parent_status",
        "managed_resources",
        ["resource_type", "parent_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_managed_resources_type_status_created",
        "managed_resources",
        ["resource_type", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_managed_resources_storage_backend",
        "managed_resources",
        ["storage_backend"],
        unique=False,
    )
    op.create_index(
        "ix_managed_resources_sha256",
        "managed_resources",
        ["sha256"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_environment_status",
        "sessions",
        ["environment_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_agent_status",
        "sessions",
        ["agent_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_session_events_session_type",
        "session_events",
        ["session_id", "type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_events_session_type", table_name="session_events")
    op.drop_index("ix_sessions_agent_status", table_name="sessions")
    op.drop_index("ix_sessions_environment_status", table_name="sessions")
    op.drop_index("ix_managed_resources_sha256", table_name="managed_resources")
    op.drop_index("ix_managed_resources_storage_backend", table_name="managed_resources")
    op.drop_index("ix_managed_resources_type_status_created", table_name="managed_resources")
    op.drop_index("ix_managed_resources_type_parent_status", table_name="managed_resources")
