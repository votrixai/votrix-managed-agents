"""Persist the on-demand worker's execution gate before changing Cloud Run."""

import sqlalchemy as sa

from alembic import op

revision = "d71f806c2e43"
down_revision = "c4d71a8e2b09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "worker_pool_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(32), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("idle_since", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(table, [{"id": 1, "target": 0, "command": "", "ready": False}])


def downgrade() -> None:
    op.drop_table("worker_pool_control")
