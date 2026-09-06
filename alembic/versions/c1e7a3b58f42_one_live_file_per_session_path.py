"""One live file per session path.

A session's outputs are a directory. Capture used to append a row every time a
path's contents changed, so a project handed over three times left three rows
carrying the same name, and every list of that session's files showed all
three. This makes the table say what the directory says: one live row per
path, with the rows behind it archived.

Revision ID: c1e7a3b58f42
Revises: b8d4f1a92c73
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1e7a3b58f42"
down_revision = "b8d4f1a92c73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retire the history the old rule accumulated, newest row per path kept.
    # Archived rather than deleted: their bytes are still in the bucket and
    # their ids may have been handed out, so they stay downloadable — they
    # just stop being listed.
    op.execute(
        """
        UPDATE files AS f
        SET archived_at = CURRENT_TIMESTAMP
        WHERE f.archived_at IS NULL
          AND f.scope_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM files AS newer
              WHERE newer.organization_id = f.organization_id
                AND newer.scope_id = f.scope_id
                AND newer.filename = f.filename
                AND newer.archived_at IS NULL
                AND (newer.created_at, newer.id) > (f.created_at, f.id)
          )
        """
    )
    op.create_index(
        "uq_files_live_scoped_path",
        "files",
        ["organization_id", "scope_id", "filename"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL AND scope_id IS NOT NULL"),
        sqlite_where=sa.text("archived_at IS NULL AND scope_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_files_live_scoped_path", table_name="files")
