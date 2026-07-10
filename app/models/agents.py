from datetime import datetime
from typing import Any

from pydantic import Field

from app.db.models import Agent, AgentVersion
from app.models.common import ApiModel


class AgentCreateRequest(ApiModel):
    name: str
    model: str | dict[str, Any]
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class AgentUpdateRequest(ApiModel):
    version: int
    name: str | None = None
    model: str | dict[str, Any] | None = None
    system: str | None = None
    description: str | None = None
    tools: list[dict[str, Any]] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    skills: list[dict[str, Any]] | None = None
    multiagent: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None


class AgentVersionResponse(ApiModel):
    id: str
    type: str = "agent_version"
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


class AgentResponse(ApiModel):
    id: str
    type: str = "agent"
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
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


def version_to_response(version: AgentVersion) -> AgentVersionResponse:
    return AgentVersionResponse(
        id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        name=version.name,
        model=version.model,
        system=version.system,
        description=version.description,
        tools=version.tools,
        mcp_servers=version.mcp_servers,
        skills=version.skills,
        multiagent=version.multiagent,
        metadata=version.metadata_,
        runtime=version.runtime,
        created_at=version.created_at,
    )


def version_to_agent_response(agent: Agent, version: AgentVersion) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        name=version.name,
        version=version.version,
        model=version.model,
        system=version.system,
        description=version.description,
        tools=version.tools,
        mcp_servers=version.mcp_servers,
        skills=version.skills,
        multiagent=version.multiagent,
        metadata=version.metadata_,
        archived_at=agent.archived_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def agent_to_response(agent: Agent, version: AgentVersion) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        version=version.version,
        model=version.model,
        system=version.system,
        description=agent.description,
        tools=version.tools,
        mcp_servers=version.mcp_servers,
        skills=version.skills,
        multiagent=version.multiagent,
        metadata=agent.metadata_,
        archived_at=agent.archived_at,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )
