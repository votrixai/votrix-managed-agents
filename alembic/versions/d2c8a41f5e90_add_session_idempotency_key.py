"""Add tenant-scoped idempotency keys to File and Session creation.

Revision ID: d2c8a41f5e90
Revises: c1e7a3b58f42
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "d2c8a41f5e90"
down_revision = "c1e7a3b58f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "uq_files_organization_idempotency_key",
        "files",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.add_column(
        "sessions",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "uq_sessions_organization_idempotency_key",
        "sessions",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sessions_organization_idempotency_key",
        table_name="sessions",
    )
    op.drop_column("sessions", "idempotency_key")
    op.drop_index(
        "uq_files_organization_idempotency_key",
        table_name="files",
    )
    op.drop_column("files", "idempotency_key")
