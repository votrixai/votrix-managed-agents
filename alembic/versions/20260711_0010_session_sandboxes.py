"""add tenant-scoped session sandbox lifecycle records

Revision ID: 20260711_0010
Revises: 20260619_0009
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20260711_0010"
down_revision = "20260619_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_sessions_workspace_id",
            ["workspace_id", "id"],
        )

    op.create_table(
        "session_sandboxes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_sandbox_id", sa.String(length=512), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=512), nullable=True),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_session_sandboxes_workspace_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "session_id",
            name="uq_session_sandboxes_workspace_session",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_sandbox_id",
            name="uq_session_sandboxes_provider_external",
        ),
    )
    op.create_index(
        op.f("ix_session_sandboxes_workspace_id"),
        "session_sandboxes",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_sandboxes_workspace_state_expires",
        "session_sandboxes",
        ["workspace_id", "state", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_session_sandboxes_provider_state_expires",
        "session_sandboxes",
        ["provider", "state", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_sandboxes_provider_state_expires",
        table_name="session_sandboxes",
    )
    op.drop_index(
        "ix_session_sandboxes_workspace_state_expires",
        table_name="session_sandboxes",
    )
    op.drop_index(
        op.f("ix_session_sandboxes_workspace_id"),
        table_name="session_sandboxes",
    )
    op.drop_table("session_sandboxes")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("uq_sessions_workspace_id", type_="unique")
