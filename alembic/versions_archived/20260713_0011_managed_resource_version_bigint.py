"""store managed resource versions as bigint

Revision ID: 20260713_0011
Revises: 20260711_0010
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_0011"
down_revision = "20260711_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("managed_resources") as batch_op:
        batch_op.alter_column(
            "version",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("managed_resources") as batch_op:
        batch_op.alter_column(
            "version",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
