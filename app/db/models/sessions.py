from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin

IDLE = "idle"
RUNNING = "running"
RESCHEDULING = "rescheduling"
TERMINATED = "terminated"
SESSION_STATUSES = (IDLE, RUNNING, RESCHEDULING, TERMINATED)

SANDBOX_PROVISIONING = "provisioning"
SANDBOX_RUNNING = "running"
SANDBOX_PAUSED = "paused"
SANDBOX_TERMINATED = "terminated"
SANDBOX_FAILED = "failed"
SANDBOX_STATES = (
    SANDBOX_PROVISIONING,
    SANDBOX_RUNNING,
    SANDBOX_PAUSED,
    SANDBOX_TERMINATED,
    SANDBOX_FAILED,
)


class Session(TimestampMixin, Base):
    """One conversation with an agent."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('idle', 'running', 'rescheduling', 'terminated')",
            name="ck_sessions_status",
        ),
        Index("ix_sessions_organization_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_id: Mapped[str] = mapped_column(
        ForeignKey("environments.id"),
        nullable=False,
    )
    # The model this conversation runs on, chosen when it opened and pinned for
    # its lifetime — same rule as agent_version above. NULL means the caller
    # expressed no preference and the pinned Agent version's own model applies,
    # resolved at run time rather than copied here, so that a conversation left
    # to the Agent's choice keeps following it.
    model: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Which Account pays for this Session, pinned when it opens and never moved.
    # Resolving it per turn instead would split one conversation's spend across
    # two Accounts the moment the Organization's default changed under it.
    #
    # NULL on Sessions that predate Accounts; those fall back to the
    # Organization's default at the point the key is resolved.
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization_accounts.id", ondelete="RESTRICT"),
    )
    title: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    stop_reason: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Held while a worker is busy with this session. New messages are refused
    # until it lapses, which is also what stops a dead worker from locking the
    # session out forever.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped every time a turn ends, however it ends. A worker records it when
    # it starts and stops as soon as it no longer matches — that is how an
    # interrupted worker learns it has been replaced.
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sandbox: Mapped[SessionSandbox | None] = relationship(
        back_populates="session",
        lazy="raise",
        passive_deletes=True,
        uselist=False,
    )


class SessionEvent(Base):
    """One entry in the conversation. Append-only; `seq` fixes the order.

    A message is only accepted while its session is free, so arrival order and
    conversation order are the same thing and `seq` alone is enough to sort by.
    """

    __tablename__ = "session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_session_events_session_seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )


class SessionFile(Base):
    """A file this session was given to work on.

    Inputs only. What a session produces is an ordinary file wearing this
    session's id as its scope — there is nothing to join, because an output
    belongs to exactly one session and has no life outside it. An input is the
    other way round: one stored file, mounted into any number of sessions at
    any number of paths, and that relationship is what this table is.
    """

    __tablename__ = "session_files"
    __table_args__ = (
        # Two files cannot land on one path — the second would silently
        # overwrite the first inside the container.
        UniqueConstraint("session_id", "path", name="uq_session_files_path"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"), nullable=False)
    # Relative to the sandbox's `uploads/` directory.
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )


class SessionSandbox(TimestampMixin, Base):
    """The container a session runs in. One per session."""

    __tablename__ = "session_sandboxes"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_sandboxes_session"),
        CheckConstraint(
            "state IN ('provisioning', 'running', 'paused', 'terminated', 'failed')",
            name="ck_session_sandboxes_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_sandbox_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    session: Mapped[Session] = relationship(back_populates="sandbox", lazy="raise")
