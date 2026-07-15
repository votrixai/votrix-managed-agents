"""add session file lookup index

Revision ID: 20260713_0013
Revises: 20260713_0012
Create Date: 2026-07-13
"""

from alembic import op


revision = "20260713_0013"
down_revision = "20260713_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some long-lived development databases may already have this index from
    # metadata-driven bootstrap. Keep the release migration safe for both those
    # databases and fresh staging/production schemas.
    op.create_index(
        "ix_managed_resources_type_parent_name",
        "managed_resources",
        ["resource_type", "parent_id", "name"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_managed_resources_type_parent_name",
        table_name="managed_resources",
        if_exists=True,
    )
