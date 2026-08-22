"""Request and response shapes for the sandbox API.

The endpoints are named after what they do rather than shaped as a REST
resource, because what a caller wants here is a handful of verbs on a
container it already holds — `create`, `exec`, `delete` — and there is no
useful sense in which `exec` is a POST to a collection.

The container's id is the whole contract. A caller creates one, keeps the id,
runs as many commands against it as it likes, and deletes it. Nothing reuses
containers on the caller's behalf: a caller that wants a warm one holds onto
its id, and a caller that does not lets the TTL collect it.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel

# The two ends of how long a container may be kept. The ceiling is what bounds
# the cost of a caller that never deletes: a leaked container is billed for at
# most this long, whatever happens to the process that made it.
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600

# How long one command may run, and how long a caller may block waiting for
# it. They are separate because a long command is normal — waiting on it
# inside an HTTP request is not.
MAX_COMMAND_TIMEOUT_SECONDS = 900
MAX_WAIT_SECONDS = 120

# What comes back of a command's output. Anything past this stays in the
# container, where the caller can read it as a file for as long as the
# container lives. It is not a limit on what a command may print: the
# redirection that enforces it happens inside the sandbox, so a command that
# writes a gigabyte writes it to the container's own disk and this process
# never sees it.
MAX_OUTPUT_CHARS = 262_144

EXEC_RUNNING = "running"
EXEC_SUCCEEDED = "succeeded"
EXEC_FAILED = "failed"
EXEC_TIMED_OUT = "timed_out"


class SandboxCreateRequest(ApiModel):
    environment_id: str = Field(min_length=1, max_length=64)
    ttl_seconds: int = Field(
        default=300, ge=MIN_TTL_SECONDS, le=MAX_TTL_SECONDS
    )
    # On by default, matching the containers agents run in. A sandbox that
    # cannot reach the network cannot install anything, which is most of what
    # a general-purpose one is for; and it would be a strange asymmetry for a
    # command someone wrote deliberately to be more restricted than one a
    # model chose. A caller who knows its workload needs nothing turns it off.
    network_access: bool = True


class SandboxGetRequest(ApiModel):
    sandbox_id: str = Field(min_length=1, max_length=64)


class SandboxDeleteRequest(ApiModel):
    sandbox_id: str = Field(min_length=1, max_length=64)


class SandboxListRequest(ApiModel):
    state: (
        Literal["provisioning", "running", "paused", "terminated", "failed"] | None
    ) = None
    # Containers a conversation owns are left out by default: they cannot be
    # deleted here and are listed properly under `/v1/sessions`.
    include_session_owned: bool = False
    limit: int = Field(default=20, ge=1, le=1000)
    before_id: str | None = Field(default=None, max_length=64)
    after_id: str | None = Field(default=None, max_length=64)


class SandboxResponse(ApiModel):
    id: str
    type: Literal["sandbox"] = "sandbox"
    environment_id: str
    # A container is never collected on a timer: past `expires_at` the
    # provider has paused it, and the next call wakes it in about a second.
    state: str
    # Null once the container belongs to nobody in particular. Set means a
    # conversation owns it and only deleting that conversation ends it.
    session_id: str | None = None
    ttl_seconds: int
    network_access: bool
    expires_at: datetime | None = None
    last_active_at: datetime | None = None
    error: dict | None = None
    created_at: datetime
    updated_at: datetime


class SandboxExecRequest(ApiModel):
    sandbox_id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=65_536)
    # Relative paths are resolved against the exec's own directory, which is
    # made fresh for every command and is where its output is kept.
    cwd: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(
        default=120, ge=1, le=MAX_COMMAND_TIMEOUT_SECONDS
    )
    # How long to hold the request open before answering `running`. Zero
    # starts the command and returns immediately.
    wait_seconds: int = Field(default=60, ge=0, le=MAX_WAIT_SECONDS)


class SandboxExecResultRequest(ApiModel):
    sandbox_id: str = Field(min_length=1, max_length=64)
    exec_id: str = Field(min_length=1, max_length=64)


class ExecResponse(ApiModel):
    id: str
    type: Literal["exec"] = "exec"
    sandbox_id: str
    state: str
    # Absent while the command is still running.
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    # True when the container held more than came back. The rest is on the
    # container's disk, under `dir`.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Where this command ran, and where its full output still is.
    dir: str
    duration_ms: int | None = None


class SandboxUploadRequest(ApiModel):
    """Put a stored VMA File into the container.

    The bytes go straight from object storage to the container over a
    short-lived signed URL; they do not pass through this service.
    """

    sandbox_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)


class SandboxDownloadRequest(ApiModel):
    """Take one file out of the container and give it a File row."""

    sandbox_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)
    filename: str | None = Field(default=None, max_length=255)
