"""add sessions.model

Until now the model was reachable only through the Agent definition: a Session
pinned an `agent_version`, and whatever that version named is what ran. Changing
model therefore meant editing the Agent — which cuts a new version and moves
every future Session on it, when the caller only wanted one conversation to run
somewhere else.

This column makes the choice per-Session, pinned exactly like `agent_version`
beside it. NULL keeps the existing behaviour and is the default: no preference
means the Agent version's model applies, resolved at run time so those Sessions
keep following the Agent rather than freezing a copy of it.

Revision ID: a7f3c2d81b40
Revises: d4f7a9c2e106
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa

revision = "a7f3c2d81b40"
down_revision = "d4f7a9c2e106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("model", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Sessions that had named a model fall back to their Agent version's, which
    # is what they would have run on had they never been able to ask.
    op.drop_column("sessions", "model")
