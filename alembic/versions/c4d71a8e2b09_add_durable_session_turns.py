"""Durable Pub/Sub handoff and turn ownership."""

import sqlalchemy as sa

from alembic import op

revision = "c4d71a8e2b09"
down_revision = "c1e7a3b58f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_turns",
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("history_ids", sa.JSON()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_session_turns_recovery", "session_turns", ["done", "available_at"]
    )


def downgrade() -> None:
    op.drop_table("session_turns")
