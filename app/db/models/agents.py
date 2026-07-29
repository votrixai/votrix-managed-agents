from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin


class Agent(TimestampMixin, Base):
    """The stable handle for an agent. Its definition lives in AgentVersion."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # `metadata` is taken by SQLAlchemy itself, hence the trailing underscore
    # on the attribute. The column keeps the name the API uses.
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersion(TimestampMixin, Base):
    """One immutable revision of an agent definition.

    Sessions pin the version they started with, so editing an agent never
    disturbs a run already in flight.
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    model: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    system: Mapped[str | None] = mapped_column(Text)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Accepted and stored so the API contract stays stable; the runtime skips it.
    mcp_servers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Also stored-but-unread for now: no subagents are spawned, and nothing
    # consumes `runtime`. They are here because the API contract has them.
    multiagent: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
