"""add managed resource name lookup index

Revision ID: 20260619_0006
Revises: 20260619_0005
Create Date: 2026-06-19
"""

from alembic import op

revision = "20260619_0006"
down_revision = "20260619_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_managed_resources_type_parent_name",
        "managed_resources",
        ["resource_type", "parent_id", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_managed_resources_type_parent_name", table_name="managed_resources")
