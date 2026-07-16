"""add Organization billing accounts and provider key bindings

Revision ID: 20260716_0017
Revises: 20260716_0016
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op


revision = "20260716_0017"
down_revision = "20260716_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_billing_accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("trial_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'suspended', 'closed')",
            name="ck_organization_billing_accounts_status",
        ),
        sa.CheckConstraint(
            "policy IN ('byok_only', 'platform_only', 'prefer_byok', 'prefer_platform')",
            name="ck_organization_billing_accounts_policy",
        ),
        sa.CheckConstraint(
            "currency = 'USD'",
            name="ck_organization_billing_accounts_currency",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            name="uq_organization_billing_accounts_organization",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_organization_billing_accounts_organization_id",
        ),
    )
    op.create_index(
        op.f("ix_organization_billing_accounts_organization_id"),
        "organization_billing_accounts",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "organization_provider_key_bindings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column(
            "organization_billing_account_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("upstream_key_id", sa.String(length=255), nullable=True),
        sa.Column("spending_limit_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
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
            "status IN ('active', 'revoked')",
            name="ck_organization_provider_keys_status",
        ),
        sa.CheckConstraint(
            "spending_limit_usd_micros IS NULL OR spending_limit_usd_micros >= 0",
            name="ck_organization_provider_keys_spending_limit_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "organization_billing_account_id"],
            [
                "organization_billing_accounts.organization_id",
                "organization_billing_accounts.id",
            ],
            name="fk_organization_provider_keys_organization_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_organization_provider_keys_organization_provider",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_organization_provider_keys_organization_id",
        ),
    )
    op.create_index(
        op.f("ix_organization_provider_key_bindings_organization_id"),
        "organization_provider_key_bindings",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_provider_keys_organization_status",
        "organization_provider_key_bindings",
        ["organization_id", "status"],
        unique=False,
    )

    with op.batch_alter_table("session_funding_bindings") as batch_op:
        batch_op.create_foreign_key(
            "fk_session_funding_bindings_organization_billing_account",
            "organization_billing_accounts",
            ["organization_id", "organization_billing_account_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_session_funding_bindings_organization_provider_key",
            "organization_provider_key_bindings",
            ["organization_id", "organization_provider_key_binding_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("session_funding_bindings") as batch_op:
        batch_op.drop_constraint(
            "fk_session_funding_bindings_organization_provider_key",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_session_funding_bindings_organization_billing_account",
            type_="foreignkey",
        )

    op.drop_index(
        "ix_organization_provider_keys_organization_status",
        table_name="organization_provider_key_bindings",
    )
    op.drop_index(
        op.f("ix_organization_provider_key_bindings_organization_id"),
        table_name="organization_provider_key_bindings",
    )
    op.drop_table("organization_provider_key_bindings")

    op.drop_index(
        op.f("ix_organization_billing_accounts_organization_id"),
        table_name="organization_billing_accounts",
    )
    op.drop_table("organization_billing_accounts")
