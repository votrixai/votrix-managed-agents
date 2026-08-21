"""add multi-provider BYOK Account model credentials

Revision ID: c1b7e4d92a60
Revises: f6c8d2a91e74
Create Date: 2026-08-20

The old table held exactly one VMA-managed OpenRouter key per Account. Rename
it to the domain name used going forward, preserve every existing row as a
Platform/OpenRouter credential, and allow BYOK Accounts to hold one direct key
per backend.

This is intentionally a coordinated migration: the physical table rename
means old application instances must be drained before the new schema lands.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


revision: str = "c1b7e4d92a60"
down_revision: Union[str, None] = "f6c8d2a91e74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_CREDENTIAL_TABLE = "account_provider_credentials"
NEW_CREDENTIAL_TABLE = "account_model_credentials"


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline SQL cannot inspect a live schema. Emit the normal path; the
        # repair branch below exists only for one identifiable historical
        # online state.
        _upgrade_existing_tables()
        return

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "organization_accounts" not in tables:
        # A short-lived rewired migration graph could be stamped past the
        # Account branch without physically creating it. Reaching this head
        # repairs that released state directly to the current shape.
        _create_current_account_tables()
        session_columns = {
            column["name"] for column in sa.inspect(bind).get_columns("sessions")
        }
        if "account_id" not in session_columns:
            op.add_column(
                "sessions", sa.Column("account_id", sa.String(64), nullable=True)
            )
            with op.batch_alter_table("sessions") as batch:
                batch.create_foreign_key(
                    "fk_sessions_account_id",
                    "organization_accounts",
                    ["account_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
        return

    _upgrade_existing_tables()


def _upgrade_existing_tables() -> None:
    """Transform the normal down-revision schema without losing key data."""

    with op.batch_alter_table("organization_accounts") as batch:
        batch.add_column(
            sa.Column(
                "funding_mode",
                sa.String(32),
                nullable=False,
                server_default="platform",
            )
        )
        batch.create_unique_constraint(
            "uq_organization_accounts_id_funding_mode",
            ["id", "funding_mode"],
        )
        batch.create_check_constraint(
            "ck_organization_accounts_funding_mode",
            "funding_mode IN ('platform', 'byok')",
        )
        batch.create_check_constraint(
            "ck_organization_accounts_byok_no_limit",
            "funding_mode != 'byok' OR limit_usd IS NULL",
        )

    op.rename_table(OLD_CREDENTIAL_TABLE, NEW_CREDENTIAL_TABLE)
    with op.batch_alter_table(NEW_CREDENTIAL_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "funding_mode",
                sa.String(32),
                nullable=False,
                server_default="platform",
            )
        )
        batch.add_column(
            sa.Column(
                "backend",
                sa.String(32),
                nullable=False,
                server_default="openrouter",
            )
        )
        batch.alter_column(
            "provider_key_name",
            existing_type=sa.String(255),
            nullable=True,
        )
        batch.drop_constraint(
            "uq_account_provider_credentials_account", type_="unique"
        )
        batch.drop_constraint(
            "uq_account_provider_credentials_key_hash", type_="unique"
        )
        batch.drop_constraint(
            "uq_account_provider_credentials_key_name", type_="unique"
        )
        batch.drop_constraint(
            "ck_account_provider_credentials_status", type_="check"
        )
        batch.create_foreign_key(
            "fk_account_model_credentials_account_funding",
            "organization_accounts",
            ["account_id", "funding_mode"],
            ["id", "funding_mode"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_account_model_credentials_account_backend",
            ["account_id", "backend"],
        )
        batch.create_unique_constraint(
            "uq_account_model_credentials_key_hash", ["key_hash"]
        )
        batch.create_unique_constraint(
            "uq_account_model_credentials_key_name", ["provider_key_name"]
        )
        batch.create_check_constraint(
            "ck_account_model_credentials_status",
            "status IN ('active', 'suspended')",
        )
        batch.create_check_constraint(
            "ck_account_model_credentials_funding_mode",
            "funding_mode IN ('platform', 'byok')",
        )
        batch.create_check_constraint(
            "ck_account_model_credentials_backend",
            "backend IN "
            "('openrouter', 'anthropic', 'openai', 'google', 'deepseek')",
        )
        batch.create_check_constraint(
            "ck_account_model_credentials_funding_backend",
            "(funding_mode = 'platform' AND backend = 'openrouter' "
            "AND provider_key_name IS NOT NULL) OR "
            "(funding_mode = 'byok' AND backend IN "
            "('anthropic', 'openai', 'google', 'deepseek') "
            "AND provider_key_name IS NULL)",
        )


def _create_current_account_tables() -> None:
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
        sa.Column(
            "funding_mode",
            sa.String(32),
            nullable=False,
            server_default="platform",
        ),
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
        sa.UniqueConstraint(
            "id",
            "funding_mode",
            name="uq_organization_accounts_id_funding_mode",
        ),
        sa.CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended')",
            name="ck_organization_accounts_status",
        ),
        sa.CheckConstraint(
            "limit_usd IS NULL OR limit_usd > 0",
            name="ck_organization_accounts_limit_positive",
        ),
        sa.CheckConstraint(
            "funding_mode IN ('platform', 'byok')",
            name="ck_organization_accounts_funding_mode",
        ),
        sa.CheckConstraint(
            "funding_mode != 'byok' OR limit_usd IS NULL",
            name="ck_organization_accounts_byok_no_limit",
        ),
    )
    op.create_index(
        "ix_organization_accounts_organization_id",
        "organization_accounts",
        ["organization_id"],
    )
    op.create_table(
        NEW_CREDENTIAL_TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(64),
            sa.ForeignKey("organization_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "funding_mode",
            sa.String(32),
            nullable=False,
            server_default="platform",
        ),
        sa.Column(
            "backend",
            sa.String(32),
            nullable=False,
            server_default="openrouter",
        ),
        sa.Column(
            "organization_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("provider_key_name", sa.String(255), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["account_id", "funding_mode"],
            ["organization_accounts.id", "organization_accounts.funding_mode"],
            name="fk_account_model_credentials_account_funding",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            "backend",
            name="uq_account_model_credentials_account_backend",
        ),
        sa.UniqueConstraint(
            "key_hash", name="uq_account_model_credentials_key_hash"
        ),
        sa.UniqueConstraint(
            "provider_key_name", name="uq_account_model_credentials_key_name"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_account_model_credentials_status",
        ),
        sa.CheckConstraint(
            "funding_mode IN ('platform', 'byok')",
            name="ck_account_model_credentials_funding_mode",
        ),
        sa.CheckConstraint(
            "backend IN "
            "('openrouter', 'anthropic', 'openai', 'google', 'deepseek')",
            name="ck_account_model_credentials_backend",
        ),
        sa.CheckConstraint(
            "(funding_mode = 'platform' AND backend = 'openrouter' "
            "AND provider_key_name IS NOT NULL) OR "
            "(funding_mode = 'byok' AND backend IN "
            "('anthropic', 'openai', 'google', 'deepseek') "
            "AND provider_key_name IS NULL)",
            name="ck_account_model_credentials_funding_backend",
        ),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        byok_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM organization_accounts "
                "WHERE funding_mode = 'byok'"
            )
        ).scalar_one()
        if byok_count:
            raise RuntimeError(
                "Cannot downgrade while BYOK Accounts exist; the previous schema "
                "cannot represent their credentials safely"
            )

    with op.batch_alter_table(NEW_CREDENTIAL_TABLE) as batch:
        batch.drop_constraint(
            "fk_account_model_credentials_account_funding", type_="foreignkey"
        )
        batch.drop_constraint(
            "uq_account_model_credentials_account_backend", type_="unique"
        )
        batch.drop_constraint(
            "uq_account_model_credentials_key_hash", type_="unique"
        )
        batch.drop_constraint(
            "uq_account_model_credentials_key_name", type_="unique"
        )
        batch.drop_constraint(
            "ck_account_model_credentials_status", type_="check"
        )
        batch.drop_constraint(
            "ck_account_model_credentials_funding_mode", type_="check"
        )
        batch.drop_constraint(
            "ck_account_model_credentials_backend", type_="check"
        )
        batch.drop_constraint(
            "ck_account_model_credentials_funding_backend", type_="check"
        )
        batch.create_unique_constraint(
            "uq_account_provider_credentials_account", ["account_id"]
        )
        batch.create_unique_constraint(
            "uq_account_provider_credentials_key_hash", ["key_hash"]
        )
        batch.create_unique_constraint(
            "uq_account_provider_credentials_key_name", ["provider_key_name"]
        )
        batch.create_check_constraint(
            "ck_account_provider_credentials_status",
            "status IN ('active', 'suspended')",
        )
        batch.alter_column(
            "provider_key_name",
            existing_type=sa.String(255),
            nullable=False,
        )
        batch.drop_column("backend")
        batch.drop_column("funding_mode")

    op.rename_table(NEW_CREDENTIAL_TABLE, OLD_CREDENTIAL_TABLE)

    with op.batch_alter_table("organization_accounts") as batch:
        batch.drop_constraint(
            "uq_organization_accounts_id_funding_mode", type_="unique"
        )
        batch.drop_constraint(
            "ck_organization_accounts_byok_no_limit", type_="check"
        )
        batch.drop_constraint(
            "ck_organization_accounts_funding_mode", type_="check"
        )
        batch.drop_column("funding_mode")
