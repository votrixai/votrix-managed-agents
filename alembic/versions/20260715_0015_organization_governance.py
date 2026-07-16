"""add organization quotas and append-only governance ledgers

Revision ID: 20260715_0015
Revises: 20260714_0014
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_0015"
down_revision = "20260714_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_quotas",
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("max_active_work", sa.Integer(), nullable=True),
        sa.Column("daily_model_tokens", sa.BigInteger(), nullable=True),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "requests_per_minute IS NULL OR requests_per_minute >= 0",
            name="ck_organization_quotas_requests_nonnegative",
        ),
        sa.CheckConstraint(
            "max_active_work IS NULL OR max_active_work >= 0",
            name="ck_organization_quotas_active_work_nonnegative",
        ),
        sa.CheckConstraint(
            "daily_model_tokens IS NULL OR daily_model_tokens >= 0",
            name="ck_organization_quotas_model_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "storage_bytes IS NULL OR storage_bytes >= 0",
            name="ck_organization_quotas_storage_nonnegative",
        ),
        sa.PrimaryKeyConstraint("organization_id"),
    )

    op.create_table(
        "organization_quota_counters",
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("value >= 0", name="ck_organization_quota_counters_value_nonnegative"),
        sa.CheckConstraint(
            "window_seconds >= 0",
            name="ck_organization_quota_counters_window_nonnegative",
        ),
        sa.PrimaryKeyConstraint("organization_id", "metric", "window_start"),
    )
    op.create_index(
        "ix_organization_quota_counters_metric_window",
        "organization_quota_counters",
        ["metric", "window_start"],
        unique=False,
    )

    op.create_table(
        "organization_quota_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("quota_name", sa.String(length=64), nullable=False),
        sa.Column("reference_id", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_organization_quota_reservations_amount_positive"),
        sa.CheckConstraint(
            "state IN ('active', 'released')",
            name="ck_organization_quota_reservations_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "quota_name",
            "reference_id",
            name="uq_organization_quota_reservations_reference",
        ),
    )
    op.create_index(
        op.f("ix_organization_quota_reservations_organization_id"),
        "organization_quota_reservations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_quota_reservations_organization_state",
        "organization_quota_reservations",
        ["organization_id", "quota_name", "state"],
        unique=False,
    )

    op.create_table(
        "audit_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_ledger_organization_id"), "audit_ledger", ["organization_id"], unique=False)
    op.create_index("ix_audit_ledger_request_id", "audit_ledger", ["request_id"], unique=False)
    op.create_index(
        "ix_audit_ledger_organization_occurred",
        "audit_ledger",
        ["organization_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_ledger_organization_action_occurred",
        "audit_ledger",
        ["organization_id", "action", "occurred_at"],
        unique=False,
    )

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("source_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_usage_ledger_quantity_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_usage_ledger_organization_idempotency",
        ),
    )
    op.create_index(op.f("ix_usage_ledger_organization_id"), "usage_ledger", ["organization_id"], unique=False)
    op.create_index(
        "ix_usage_ledger_organization_metric_occurred",
        "usage_ledger",
        ["organization_id", "metric", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_usage_ledger_organization_source",
        "usage_ledger",
        ["organization_id", "source_type", "source_id"],
        unique=False,
    )

    op.create_table(
        "tenant_idempotency",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="ck_tenant_idempotency_state",
        ),
        sa.CheckConstraint(
            "state = 'in_progress' OR response_status IS NOT NULL",
            name="ck_tenant_idempotency_completed_response",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "operation",
            "key_hash",
            name="uq_tenant_idempotency_organization_operation_key",
        ),
    )
    op.create_index(
        op.f("ix_tenant_idempotency_organization_id"),
        "tenant_idempotency",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_idempotency_organization_created",
        "tenant_idempotency",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_idempotency_state_updated",
        "tenant_idempotency",
        ["state", "updated_at"],
        unique=False,
    )

    _create_append_only_triggers()


def downgrade() -> None:
    _drop_append_only_triggers()

    op.drop_index(
        "ix_tenant_idempotency_state_updated",
        table_name="tenant_idempotency",
    )
    op.drop_index(
        "ix_tenant_idempotency_organization_created",
        table_name="tenant_idempotency",
    )
    op.drop_index(
        op.f("ix_tenant_idempotency_organization_id"),
        table_name="tenant_idempotency",
    )
    op.drop_table("tenant_idempotency")

    op.drop_index("ix_usage_ledger_organization_source", table_name="usage_ledger")
    op.drop_index("ix_usage_ledger_organization_metric_occurred", table_name="usage_ledger")
    op.drop_index(op.f("ix_usage_ledger_organization_id"), table_name="usage_ledger")
    op.drop_table("usage_ledger")

    op.drop_index("ix_audit_ledger_organization_action_occurred", table_name="audit_ledger")
    op.drop_index("ix_audit_ledger_organization_occurred", table_name="audit_ledger")
    op.drop_index("ix_audit_ledger_request_id", table_name="audit_ledger")
    op.drop_index(op.f("ix_audit_ledger_organization_id"), table_name="audit_ledger")
    op.drop_table("audit_ledger")

    op.drop_index(
        "ix_organization_quota_reservations_organization_state",
        table_name="organization_quota_reservations",
    )
    op.drop_index(
        op.f("ix_organization_quota_reservations_organization_id"),
        table_name="organization_quota_reservations",
    )
    op.drop_table("organization_quota_reservations")

    op.drop_index(
        "ix_organization_quota_counters_metric_window",
        table_name="organization_quota_counters",
    )
    op.drop_table("organization_quota_counters")
    op.drop_table("organization_quotas")


def _create_append_only_triggers() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION vma_reject_append_only_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in ("audit_ledger", "usage_ledger"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION vma_reject_append_only_mutation()
                """
            )
    elif dialect == "sqlite":
        for table in ("audit_ledger", "usage_ledger"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )


def _drop_append_only_triggers() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        for table in ("audit_ledger", "usage_ledger"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS vma_reject_append_only_mutation()")
    elif dialect == "sqlite":
        for table in ("audit_ledger", "usage_ledger"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
