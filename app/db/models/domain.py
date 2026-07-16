from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.models._base import Base, TimestampMixin
from app.ids import new_id
from app.organization import resolve_organization_id


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("id")
    def validate_id(self, _key: str, value: str) -> str:
        return resolve_organization_id(value)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        Index("ix_api_keys_organization_revoked", "organization_id", "revoked_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(128))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    replaced_by_key_id: Mapped[str | None] = mapped_column(String(64))
    replaces_key_id: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationQuota(TimestampMixin, Base):
    __tablename__ = "organization_quotas"
    __table_args__ = (
        CheckConstraint(
            "requests_per_minute IS NULL OR requests_per_minute >= 0",
            name="ck_organization_quotas_requests_nonnegative",
        ),
        CheckConstraint(
            "max_active_work IS NULL OR max_active_work >= 0",
            name="ck_organization_quotas_active_work_nonnegative",
        ),
        CheckConstraint(
            "daily_model_tokens IS NULL OR daily_model_tokens >= 0",
            name="ck_organization_quotas_model_tokens_nonnegative",
        ),
        CheckConstraint(
            "storage_bytes IS NULL OR storage_bytes >= 0",
            name="ck_organization_quotas_storage_nonnegative",
        ),
    )

    organization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    max_active_work: Mapped[int | None] = mapped_column(Integer)
    daily_model_tokens: Mapped[int | None] = mapped_column(BigInteger)
    storage_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class OrganizationQuotaCounter(TimestampMixin, Base):
    __tablename__ = "organization_quota_counters"
    __table_args__ = (
        CheckConstraint("value >= 0", name="ck_organization_quota_counters_value_nonnegative"),
        CheckConstraint(
            "window_seconds >= 0",
            name="ck_organization_quota_counters_window_nonnegative",
        ),
        Index(
            "ix_organization_quota_counters_metric_window",
            "metric",
            "window_start",
        ),
    )

    organization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class OrganizationQuotaReservation(TimestampMixin, Base):
    __tablename__ = "organization_quota_reservations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "quota_name",
            "reference_id",
            name="uq_organization_quota_reservations_reference",
        ),
        CheckConstraint("amount > 0", name="ck_organization_quota_reservations_amount_positive"),
        CheckConstraint(
            "state IN ('active', 'released')",
            name="ck_organization_quota_reservations_state",
        ),
        Index(
            "ix_organization_quota_reservations_organization_state",
            "organization_id",
            "quota_name",
            "state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quota_name: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class AuditLedgerEntry(Base):
    __tablename__ = "audit_ledger"
    __table_args__ = (
        Index("ix_audit_ledger_organization_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_ledger_organization_action_occurred", "organization_id", "action", "occurred_at"),
        Index("ix_audit_ledger_request_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class UsageLedgerEntry(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_usage_ledger_organization_idempotency",
        ),
        CheckConstraint("quantity >= 0", name="ck_usage_ledger_quantity_nonnegative"),
        Index(
            "ix_usage_ledger_organization_metric_occurred",
            "organization_id",
            "metric",
            "occurred_at",
        ),
        Index(
            "ix_usage_ledger_organization_source",
            "organization_id",
            "source_type",
            "source_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class TenantIdempotencyRecord(TimestampMixin, Base):
    __tablename__ = "tenant_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "operation",
            "key_hash",
            name="uq_tenant_idempotency_organization_operation_key",
        ),
        CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="ck_tenant_idempotency_state",
        ),
        CheckConstraint(
            "state = 'in_progress' OR response_status IS NOT NULL",
            name="ck_tenant_idempotency_completed_response",
        ),
        Index(
            "ix_tenant_idempotency_organization_created",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_tenant_idempotency_state_updated",
            "state",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AppendOnlyLedgerError(RuntimeError):
    pass


def _reject_ledger_mutation(_mapper, _connection, target) -> None:
    raise AppendOnlyLedgerError(f"{target.__tablename__} is append-only")


for _ledger_model in (AuditLedgerEntry, UsageLedgerEntry):
    event.listen(_ledger_model, "before_update", _reject_ledger_mutation)
    event.listen(_ledger_model, "before_delete", _reject_ledger_mutation)


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list["AgentVersion"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AgentVersion(TimestampMixin, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
        UniqueConstraint("organization_id", "agent_id", "version", name="uq_agent_versions_organization_agent_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    system: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    mcp_servers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    multiagent: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="versions")


class Environment(TimestampMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_environments_organization_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedSession(TimestampMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_sessions_organization_id"),
        UniqueConstraint("organization_id", "runtime_thread_id", name="uq_sessions_organization_runtime_thread"),
        Index("ix_sessions_environment_status", "environment_id", "status"),
        Index("ix_sessions_agent_status", "agent_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), nullable=False)
    runtime_thread_id: Mapped[str] = mapped_column(String(64), nullable=False, default=lambda: new_id("thread"))
    title: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="idle")
    status_details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stop_reason: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sandbox_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    last_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sandbox: Mapped[SessionSandbox | None] = relationship(
        back_populates="session",
        lazy="raise",
        passive_deletes=True,
        uselist=False,
    )


class SessionSandbox(TimestampMixin, Base):
    __tablename__ = "session_sandboxes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_session_sandboxes_organization_session",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "session_id",
            name="uq_session_sandboxes_organization_session",
        ),
        UniqueConstraint(
            "provider",
            "external_sandbox_id",
            name="uq_session_sandboxes_provider_external",
        ),
        Index(
            "ix_session_sandboxes_organization_state_expires",
            "organization_id",
            "state",
            "expires_at",
        ),
        Index(
            "ix_session_sandboxes_provider_state_expires",
            "provider",
            "state",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_sandbox_id: Mapped[str | None] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="provisioning")
    template_id: Mapped[str | None] = mapped_column(String(512))
    region: Mapped[str | None] = mapped_column(String(128))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    session: Mapped[ManagedSession] = relationship(back_populates="sandbox")


class SessionEvent(Base):
    __tablename__ = "session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_session_events_session_seq"),
        Index("ix_session_events_session_type", "session_id", "type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class SessionEventIdempotency(TimestampMixin, Base):
    __tablename__ = "session_event_idempotency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "session_id"],
            ["sessions.organization_id", "sessions.id"],
            name="fk_session_event_idempotency_organization_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "session_id",
            "key_hash",
            name="uq_session_event_idempotency_organization_session_key",
        ),
        Index(
            "ix_session_event_idempotency_organization_session_created",
            "organization_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    work_id: Mapped[str | None] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ManagedResource(TimestampMixin, Base):
    __tablename__ = "managed_resources"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "parent_id",
            "version",
            name="uq_managed_resources_type_parent_version",
        ),
        UniqueConstraint(
            "organization_id",
            "resource_type",
            "parent_id",
            "version",
            name="uq_managed_resources_organization_type_parent_version",
        ),
        Index(
            "ix_managed_resources_organization_type_parent_deleted_name",
            "organization_id",
            "resource_type",
            "parent_id",
            "deleted_at",
            "name",
        ),
        Index(
            "ix_managed_resources_type_parent_status",
            "resource_type",
            "parent_id",
            "status",
        ),
        Index(
            "ix_managed_resources_type_status_created",
            "resource_type",
            "status",
            "created_at",
        ),
        Index(
            "ix_managed_resources_type_parent_name",
            "resource_type",
            "parent_id",
            "name",
        ),
        Index("ix_managed_resources_storage_backend", "storage_backend"),
        Index("ix_managed_resources_sha256", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Skill versions are epoch-microsecond identifiers for Anthropic SDK
    # compatibility, so PostgreSQL's 32-bit INTEGER is not large enough.
    version: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_type: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str | None] = mapped_column(String(1024))
    storage_backend: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(2048))
    storage_url: Mapped[str | None] = mapped_column(String(4096))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
