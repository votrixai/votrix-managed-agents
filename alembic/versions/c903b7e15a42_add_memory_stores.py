"""add memory stores

Revision ID: c903b7e15a42
Revises: 71230f71b754
Create Date: 2026-07-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c903b7e15a42"
down_revision = "71230f71b754"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_stores",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "volume_provider",
            sa.String(length=32),
            server_default="e2b",
            nullable=False,
        ),
        sa.Column("volume_locator", sa.JSON(), nullable=True),
        sa.Column(
            "provisioning_status",
            sa.String(length=32),
            server_default="provisioning",
            nullable=False,
        ),
        sa.Column("provisioning_error", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "volume_provider IN ('e2b', 'r2')",
            name="ck_memory_stores_volume_provider",
        ),
        sa.CheckConstraint(
            "provisioning_status IN "
            "('provisioning', 'ready', 'failed', 'deleting', 'deleted')",
            name="ck_memory_stores_provisioning_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_memory_stores_organization_id"),
        "memory_stores",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_stores_organization_lifecycle_created",
        "memory_stores",
        ["organization_id", "deleted_at", "archived_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_stores_provider_provisioning_updated",
        "memory_stores",
        ["volume_provider", "provisioning_status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_stores_provider_provisioning_updated",
        table_name="memory_stores",
    )
    op.drop_index(
        "ix_memory_stores_organization_lifecycle_created",
        table_name="memory_stores",
    )
    op.drop_index(op.f("ix_memory_stores_organization_id"), table_name="memory_stores")
    op.drop_table("memory_stores")
