"""drop sessions.current_event_id

The column recorded which event started the turn in flight, and had exactly one
reader: the `event_ids` an interrupt put on its stop reason. That field is gone
— `requires_action` already uses `event_ids` for the calls a client must answer,
and the same name meaning something else on the neighbouring stop reason was a
trap — so nothing reads this any more.

Revision ID: 71230f71b754
Revises: 6522db3cbf99
Create Date: 2026-07-29 03:47:13.709577
"""

from alembic import op
import sqlalchemy as sa

revision = "71230f71b754"
down_revision = "6522db3cbf99"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("sessions", "current_event_id")


def downgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("current_event_id", sa.VARCHAR(length=64), nullable=True),
    )
