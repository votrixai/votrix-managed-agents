"""add resource storage metadata

Revision ID: 20260619_0003
Revises: 20260619_0002
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "20260619_0003"
down_revision = "20260619_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("managed_resources", sa.Column("storage_backend", sa.String(length=64), nullable=True))
    op.add_column("managed_resources", sa.Column("storage_key", sa.String(length=2048), nullable=True))
    op.add_column("managed_resources", sa.Column("storage_url", sa.String(length=4096), nullable=True))
    op.add_column("managed_resources", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column("managed_resources", sa.Column("sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("managed_resources", "sha256")
    op.drop_column("managed_resources", "size_bytes")
    op.drop_column("managed_resources", "storage_url")
    op.drop_column("managed_resources", "storage_key")
    op.drop_column("managed_resources", "storage_backend")
