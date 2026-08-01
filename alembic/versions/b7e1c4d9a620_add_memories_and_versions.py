"""add memories and memory versions

Revision ID: b7e1c4d9a620
Revises: e3115a9c42d0
Create Date: 2026-08-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b7e1c4d9a620"
down_revision = "e3115a9c42d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        sa.Column("lock_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["memory_store_id"],
            ["memory_stores.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "memory_store_id",
            "path",
            name="uq_memories_store_path",
        ),
    )
    op.create_index(
        op.f("ix_memories_organization_id"),
        "memories",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_memories_store_path",
        "memories",
        ["memory_store_id", "path"],
        unique=False,
    )
    op.create_index(
        "ix_memories_organization_store_updated",
        "memories",
        ["organization_id", "memory_store_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "memory_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("memory_store_id", sa.String(length=64), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.JSON(), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=True),
        sa.Column("api_key_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redacted_by", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('created', 'modified', 'deleted')",
            name="ck_memory_versions_operation",
        ),
        sa.ForeignKeyConstraint(
            ["memory_store_id"],
            ["memory_stores.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_memory_versions_organization_id"),
        "memory_versions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_versions_store_created",
        "memory_versions",
        ["memory_store_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_versions_store_memory_created",
        "memory_versions",
        ["memory_store_id", "memory_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_versions_store_api_key_created",
        "memory_versions",
        ["memory_store_id", "api_key_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_versions_store_session_created",
        "memory_versions",
        ["memory_store_id", "session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_versions_store_session_created",
        table_name="memory_versions",
    )
    op.drop_index(
        "ix_memory_versions_store_api_key_created",
        table_name="memory_versions",
    )
    op.drop_index(
        "ix_memory_versions_store_memory_created",
        table_name="memory_versions",
    )
    op.drop_index(
        "ix_memory_versions_store_created",
        table_name="memory_versions",
    )
    op.drop_index(
        op.f("ix_memory_versions_organization_id"),
        table_name="memory_versions",
    )
    op.drop_table("memory_versions")

    op.drop_index(
        "ix_memories_organization_store_updated",
        table_name="memories",
    )
    op.drop_index("ix_memories_store_path", table_name="memories")
    op.drop_index(op.f("ix_memories_organization_id"), table_name="memories")
    op.drop_table("memories")
