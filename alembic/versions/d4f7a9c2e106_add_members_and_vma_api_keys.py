"""add organization members and VMA API keys

Revision ID: d4f7a9c2e106
Revises: b7e1c4d9a620
Create Date: 2026-08-02 00:00:00.000000
"""

from alembic import context, op
import sqlalchemy as sa


revision = "d4f7a9c2e106"
down_revision = "b7e1c4d9a620"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the live owner rows while widening membership to support roles.
    op.rename_table("organization_owners", "organization_members")
    if _dialect_name() == "postgresql":
        op.execute(
            "ALTER TABLE organization_members "
            "RENAME CONSTRAINT organization_owners_pkey "
            "TO organization_members_pkey"
        )
    op.add_column(
        "organization_members",
        sa.Column(
            "role",
            sa.String(length=32),
            server_default="owner",
            nullable=False,
        ),
    )
    with op.batch_alter_table("organization_members") as batch_op:
        batch_op.drop_constraint(
            "uq_organization_owners_organization_user",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_organization_members_organization_user",
            ["organization_id", "user_id"],
        )
        batch_op.create_check_constraint(
            "ck_organization_members_role",
            "role IN ('owner', 'admin', 'member')",
        )
        batch_op.create_foreign_key(
            "fk_organization_members_organization",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_index("ix_organization_owners_organization_id")
        batch_op.create_index(
            "ix_organization_members_organization_id",
            ["organization_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_organization_members_user_id",
            ["user_id"],
            unique=False,
        )
        batch_op.alter_column(
            "role",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )

    op.create_table(
        "vma_api_keys",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "scopes",
            sa.JSON(),
            server_default=sa.text("'[\"api\"]'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=128), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("replaces_key_id", sa.String(length=64), nullable=True),
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
            "replaces_key_id IS NULL OR id <> replaces_key_id",
            name="ck_vma_api_keys_not_self_replacing",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_vma_api_keys_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "replaces_key_id"],
            ["vma_api_keys.organization_id", "vma_api_keys.id"],
            name="fk_vma_api_keys_replaces_key",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_vma_api_keys_key_hash"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_vma_api_keys_organization_id",
        ),
        sa.UniqueConstraint(
            "replaces_key_id",
            name="uq_vma_api_keys_replaces_key",
        ),
    )
    op.create_index(
        "ix_vma_api_keys_organization_revoked",
        "vma_api_keys",
        ["organization_id", "revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    _guard_members_are_owners()

    op.drop_index(
        "ix_vma_api_keys_organization_revoked",
        table_name="vma_api_keys",
    )
    op.drop_table("vma_api_keys")

    with op.batch_alter_table("organization_members") as batch_op:
        batch_op.drop_constraint(
            "fk_organization_members_organization",
            type_="foreignkey",
        )
        batch_op.drop_constraint("ck_organization_members_role", type_="check")
        batch_op.drop_constraint(
            "uq_organization_members_organization_user",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_organization_owners_organization_user",
            ["organization_id", "user_id"],
        )
        batch_op.drop_index("ix_organization_members_user_id")
        batch_op.drop_index("ix_organization_members_organization_id")
        batch_op.create_index(
            "ix_organization_owners_organization_id",
            ["organization_id"],
            unique=False,
        )
        batch_op.drop_column("role")
    if _dialect_name() == "postgresql":
        op.execute(
            "ALTER TABLE organization_members "
            "RENAME CONSTRAINT organization_members_pkey "
            "TO organization_owners_pkey"
        )
    op.rename_table("organization_members", "organization_owners")


def _guard_members_are_owners() -> None:
    message = "Cannot downgrade organization_members while admin or member rows exist"
    if context.is_offline_mode():
        if _dialect_name() != "postgresql":
            raise RuntimeError(
                "Offline downgrade is only supported for PostgreSQL because role data "
                "must be checked before removing organization_members.role"
            )
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM organization_members WHERE role <> 'owner'
                ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade organization_members while admin or member rows exist';
                END IF;
            END
            $$
            """
        )
        return

    connection = op.get_bind()
    non_owner_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM organization_members "
            "WHERE role <> 'owner'"
        )
    )
    if non_owner_count:
        raise RuntimeError(message)


def _dialect_name() -> str:
    return context.get_context().dialect.name
