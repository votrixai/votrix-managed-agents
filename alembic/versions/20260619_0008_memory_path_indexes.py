"""add memory path lookup indexes

Revision ID: 20260619_0008
Revises: 20260619_0007
Create Date: 2026-06-19
"""

from alembic import op

revision = "20260619_0008"
down_revision = "20260619_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_managed_resources_workspace_type_parent_deleted_name",
        "managed_resources",
        ["workspace_id", "resource_type", "parent_id", "deleted_at", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_managed_resources_workspace_type_parent_deleted_name", table_name="managed_resources")
