"""add organization accounts and their provider credentials

Revision ID: a4e91c7d5b02
Revises: f2a9c47d1b08
Create Date: 2026-08-06

An Account is where an Organization's spend is measured and capped. One
Account holds one provider credential, and the credential is what makes the
boundary enforceable: a request either carries it or fails.

Accounts are never deleted, only suspended, so the figures recorded against one
stay readable for as long as the Organization does. There is no delete path in
the API and none here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4e91c7d5b02"
# Rebased onto the peteryue chain at the merge rather than left as a second
# head. Both branches grew from d4f7a9c2e106 and touch different tables — this
# one adds organization_accounts, the other adds sessions.model — so the order
# between them carries no meaning and a linear history is worth more than a
# merge revision recording a coincidence.
down_revision: Union[str, None] = "f2a9c47d1b08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_accounts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        # NULL for a non-default. The unique constraint below then permits many
        # non-defaults and exactly one default, because NULLs do not collide.
        sa.Column("is_default", sa.Boolean()),
        sa.Column("limit_usd", sa.Numeric(18, 6)),
        sa.Column("idempotency_key", sa.String(255)),
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
        sa.UniqueConstraint(
            "organization_id",
            "is_default",
            name="uq_organization_accounts_single_default",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_organization_accounts_idempotency_key",
        ),
        sa.CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended')",
            name="ck_organization_accounts_status",
        ),
        sa.CheckConstraint(
            "limit_usd IS NULL OR limit_usd > 0",
            name="ck_organization_accounts_limit_positive",
        ),
    )
    op.create_index(
        "ix_organization_accounts_organization_id",
        "organization_accounts",
        ["organization_id"],
    )

    op.create_table(
        "account_provider_credentials",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(64),
            sa.ForeignKey("organization_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("provider_key_name", sa.String(255), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
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
        # One credential per Account. Two would mean two things can spend on
        # one boundary, and no way to say which of them a charge came from.
        sa.UniqueConstraint(
            "account_id", name="uq_account_provider_credentials_account"
        ),
        sa.UniqueConstraint(
            "key_hash", name="uq_account_provider_credentials_key_hash"
        ),
        # The provider-side name is the only attribution readable from the
        # provider. Two Accounts answering to one name makes that unreadable.
        sa.UniqueConstraint(
            "provider_key_name", name="uq_account_provider_credentials_key_name"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_account_provider_credentials_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("account_provider_credentials")
    op.drop_index(
        "ix_organization_accounts_organization_id",
        table_name="organization_accounts",
    )
    op.drop_table("organization_accounts")
