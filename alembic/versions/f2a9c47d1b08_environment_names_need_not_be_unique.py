"""environment names need not be unique

Revision ID: f2a9c47d1b08
Revises: c8b204f61e33
Create Date: 2026-08-06

An environment is a recipe that gets built once and never edited: rebuilding
under a running session would change the image out from under containers that
already exist. So changing a recipe means registering a new environment beside
the old one, and the sessions still pointing at the old one keep working.

A unique name per organization made that impossible. The caller's label is the
same across the whole history of one logical environment — `votrix-file-test`
before the package was added and after — so the second registration collided
with the first and the recipe could never move.

The name was never an identifier. Nothing resolves an environment by it:
sessions, agents and blueprints all carry `environment_id`, and the only two
readers of the name were the checks enforcing this constraint on each other.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f2a9c47d1b08"
down_revision: Union[str, None] = "c8b204f61e33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so this also runs on SQLite, which cannot ALTER a constraint
    # and has to copy the table instead. On PostgreSQL it is a plain ALTER.
    with op.batch_alter_table("environments") as batch:
        batch.drop_constraint("uq_environments_organization_name", type_="unique")


def downgrade() -> None:
    """Fails if duplicates were registered while the constraint was gone —
    which is the whole point of removing it, so expect that on a live database.
    """

    with op.batch_alter_table("environments") as batch:
        batch.create_unique_constraint(
            "uq_environments_organization_name", ["organization_id", "name"]
        )
