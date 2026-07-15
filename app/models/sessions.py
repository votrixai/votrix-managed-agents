from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.db.models import ManagedSession
from app.models.common import ApiModel, FlexibleApiModel


class AgentReference(ApiModel):
    type: Literal["agent"] = "agent"
    id: str
    version: int | None = None


class AgentWithOverrides(ApiModel):
    type: Literal["agent_with_overrides"]
    id: str
    version: int | None = None
    model: str | dict[str, Any] | None = None
    system: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)


class SessionCreateRequest(ApiModel):
    agent: str | AgentReference | AgentWithOverrides
    environment_id: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    vault_ids: list[str] = Field(default_factory=list)


class SessionUpdateRequest(ApiModel):
    title: str | None = None
    metadata: dict[str, Any] | None = None
    agent: dict[str, Any] | None = None
    vault_ids: list[str] | None = None


class SessionResponse(ApiModel):
    id: str
    type: str = "session"
    agent: dict[str, Any] | None = None
    agent_id: str
    agent_version: int
    environment_id: str
    title: str | None = None
    status: str
    status_details: dict[str, Any] = Field(default_factory=dict)
    stop_reason: dict[str, Any] | None = None
    run_state: dict[str, Any] | None = None
    sandbox_state: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    outcome_evaluations: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    vault_ids: list[str] = Field(default_factory=list)
    last_event_seq: int
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    deployment_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionDeletedResponse(ApiModel):
    id: str
    type: Literal["session_deleted"] = "session_deleted"
    deleted: Literal[True] = True


class SessionFileResourceCreateRequest(FlexibleApiModel):
    type: Literal["file"] = "file"
    file_id: str = Field(description="Identifier of an uploaded file to copy into the Session scope.")
    mount_path: str | None = Field(
        default=None,
        description="Absolute read-only path where the Session runtime mounts the file.",
    )


class SessionResourceTokenRotateRequest(FlexibleApiModel):
    authorization_token: str = Field(
        description="Replacement authorization token for a legacy GitHub repository resource.",
    )


class SessionFileResourceResponse(FlexibleApiModel):
    id: str
    type: Literal["file"] = "file"
    file_id: str = Field(description="Identifier of the isolated Session-scoped file copy.")
    mount_path: str = Field(description="Absolute path where the runtime mounts the file.")
    created_at: datetime
    updated_at: datetime


class SessionGithubResourceResponse(FlexibleApiModel):
    id: str
    type: Literal["github_repository"] = "github_repository"
    url: str = Field(description="Repository URL associated with this legacy resource.")
    mount_path: str = Field(description="Absolute path where the runtime mounts the repository.")
    checkout: dict[str, str] | None = Field(
        default=None,
        description="Pinned branch or commit checkout configuration.",
    )
    created_at: datetime
    updated_at: datetime


class SessionMemoryResourceResponse(FlexibleApiModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    memory_store_id: str = Field(description="Identifier of the attached memory store.")
    access: Literal["read_only", "read_write"] = Field(description="Access granted to the Session runtime.")
    description: str
    mount_path: str | None = Field(default=None, description="Runtime mount path for the memory store.")
    name: str | None = None
    instructions: str | None = Field(default=None, description="Session-specific instructions for using this memory store.")
    created_at: datetime
    updated_at: datetime


class SessionGenericResourceResponse(FlexibleApiModel):
    """Compatibility shape for resource types created by older deployments."""

    id: str
    type: str
    created_at: datetime
    updated_at: datetime


SessionResourceResponse = (
    SessionFileResourceResponse
    | SessionGithubResourceResponse
    | SessionMemoryResourceResponse
    | SessionGenericResourceResponse
)


class SessionResourceDeletedResponse(ApiModel):
    id: str
    type: Literal["session_resource_deleted"] = "session_resource_deleted"
    deleted: Literal[True] = True


def session_to_response(
    session: ManagedSession,
    *,
    agent: dict[str, Any] | None = None,
    resources: list[dict[str, Any]] | None = None,
) -> SessionResponse:
    details = session.status_details or {}
    public_details = _public_status_details(details)
    return SessionResponse(
        id=session.id,
        agent=agent,
        agent_id=session.agent_id,
        agent_version=session.agent_version,
        environment_id=session.environment_id,
        title=session.title,
        status=session.status,
        status_details=public_details,
        stop_reason=session.stop_reason,
        run_state=session.run_state,
        sandbox_state=session.sandbox_state,
        metadata=session.metadata_,
        resources=resources if resources is not None else list(details.get("resources") or []),
        outcome_evaluations=list(details.get("outcome_evaluations") or []),
        stats=dict(details.get("stats") or {}),
        usage=dict(details.get("usage") or {}),
        vault_ids=list(details.get("vault_ids") or []),
        last_event_seq=session.last_event_seq,
        archived_at=session.archived_at,
        deleted_at=session.deleted_at,
        deployment_id=details.get("deployment_id"),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _public_status_details(details: dict[str, Any]) -> dict[str, Any]:
    """Remove control-plane-only model credential coordinates from responses."""

    public = dict(details)
    binding = details.get("model_credential_binding")
    if isinstance(binding, dict):
        public["model_credential_binding"] = {
            key: binding[key]
            for key in (
                "version",
                "source",
                "credential_id",
                "vault_id",
                "model_provider",
            )
            if key in binding
        }
    return public
