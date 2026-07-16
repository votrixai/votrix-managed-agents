"""add durable session event request idempotency

Revision ID: 20260713_0012
Revises: 20260713_0011
Create Date: 2026-07-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260713_0012"
down_revision = "20260713_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_event_idempotency",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("work_id", sa.String(length=64), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
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
            ["organization_id", "session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_session_event_idempotency_organization_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "session_id",
            "key_hash",
            name="uq_session_event_idempotency_organization_session_key",
        ),
    )
    op.create_index(
        "ix_session_event_idempotency_organization_session_created",
        "session_event_idempotency",
        ["organization_id", "session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_event_idempotency_organization_session_created",
        table_name="session_event_idempotency",
    )
    op.drop_table("session_event_idempotency")
