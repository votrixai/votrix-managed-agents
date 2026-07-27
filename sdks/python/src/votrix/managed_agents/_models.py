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
MemoryVersionOperation = Literal["created", "modified", "deleted"]
MemoryOperation = MemoryVersionOperation
MemoryView = Literal["basic", "full"]
SessionFundingType = Literal[
    "organization_default",
    "byok",
    "platform_credits",
]


class SessionFundingRequest(VotrixModel):
    type: SessionFundingType


class ApiKey(VotrixModel):
    """Safe API-key metadata returned by list/retrieve/revoke operations."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    type: str = "api_key"
    organization_id: str
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


class UsageEntry(VotrixModel):
    id: str
    type: str = "usage"
    organization_id: str
    metric: str
    quantity: int
    unit: str
    provider: str | None = None
    model: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class ModelUsage(VotrixModel):
    """Token usage for a single model request.

    Cached tokens are reported separately and are not included in
    ``input_tokens``, so the four counts sum to the request's total.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class StopReason(VotrixModel):
    """Why a Session went idle.

    ``requires_action`` means the Session is blocked on the caller and
    ``event_ids`` lists the tool-use events awaiting a result. Every other type
    is terminal for the turn.
    """

    type: str
    event_ids: list[str] = Field(default_factory=list)


class ContentBlock(VotrixModel):
    """One block of an event's content. ``text`` is set on text blocks."""

    type: str
    text: str | None = None


class RetryStatus(VotrixModel):
    type: str


class SessionError(VotrixModel):
    type: str
    message: str | None = None
    retry_status: RetryStatus | None = None


class SessionEvent(VotrixModel):
    """One Session event, with its payload parsed into typed attributes.

    The event surface is open — the server may add types and fields — so this is
    a single permissive model rather than a closed union: an unrecognized event
    still parses, with the fields it does not carry left as ``None`` and
    anything unmodelled reachable through ``model_extra``. Branch on ``type``,
    then read the fields that type carries:

    ``agent.message``
        ``content``
    ``agent.tool_use`` / ``agent.mcp_tool_use`` / ``agent.custom_tool_use``
        ``name``, ``input``
    ``agent.tool_result``
        ``tool_use_id`` (the id of the ``agent.tool_use`` event it completes),
        ``content``, ``is_error``
    ``agent.mcp_tool_result``
        ``mcp_tool_use_id``, ``content``, ``is_error``
    ``session.status_idle``
        ``stop_reason``
    ``session.error``
        ``error``
    ``span.model_request_end``
        ``model_request_start_id``, ``model_usage``
    """

    id: str | None = None
    type: str
    session_id: str | None = None
    seq: int | None = None
    created_at: datetime | None = None
    processed_at: datetime | None = None

    name: str | None = None
    input: dict[str, Any] | None = None
    # Managed Agents sends tool-result content either as blocks or as a bare
    # string, so both are preserved rather than normalized into one.
    content: list[ContentBlock] | str | None = None
    is_error: bool | None = None

    tool_use_id: str | None = None
    mcp_tool_use_id: str | None = None

    stop_reason: StopReason | None = None
    error: SessionError | None = None

    model_request_start_id: str | None = None
    model_usage: ModelUsage | None = None


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


class MemoryPrecondition(VotrixModel):
    type: Literal["content_sha256"] = "content_sha256"
    content_sha256: str | None = None


class MemoryStore(VotrixModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    name: str
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Memory(VotrixModel):
    id: str
    type: Literal["memory"] = "memory"
    memory_store_id: str
    memory_version_id: str
    path: str
    path_key: str
    content: str | None = None
    content_sha256: str
    content_size_bytes: int
    version: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    session_id: str | None = None
    redacted: bool = False
    redacted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListItem(VotrixModel):
    """A memory or a synthetic directory prefix returned by a depth-limited list."""

    type: Literal["memory", "memory_prefix"]
    path: str
    id: str | None = None
    memory_store_id: str | None = None
    memory_version_id: str | None = None
    content: str | None = None
    content_sha256: str | None = None
    content_size_bytes: int | None = None
    version: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryVersion(VotrixModel):
    id: str
    type: Literal["memory_version"] = "memory_version"
    memory_store_id: str
    memory_id: str
    operation: MemoryVersionOperation
    version: int
    memory_version: int | None = None
    path: str | None = None
    content: str | None = None
    content_sha256: str | None = None
    content_size_bytes: int | None = None
    created_by: dict[str, Any] | None = None
    redacted: bool = False
    redacted_at: datetime | None = None
    redacted_by: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


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
