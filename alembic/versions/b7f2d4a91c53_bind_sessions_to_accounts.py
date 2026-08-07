"""pin each Session to the Account that pays for it

Revision ID: b7f2d4a91c53
Revises: a4e91c7d5b02
Create Date: 2026-08-06

Pinned at creation rather than resolved per turn: resolving each time would
split one conversation's spend across two Accounts the moment the
Organization's default changed under it, and no reading of the bill afterwards
could put it back together.

Nullable, because Sessions opened before Accounts existed have none. Those fall
back to the Organization's default when their key is resolved, so an old
conversation keeps working rather than failing on its next turn.

RESTRICT rather than CASCADE on the reference: an Account is never deleted, and
if one somehow were, taking the Sessions it billed along with it would destroy
the record of what the charge was for.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7f2d4a91c53"
down_revision: Union[str, None] = "a4e91c7d5b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("account_id", sa.String(64), nullable=True),
    )
    # Batch mode because SQLite cannot ALTER a constraint into place, and the
    # test suite runs these migrations against SQLite.
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.create_foreign_key(
            "fk_sessions_account_id",
            "organization_accounts",
            ["account_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("fk_sessions_account_id", type_="foreignkey")
    op.drop_column("sessions", "account_id")
