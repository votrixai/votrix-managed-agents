"""Persistent Memory Store metadata and its provider-side Volume binding."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, JSON, String, Text
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
