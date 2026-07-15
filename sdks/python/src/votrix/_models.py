from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class VotrixModel(BaseModel):
    """Forward-compatible base for objects returned by the Votrix API."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OpenObject(VotrixModel):
    pass


ApiKeyScope = Literal["api", "api_keys:manage", "worker"]


class ApiKey(VotrixModel):
    """Safe API-key metadata returned by list/retrieve/revoke operations."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: str = "api_key"
    workspace_id: str
    name: str
    prefix: str
    scopes: list[ApiKeyScope]
    expires_at: datetime | None = None
    created_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None
    replaced_by_key_id: str | None = None
    replaces_key_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ApiKeyCreated(ApiKey):
    """One-time create/rotate response containing the plaintext secret."""

    secret: SecretStr


class ModelSpec(VotrixModel):
    id: str
    provider: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ModelSpec":
        if isinstance(value, str):
            return cls(id=value)
        return cls.model_validate(value)


class Agent(VotrixModel):
    id: str
    type: str = "agent"
    name: str | None = None
    version: int | None = None
    model: ModelSpec | None = None
    system: str | None = None
    description: str | None = None
    tools: list[OpenObject] = Field(default_factory=list)
    mcp_servers: list[OpenObject] = Field(default_factory=list)
    skills: list[OpenObject] = Field(default_factory=list)
    multiagent: OpenObject | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("model", mode="before")
    @classmethod
    def _normalize_model(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"id": value}
        return value


class Environment(VotrixModel):
    id: str
    type: str = "environment"
    name: str | None = None
    description: str | None = None
    config: OpenObject | None = None
    scope: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionResource(VotrixModel):
    id: str | None = None
    type: str
    file_id: str | None = None
    mount_path: str | None = None


class Session(VotrixModel):
    id: str
    type: str = "session"
    agent: Agent | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    environment_id: str | None = None
    title: str | None = None
    status: str | None = None
    status_details: dict[str, Any] = Field(default_factory=dict)
    stop_reason: OpenObject | None = None
    run_state: OpenObject | None = None
    sandbox_state: OpenObject | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[SessionResource] = Field(default_factory=list)
    outcome_evaluations: list[OpenObject] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    vault_ids: list[str] = Field(default_factory=list)
    last_event_seq: int | None = None
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    deployment_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionEvent(VotrixModel):
    id: str
    type: str
    session_id: str | None = None
    seq: int | None = None
    created_at: datetime | None = None
    processed_at: datetime | None = None


class SendEventsResult(VotrixModel):
    data: list[SessionEvent]


class FileObject(VotrixModel):
    id: str
    type: str = "file"
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    scope: OpenObject | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillVersion(VotrixModel):
    id: str
    type: str = "skill_version"
    skill_id: str | None = None
    version: str | int | None = None
    name: str | None = None
    description: str | None = None
    directory: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Skill(VotrixModel):
    id: str
    type: str = "skill"
    name: str | None = None
    display_title: str | None = None
    description: str | None = None
    source: str | None = None
    latest_version: str | int | None = None
    version: SkillVersion | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Vault(VotrixModel):
    id: str
    type: str = "vault"
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ModelCredential(VotrixModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: str = "model_credential"
    vault_id: str
    model_provider: str
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderCapabilities(VotrixModel):
    streaming: bool = False
    tool_calls: bool = False
    multimodal_input: bool = False
    reasoning: bool = False
    native_structured_output: bool = False


class ModelProvider(VotrixModel):
    id: str
    type: str = "model_provider"
    display_name: str
    adapter: str
    credential_type: str
    default_model: str | None = None
    capabilities: ProviderCapabilities


class DeletedObject(VotrixModel):
    id: str
    type: str
    deleted: bool = True


T = TypeVar("T", bound=VotrixModel)


class ListEnvelope(VotrixModel, Generic[T]):
    data: list[T]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None
    next_page: str | None = None
