from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.sessions import Session

# A container is never collected on a timer. When its timeout passes the
# provider pauses it — the filesystem stays, and the next call resumes it in
# about a second — and it stays paused until something kills it on purpose.
#
# That is why there is no `expired` here. Nothing expires; things are ended.
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

# States a container can still be worked in. `paused` counts: it wakes up on
# the next call, which is the normal state of anything nobody is using.
LIVE_SANDBOX_STATES = (SANDBOX_PROVISIONING, SANDBOX_RUNNING, SANDBOX_PAUSED)

_STATE_LIST = ", ".join(f"'{state}'" for state in SANDBOX_STATES)


class Sandbox(TimestampMixin, Base):
    """One container, whoever it belongs to.

    This was two tables — `session_sandboxes` and one for containers held
    directly through the API. They carried the same columns bar three, were
    driven by the same class in `app.utils.sandbox`, and differed only in who
    is allowed to end them. That is an owner, not a second kind of thing, and
    `session_id` already says which it is: set means a conversation owns this
    container and only deleting that conversation can destroy it; null means
    whoever asked for it owns it and may delete it whenever.

    There is deliberately no `kind` column. It would be a second copy of what
    the foreign key already states, and a second copy is a thing that can
    disagree with the first.
    """

    __tablename__ = "sandboxes"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="ck_sandboxes_state"),
        CheckConstraint("ttl_seconds > 0", name="ck_sandboxes_ttl_positive"),
        # One container per conversation, and any number belonging to none. A
        # plain unique constraint would allow only a single API-held container
        # in the whole table, because those all have a NULL session.
        Index(
            "uq_sandboxes_session",
            "session_id",
            unique=True,
            sqlite_where=text("session_id IS NOT NULL"),
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        Index("ix_sandboxes_organization_state", "organization_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # No index of its own: `ix_sandboxes_organization_state` leads with this
    # column, so it already serves a lookup by tenant alone.
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Who owns this container's lifetime. `ondelete="CASCADE"` is what makes a
    # deleted conversation take its container's record with it — and the
    # service kills the container itself first, or it would be left running at
    # the provider with nothing left that knows its id.
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    # Which image it started from. Null only on a row written by a release
    # that predates this column; a session's environment is on the session.
    environment_id: Mapped[str | None] = mapped_column(ForeignKey("environments.id"))

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_sandbox_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)

    # How long the provider is told to keep this container before pausing it.
    # Every call pushes it out again, so it is an idle timeout rather than a
    # lifetime.
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Recorded rather than merely applied, so "could this container have
    # reached the internet" stays answerable about one that no longer exists.
    network_access: Mapped[bool] = mapped_column(Boolean, nullable=False)

    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Optimistic locking, so an interrupted worker learns it was replaced.
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    session: Mapped[Session | None] = relationship(
        back_populates="sandbox", lazy="raise"
    )

    @property
    def is_session_owned(self) -> bool:
        """Whether a conversation decides when this container ends."""
        return self.session_id is not None
