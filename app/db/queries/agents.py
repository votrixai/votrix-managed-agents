from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Agent, AgentVersion
from app.ids import new_id
from app.organization import resolve_organization_id


def _model_json(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        return {"id": value}
    return value


async def create_agent(
    db: AsyncSession,
    *,
    name: str,
    model: str | dict[str, Any],
    system: str | None = None,
    description: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    skills: list[dict[str, Any]] | None = None,
    multiagent: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    organization_id: str | None = None,
) -> tuple[Agent, AgentVersion]:
    scoped_organization_id = resolve_organization_id(organization_id)
    agent = Agent(
        id=new_id("agt"),
        organization_id=scoped_organization_id,
        name=name,
        description=description,
        active_version=1,
        metadata_=metadata or {},
    )
    version = AgentVersion(
        id=new_id("agtv"),
        organization_id=scoped_organization_id,
        agent_id=agent.id,
        version=1,
        name=name,
        model=_model_json(model),
        system=system,
        description=description,
        tools=tools or [],
        mcp_servers=mcp_servers or [],
        skills=skills or [],
        multiagent=multiagent,
        metadata_=metadata or {},
        runtime=runtime or {},
    )
    db.add(agent)
    db.add(version)
    await db.flush()
    return agent, version


async def get_agent(
    db: AsyncSession,
    agent_id: str,
    *,
    organization_id: str | None = None,
) -> Agent | None:
    organization_id = resolve_organization_id(organization_id)
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.versions))
        .where(Agent.id == agent_id, Agent.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def list_agents(db: AsyncSession, *, limit: int = 50, include_archived: bool = False) -> list[Agent]:
    organization_id = resolve_organization_id()
    stmt = (
        select(Agent)
        .options(selectinload(Agent.versions))
        .where(Agent.organization_id == organization_id)
        .order_by(Agent.created_at.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(Agent.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_agent_version(
    db: AsyncSession,
    *,
    agent_id: str,
    version: int,
    organization_id: str | None = None,
) -> AgentVersion | None:
    organization_id = resolve_organization_id(organization_id)
    result = await db.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.version == version,
            AgentVersion.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def get_active_agent_version(db: AsyncSession, agent: Agent) -> AgentVersion | None:
    return await get_agent_version(
        db,
        agent_id=agent.id,
        version=agent.active_version,
        organization_id=agent.organization_id,
    )


async def update_agent(
    db: AsyncSession,
    agent: Agent,
    *,
    name: str,
    model: dict[str, Any],
    system: str | None,
    description: str | None = None,
    tools: list[dict[str, Any]],
    mcp_servers: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    multiagent: dict[str, Any] | None = None,
    metadata: dict[str, Any],
    runtime: dict[str, Any],
) -> tuple[AgentVersion, bool]:
    active = await get_active_agent_version(db, agent)
    if active is None:
        raise ValueError(f"Agent {agent.id} has no active version")

    if (
        active.name == name
        and active.description == description
        and active.metadata_ == metadata
        and active.model == model
        and active.system == system
        and active.tools == tools
        and active.mcp_servers == mcp_servers
        and active.skills == skills
        and active.multiagent == multiagent
        and active.runtime == runtime
    ):
        return active, False

    next_version = agent.active_version + 1
    agent.active_version = next_version
    agent.name = name
    agent.description = description
    agent.metadata_ = metadata

    version = AgentVersion(
        id=new_id("agtv"),
        organization_id=agent.organization_id,
        agent_id=agent.id,
        version=next_version,
        name=name,
        model=model,
        system=system,
        description=description,
        tools=tools,
        mcp_servers=mcp_servers,
        skills=skills,
        multiagent=multiagent,
        metadata_=metadata,
        runtime=runtime,
    )
    db.add(version)
    await db.flush()
    return version, True


async def archive_agent(db: AsyncSession, agent: Agent) -> Agent:
    agent.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return agent


def agent_versions_query(agent_id: str) -> Select[tuple[AgentVersion]]:
    organization_id = resolve_organization_id()
    return (
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id, AgentVersion.organization_id == organization_id)
        .order_by(AgentVersion.version.desc())
    )
