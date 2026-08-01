from datetime import datetime
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
    title: str | None = Field(default=None, max_length=255)
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
    title: str | None = None
    status: str
    stop_reason: dict[str, Any] | None = None
    last_event_seq: int
    resources: list[
        SessionFileResourceResponse | SessionMemoryStoreResourceResponse
    ] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


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
