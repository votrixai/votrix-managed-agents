"""add session memory stores

Revision ID: e3115a9c42d0
Revises: c903b7e15a42
Create Date: 2026-08-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e3115a9c42d0"
down_revision = "c903b7e15a42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_memory_stores",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("memory_store_id", sa.String(length=64), nullable=False),
        sa.Column(
            "access",
            sa.String(length=32),
            server_default="read_write",
            nullable=False,
        ),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("mount_path", sa.String(length=1024), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
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
        sa.CheckConstraint(
            "access IN ('read_write', 'read_only')",
            name="ck_session_memory_stores_access",
        ),
        sa.ForeignKeyConstraint(
            ["memory_store_id"],
            ["memory_stores.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "memory_store_id",
            name="uq_session_memory_stores_store",
        ),
        sa.UniqueConstraint(
            "session_id",
            "mount_path",
            name="uq_session_memory_stores_mount_path",
        ),
    )
    op.create_index(
        op.f("ix_session_memory_stores_organization_id"),
        "session_memory_stores",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_memory_stores_session_id"),
        "session_memory_stores",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_memory_stores_store",
        "session_memory_stores",
        ["memory_store_id", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_memory_stores_store",
        table_name="session_memory_stores",
    )
    op.drop_index(
        op.f("ix_session_memory_stores_session_id"),
        table_name="session_memory_stores",
    )
    op.drop_index(
        op.f("ix_session_memory_stores_organization_id"),
        table_name="session_memory_stores",
    )
    op.drop_table("session_memory_stores")
