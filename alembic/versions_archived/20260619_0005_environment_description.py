"""add environment description

Revision ID: 20260619_0005
Revises: 20260619_0004
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "20260619_0005"
down_revision = "20260619_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environments",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("environments", "description")
