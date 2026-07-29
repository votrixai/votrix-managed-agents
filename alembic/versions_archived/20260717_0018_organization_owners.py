"""add organization owners

Revision ID: 20260717_0018
Revises: 20260716_0017
Create Date: 2026-07-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_0018"
down_revision = "20260716_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_owners",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("granted_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_owners_organization_user",
        ),
    )
    op.create_index(
        op.f("ix_organization_owners_organization_id"),
        "organization_owners",
        ["organization_id"],
    )
    op.create_index("ix_organization_owners_user_id", "organization_owners", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_organization_owners_user_id", table_name="organization_owners")
    op.drop_index(op.f("ix_organization_owners_organization_id"), table_name="organization_owners")
    op.drop_table("organization_owners")
