from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from app.models.common import ApiModel

# --- session.status --------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
RESCHEDULING = "rescheduling"
TERMINATED = "terminated"

# --- stop_reason.type on a session.status_idle event -----------------------

STOP_END_TURN = "end_turn"
STOP_REQUIRES_ACTION = "requires_action"
# Ours, not CMA's. CMA reports an interrupted turn as `end_turn`, which leaves
# the client unable to tell a reply that finished from one that was cut off.
STOP_INTERRUPTED = "interrupted"
# Also ours. The `session.error` event beside it says what went wrong; this
# only says the turn is over, which a client waiting for idle has to hear.
STOP_ERROR = "error"

class FileResource(ApiModel):
    """An uploaded file to put in the container before the session starts.

    `path` is relative to the sandbox's `uploads/` directory and defaults to the
    file's own name. It is relative on purpose: an absolute path would let an
    upload land on `skills/`, which the agent reads as instructions, so the
    contract has no way to say it rather than a rule against saying it.
    """

    type: Literal["file"] = "file"
    file_id: str
    path: str | None = Field(default=None, max_length=512)


class MemoryStoreResource(ApiModel):
    """A persistent Store mounted once, when the Session Sandbox is created."""

    type: Literal["memory_store"] = "memory_store"
    memory_store_id: str
    access: Literal["read_write", "read_only"] = "read_write"
    instructions: str | None = Field(default=None, max_length=4096)


class SessionCreateRequest(ApiModel):
    agent_id: str
    environment_id: str
    agent_version: int | None = None
    model: str | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional model for this Session, pinned for its lifetime. When "
            "omitted or null the pinned Agent version's own model applies, "
            "resolved at run time — so a Session that expresses no preference "
            "keeps following the Agent instead of freezing a copy of it. A bare "
            "string is shorthand for `{\"id\": ...}`, as on an Agent."
        ),
        examples=["claude-opus-5"],
    )
    account_id: str | None = Field(
        default=None,
        description=(
            "Which Account pays for this Session. Omit to use the "
            "Organization's default. Pinned when the Session opens, so its "
            "spend stays on one Account for the whole conversation."
        ),
    )
    title: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Optional caller operation identity, scoped to the Organization. "
            "Repeating a successful create with the same value returns the "
            "original Session instead of provisioning another sandbox."
        ),
    )
    # Attached once, when the container is built. A session cannot be given
    # more later — the sandbox is created with the session and never rebuilt.
    resources: list[FileResource | MemoryStoreResource] = Field(
        default_factory=list,
        max_length=100,
    )


class SessionUpdateRequest(ApiModel):
    title: str | None = Field(default=None, max_length=255)


class SessionFileResourceResponse(ApiModel):
    id: str
    type: Literal["file"] = "file"
    file_id: str
    mount_path: str
    created_at: datetime
    updated_at: datetime


class SessionMemoryStoreResourceResponse(ApiModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    memory_store_id: str
    access: Literal["read_write", "read_only"]
    instructions: str | None = None
    mount_path: str
    name: str
    description: str = ""
    created_at: datetime
    updated_at: datetime


class SessionResponse(ApiModel):
    id: str
    type: Literal["session"] = "session"
    agent_id: str
    agent_version: int
    environment_id: str
    model: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The model pinned to this Session, or null when it follows the "
            "Agent version's."
        ),
    )
    # Which Account this Session is billed to. Read back rather than assumed:
    # omitting it on create resolves the Organization's default, and this says
    # which one that turned out to be.
    account_id: str | None = None
    title: str | None = None
    status: str
    stop_reason: dict[str, Any] | None = None
    last_event_seq: int
    # The container this Session runs in. Created with the Session and kept for
    # its whole life, so it is stated here rather than left to be discovered:
    # a caller that wants to run something against the conversation's own files
    # would otherwise have to list every sandbox and filter, which is the same
    # answer reached the long way round.
    sandbox_id: str | None = None
    resources: list[
        SessionFileResourceResponse | SessionMemoryStoreResourceResponse
    ] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class SessionUsageResponse(ApiModel):
    """OpenRouter's current cumulative USD spend for one Session.

    This is a live provider snapshot, not a locally accumulated receipt. Calling
    the endpoint again returns the Session's new lifetime total; subtract two
    readings to settle only the spend since the earlier one.
    """

    session_id: str
    type: Literal["session_usage"] = "session_usage"
    account_id: str
    usage_usd: Decimal
    snapshot: Literal["cumulative"] = "cumulative"
    source: Literal["openrouter"] = "openrouter"
    as_of: datetime


class SessionBusyError(ApiModel):
    """Body of the 409 returned when a session is still working.

    There is no queue, so a message that arrives mid-reply is refused rather
    than held. No retry hint comes with it: how long the agent still needs is
    not something this service can know, and the lease — which is renewed for
    as long as the worker lives — only measures how quickly a dead one is
    noticed.
    """

    type: Literal["session_busy"] = "session_busy"
    message: str
