"""add resumable organization onboarding requests

Revision ID: 91b7c24d5e8f
Revises: b8d4f1a92c73
Create Date: 2026-08-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "91b7c24d5e8f"
down_revision = "b8d4f1a92c73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_onboarding_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("requester_user_id", sa.String(length=64), nullable=False),
        sa.Column("requester_email", sa.String(length=255), nullable=True),
        sa.Column("requested_name", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=True),
        sa.Column("provisioning_lease_token", sa.String(length=64), nullable=True),
        sa.Column(
            "provisioning_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_onboarding_requests_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            name="uq_organization_onboarding_requests_organization",
        ),
        sa.UniqueConstraint(
            "requester_user_id",
            name="uq_organization_onboarding_requests_user",
        ),
    )


def downgrade() -> None:
    op.drop_table("organization_onboarding_requests")
