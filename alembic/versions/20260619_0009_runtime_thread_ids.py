"""add opaque runtime thread identifiers

Revision ID: 20260619_0009
Revises: 20260619_0008
Create Date: 2026-06-19
"""

import sqlalchemy as sa
from alembic import op

revision = "20260619_0009"
down_revision = "20260619_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("runtime_thread_id", sa.String(length=64), nullable=True))
    op.execute("UPDATE sessions SET runtime_thread_id = 'thread_' || id WHERE runtime_thread_id IS NULL")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column("runtime_thread_id", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_sessions_workspace_runtime_thread",
            ["workspace_id", "runtime_thread_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("uq_sessions_workspace_runtime_thread", type_="unique")
        batch_op.drop_column("runtime_thread_id")
