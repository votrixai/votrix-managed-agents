from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import ApiModel


class AgentCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    # A bare string is accepted as shorthand for `{"id": ...}`.
    model: str | dict[str, Any]
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    # Accepted and stored so the contract stays stable; the runtime skips it.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class AgentUpdateRequest(ApiModel):
    """An edit, stated against the version it was made from.

    `version` is required. A client that built its form from version 5 would
    otherwise write its stale copy of every field it did not touch back over
    whatever version 6 changed. Sending the wrong one is a 409, not a silent
    overwrite.
    """

    version: int
    name: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | dict[str, Any] | None = None
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    skills: list[dict[str, Any]] | None = None
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None


class AgentResponse(ApiModel):
    id: str
    type: Literal["agent"] = "agent"
    name: str
    version: int
    model: dict[str, Any]
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class AgentVersionResponse(ApiModel):
    id: str
    type: Literal["agent_version"] = "agent_version"
    agent_id: str
    version: int
    name: str
    model: dict[str, Any]
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
