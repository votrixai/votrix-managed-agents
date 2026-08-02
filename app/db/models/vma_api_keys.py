from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin


VMA_API_SCOPE = "api"


class VmaApiKey(TimestampMixin, Base):
    """An Organization credential whose plaintext is never persisted."""

    __tablename__ = "vma_api_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_vma_api_keys_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "replaces_key_id"],
            ["vma_api_keys.organization_id", "vma_api_keys.id"],
            name="fk_vma_api_keys_replaces_key",
        ),
        UniqueConstraint("key_hash", name="uq_vma_api_keys_key_hash"),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_vma_api_keys_organization_id",
        ),
        UniqueConstraint("replaces_key_id", name="uq_vma_api_keys_replaces_key"),
        CheckConstraint(
            "replaces_key_id IS NULL OR id <> replaces_key_id",
            name="ck_vma_api_keys_not_self_replacing",
        ),
        Index(
            "ix_vma_api_keys_organization_revoked",
            "organization_id",
            "revoked_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: [VMA_API_SCOPE],
        server_default=text("'[\"api\"]'"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(128))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    replaces_key_id: Mapped[str | None] = mapped_column(String(64))
