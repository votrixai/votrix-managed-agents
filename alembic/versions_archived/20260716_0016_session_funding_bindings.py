"""add durable Organization-scoped Session funding bindings

Revision ID: 20260716_0016
Revises: 20260715_0015
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op

revision = "20260716_0016"
down_revision = "20260715_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vaults and model Credentials are soft-deleted managed resources. This
    # composite key lets funding bindings retain an Organization-scoped,
    # immutable reference after a resource is archived or deleted.
    with op.batch_alter_table("managed_resources") as batch_op:
        batch_op.create_unique_constraint(
            "uq_managed_resources_organization_id",
            ["organization_id", "id"],
        )

    op.create_table(
        "session_funding_bindings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("vault_id", sa.String(length=64), nullable=True),
        sa.Column("model_credential_id", sa.String(length=64), nullable=True),
        sa.Column("organization_billing_account_id", sa.String(length=64), nullable=True),
        sa.Column(
            "organization_provider_key_binding_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('none', 'vault', 'platform')",
            name="ck_session_funding_bindings_source",
        ),
        sa.CheckConstraint(
            "(source = 'none' AND vault_id IS NULL AND model_credential_id IS NULL "
            "AND organization_billing_account_id IS NULL "
            "AND organization_provider_key_binding_id IS NULL) OR "
            "(source = 'vault' AND vault_id IS NOT NULL "
            "AND model_credential_id IS NOT NULL "
            "AND organization_billing_account_id IS NULL "
            "AND organization_provider_key_binding_id IS NULL) OR "
            "(source = 'platform' AND vault_id IS NULL "
            "AND model_credential_id IS NULL "
            "AND organization_billing_account_id IS NOT NULL "
            "AND organization_provider_key_binding_id IS NOT NULL)",
            name="ck_session_funding_bindings_source_shape",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_session_funding_bindings_organization_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vault_id"],
            ["managed_resources.organization_id", "managed_resources.id"],
            name="fk_session_funding_bindings_organization_vault",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "model_credential_id"],
            ["managed_resources.organization_id", "managed_resources.id"],
            name="fk_session_funding_bindings_organization_credential",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "session_id",
            name="uq_session_funding_bindings_organization_session",
        ),
    )
    op.create_index(
        op.f("ix_session_funding_bindings_organization_id"),
        "session_funding_bindings",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_funding_bindings_organization_source",
        "session_funding_bindings",
        ["organization_id", "source"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_funding_bindings_organization_source",
        table_name="session_funding_bindings",
    )
    op.drop_index(
        op.f("ix_session_funding_bindings_organization_id"),
        table_name="session_funding_bindings",
    )
    op.drop_table("session_funding_bindings")

    with op.batch_alter_table("managed_resources") as batch_op:
        batch_op.drop_constraint(
            "uq_managed_resources_organization_id",
            type_="unique",
        )
