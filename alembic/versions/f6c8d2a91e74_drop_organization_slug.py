"""drop the unused Organization slug

Revision ID: f6c8d2a91e74
Revises: e3a14b7c9d52
Create Date: 2026-08-13

Organization identity and tenant isolation use the generated ``org_*`` ID.
The slug was never used for routing, authentication, storage, or provider
attribution, so keeping a second globally unique permanent identifier only
made provisioning stricter without serving the runtime.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f6c8d2a91e74"
down_revision: Union[str, None] = "e3a14b7c9d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("slug")


def downgrade() -> None:
    # The original values cannot be reconstructed after the column is dropped.
    # IDs are unique and stable, so they are safe compatibility values if an
    # operator must temporarily run the previous application version.
    with op.batch_alter_table("organizations") as batch:
        batch.add_column(sa.Column("slug", sa.String(64), nullable=True))

    op.execute(sa.text("UPDATE organizations SET slug = id"))

    with op.batch_alter_table("organizations") as batch:
        batch.alter_column("slug", existing_type=sa.String(64), nullable=False)
        batch.create_unique_constraint("uq_organizations_slug", ["slug"])
