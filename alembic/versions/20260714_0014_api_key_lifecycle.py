"""add scoped API key lifecycle metadata

Revision ID: 20260714_0014
Revises: 20260713_0013
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0014"
down_revision = "20260713_0013"
branch_labels = None
depends_on = None

_LEGACY_SCOPES = '["api", "api_keys:manage", "worker"]'


def upgrade() -> None:
    # Existing environment-provisioned keys were unrestricted. Preserve that
    # behavior during migration; all newly issued keys use explicit scopes.
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(f"'{_LEGACY_SCOPES}'"),
        ),
    )
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_keys", sa.Column("created_by", sa.String(length=128), nullable=True))
    op.add_column("api_keys", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_keys", sa.Column("revoked_by", sa.String(length=128), nullable=True))
    op.add_column("api_keys", sa.Column("revocation_reason", sa.Text(), nullable=True))
    op.add_column("api_keys", sa.Column("replaced_by_key_id", sa.String(length=64), nullable=True))
    op.add_column("api_keys", sa.Column("replaces_key_id", sa.String(length=64), nullable=True))
    op.execute(
        sa.text(
            "UPDATE api_keys "
            "SET revoked_at = archived_at, revocation_reason = 'legacy archive' "
            "WHERE archived_at IS NOT NULL"
        )
    )
    op.create_index(
        "ix_api_keys_organization_revoked",
        "api_keys",
        ["organization_id", "revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_organization_revoked", table_name="api_keys")
    op.drop_column("api_keys", "replaces_key_id")
    op.drop_column("api_keys", "replaced_by_key_id")
    op.drop_column("api_keys", "revocation_reason")
    op.drop_column("api_keys", "revoked_by")
    op.drop_column("api_keys", "revoked_at")
    op.drop_column("api_keys", "created_by")
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "scopes")
