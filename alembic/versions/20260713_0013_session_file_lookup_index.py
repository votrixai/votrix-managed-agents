"""reserve session file lookup revision

Revision ID: 20260713_0013
Revises: 20260713_0012
Create Date: 2026-07-13
"""

revision = "20260713_0013"
down_revision = "20260713_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh history creates the canonical lookup index in revision 0006.
    pass


def downgrade() -> None:
    pass
