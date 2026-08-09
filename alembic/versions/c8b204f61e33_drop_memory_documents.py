"""drop memory documents and their version history

Revision ID: c8b204f61e33
Revises: a7f3c2d81b40
Create Date: 2026-08-06

A Memory Store is a durable Volume an Agent mounts and writes files into. It
also carried a second, parallel account of the same contents: `memories` held
an API-visible head per path and `memory_versions` an immutable history, kept
in step by a reconciliation that hashed the whole mount after every turn.

Nothing read them. The API over documents is gone, so the projection has no
audience — and it was not free: every turn diffed the mounted Volume to keep
rows nobody was going to look at.

What remains is the store itself. The Volume is the contents, the Agent's
filesystem tools are the interface, and the bytes outlive a Session because the
Volume does. Recovering a document API later means reading the Volume, not
restoring these tables.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8b204f61e33"
down_revision: Union[str, None] = "a7f3c2d81b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("memory_versions")
    op.drop_table("memories")


def downgrade() -> None:
    """Recreates the tables, empty. The documents themselves are in the
    Volumes, so a downgrade brings back the shape and not the history."""

    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("memory_store_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_size_bytes", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.String(length=64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_store_id"], ["memory_stores.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_store_id", "path", name="uq_memories_store_path"),
    )
    op.create_index("ix_memories_organization_id", "memories", ["organization_id"])
    op.create_index(
        "ix_memories_store_path", "memories", ["memory_store_id", "path"]
    )
    op.create_index(
        "ix_memories_organization_store_updated",
        "memories",
        ["organization_id", "memory_store_id", "updated_at"],
    )

    op.create_table(
        "memory_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("memory_store_id", sa.String(length=64), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.JSON(), nullable=True),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redacted_by", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('created', 'modified', 'deleted')",
            name="ck_memory_versions_operation",
        ),
        sa.ForeignKeyConstraint(
            ["memory_store_id"], ["memory_stores.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_versions_store_created",
        "memory_versions",
        ["memory_store_id", "created_at", "id"],
    )
