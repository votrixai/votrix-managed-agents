"""Persistent Memory Store metadata and its provider-side Volume binding."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin

VOLUME_PROVIDER_E2B = "e2b"
VOLUME_PROVIDER_R2 = "r2"
VOLUME_PROVIDERS = (VOLUME_PROVIDER_E2B, VOLUME_PROVIDER_R2)

VOLUME_PROVISIONING = "provisioning"
VOLUME_READY = "ready"
VOLUME_FAILED = "failed"
VOLUME_DELETING = "deleting"
VOLUME_DELETED = "deleted"
VOLUME_PROVISIONING_STATES = (
    VOLUME_PROVISIONING,
    VOLUME_READY,
    VOLUME_FAILED,
    VOLUME_DELETING,
    VOLUME_DELETED,
)

MEMORY_ACCESS_READ_WRITE = "read_write"
MEMORY_ACCESS_READ_ONLY = "read_only"
MEMORY_ACCESS_MODES = (MEMORY_ACCESS_READ_WRITE, MEMORY_ACCESS_READ_ONLY)

MEMORY_VERSION_CREATED = "created"
MEMORY_VERSION_MODIFIED = "modified"
MEMORY_VERSION_DELETED = "deleted"
MEMORY_VERSION_OPERATIONS = (
    MEMORY_VERSION_CREATED,
    MEMORY_VERSION_MODIFIED,
    MEMORY_VERSION_DELETED,
)


class MemoryStore(TimestampMixin, Base):
    """A durable logical filesystem that can be attached to many Sessions.

    The public identity and tenant lifecycle live here. File content does not:
    ``volume_locator`` tells the selected provider adapter where that content
    lives. It is nullable while provisioning because creating this row and
    creating the provider resource cannot be one transaction.
    """

    __tablename__ = "memory_stores"
    __table_args__ = (
        CheckConstraint(
            "volume_provider IN ('e2b', 'r2')",
            name="ck_memory_stores_volume_provider",
        ),
        CheckConstraint(
            "provisioning_status IN "
            "('provisioning', 'ready', 'failed', 'deleting', 'deleted')",
            name="ck_memory_stores_provisioning_status",
        ),
        Index(
            "ix_memory_stores_organization_lifecycle_created",
            "organization_id",
            "deleted_at",
            "archived_at",
            "created_at",
        ),
        Index(
            "ix_memory_stores_provider_provisioning_updated",
            "volume_provider",
            "provisioning_status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )

    # ``volume_provider`` names our adapter. ``volume_locator`` is deliberately
    # provider-specific: E2B stores a volume id/name; R2 stores a bucket/prefix.
    volume_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VOLUME_PROVIDER_E2B
    )
    volume_locator: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    provisioning_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VOLUME_PROVISIONING
    )
    provisioning_error: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionMemoryStore(TimestampMixin, Base):
    """A Memory Store mounted when one Session's Sandbox was created.

    The human-facing fields and path are snapshots. Renaming a Memory Store
    changes the mount slug for future Sessions but cannot move a directory in
    a Sandbox that already exists.
    """

    __tablename__ = "session_memory_stores"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "memory_store_id",
            name="uq_session_memory_stores_store",
        ),
        UniqueConstraint(
            "session_id",
            "mount_path",
            name="uq_session_memory_stores_mount_path",
        ),
        CheckConstraint(
            "access IN ('read_write', 'read_only')",
            name="ck_session_memory_stores_access",
        ),
        Index("ix_session_memory_stores_store", "memory_store_id", "session_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_store_id: Mapped[str] = mapped_column(
        ForeignKey("memory_stores.id"),
        nullable=False,
    )
    access: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MEMORY_ACCESS_READ_WRITE
    )
    instructions: Mapped[str | None] = mapped_column(Text)
    mount_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
