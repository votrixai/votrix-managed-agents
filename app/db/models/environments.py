from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin

BUILDING = "building"
READY = "ready"
FAILED = "failed"
BUILD_STATES = (BUILDING, READY, FAILED)


class Environment(TimestampMixin, Base):
    """An image recipe. Declare what should be installed; we build it once.

    Sessions do not share an environment's container — each gets a fresh one
    started from the image this describes. That is why the packages are baked
    in rather than installed per session: install once, start in under a second
    however many times.
    """

    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The caller's own label for this environment. Nothing downstream reads it,
    # and it is deliberately not unique: an environment is never edited, so
    # changing a recipe means registering a new one beside the old, which the
    # sessions already running still point at. Two under one label is the
    # normal shape of that history — the id is what identifies them apart.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # {"packages": {"pip": [...], "apt": [...], ...}}
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # The image itself, over at the sandbox provider — the provider's own id,
    # not the name we asked for. Null means nothing was declared, so sessions
    # start from the platform's default image.
    image_id: Mapped[str | None] = mapped_column(String(255))
    # Handed back when a build starts; the only way to ask how it went, since
    # nothing calls us when it finishes.
    build_id: Mapped[str | None] = mapped_column(String(255))
    build_state: Mapped[str] = mapped_column(String(32), nullable=False, default=READY)
    build_error: Mapped[str | None] = mapped_column(Text)

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
