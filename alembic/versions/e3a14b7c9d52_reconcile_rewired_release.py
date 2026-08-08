"""reconcile databases stamped by the rewired release history

Revision ID: e3a14b7c9d52
Revises: b7f2d4a91c53
Create Date: 2026-08-08

The release that introduced ``a7f3c2d81b40``, ``c8b204f61e33``, and
``f2a9c47d1b08`` also changed the already-deployed ``a4e91c7d5b02`` parent.
Databases that had reached ``b7f2d4a91c53`` through the old parent therefore
looked current to Alembic and skipped those three revisions.

This forward-only reconciliation applies the two runtime-required schema
effects after the published head. It deliberately leaves the skipped legacy
memory projection tables in place: they are unused, and dropping data is not an
appropriate side effect of an emergency release repair. Databases that followed
the new history already have both effects, so every operation is conditional.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3a14b7c9d52"
down_revision: Union[str, None] = "b7f2d4a91c53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    session_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("sessions")
    }
    if "model" not in session_columns:
        op.add_column("sessions", sa.Column("model", sa.JSON(), nullable=True))

    environment_uniques = sa.inspect(bind).get_unique_constraints("environments")
    if any(
        constraint.get("name") == "uq_environments_organization_name"
        for constraint in environment_uniques
    ):
        with op.batch_alter_table("environments") as batch:
            batch.drop_constraint(
                "uq_environments_organization_name",
                type_="unique",
            )


def downgrade() -> None:
    # This revision cannot know whether each effect came from the original
    # migrations or from this reconciliation. Reversing it could therefore
    # destroy schema that belongs to still-applied predecessor revisions.
    pass
