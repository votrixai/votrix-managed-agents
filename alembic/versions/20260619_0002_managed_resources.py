"""add generic managed resources

Revision ID: 20260619_0002
Revises: 20260619_0001
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "20260619_0002"
down_revision = "20260619_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_resources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("filename", sa.String(length=1024), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_type",
            "parent_id",
            "version",
            name="uq_managed_resources_type_parent_version",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "resource_type",
            "parent_id",
            "version",
            name="uq_managed_resources_workspace_type_parent_version",
        ),
    )
    op.create_index(op.f("ix_managed_resources_workspace_id"), "managed_resources", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_managed_resources_resource_type"), "managed_resources", ["resource_type"], unique=False)
    op.create_index(op.f("ix_managed_resources_parent_id"), "managed_resources", ["parent_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_managed_resources_parent_id"), table_name="managed_resources")
    op.drop_index(op.f("ix_managed_resources_resource_type"), table_name="managed_resources")
    op.drop_index(op.f("ix_managed_resources_workspace_id"), table_name="managed_resources")
    op.drop_table("managed_resources")
