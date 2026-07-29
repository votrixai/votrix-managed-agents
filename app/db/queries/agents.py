from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, AgentVersion
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id

# Everything that lives on a version. Two versions equal on all of these are
# the same agent, which is how a no-op edit avoids minting a new one.
VERSIONED_FIELDS = (
    "name",
    "description",
    "model",
    "system",
    "tools",
    "mcp_servers",
    "skills",
    "multiagent",
    "runtime",
    "metadata_",
)


async def create_agent(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    model: dict[str, Any],
    system: str | None = None,
    description: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    skills: list[dict[str, Any]] | None = None,
    multiagent: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> tuple[Agent, AgentVersion]:
    """Create an agent together with its first version."""
    agent = Agent(
        id=new_id("agent"),
        organization_id=organization_id,
        name=name,
        description=description,
        active_version=1,
        metadata_=metadata or {},
    )
    db.add(agent)
    version = AgentVersion(
        id=new_id("av"),
        organization_id=organization_id,
        agent_id=agent.id,
        version=1,
        name=name,
        description=description,
        model=model,
        system=system,
        tools=tools or [],
        mcp_servers=mcp_servers or [],
        skills=skills or [],
        multiagent=multiagent,
        runtime=runtime or {},
        metadata_=metadata or {},
    )
    db.add(version)
    await db.flush()
    return agent, version


async def add_agent_version(
    db: AsyncSession,
    agent: Agent,
    active: AgentVersion,
    *,
    config: dict[str, Any],
) -> tuple[AgentVersion, bool]:
    """Append a version and point the agent at it, unless nothing changed.

    Returns `(version, created)`. Saving a form without touching anything is a
    common client behaviour, and it should not leave a trail of identical
    versions that sessions can pin to.
    """
    if all(getattr(active, field) == config[field] for field in VERSIONED_FIELDS):
        return active, False

    version = AgentVersion(
        id=new_id("av"),
        organization_id=agent.organization_id,
        agent_id=agent.id,
        version=agent.active_version + 1,
        **config,
    )
    db.add(version)
    agent.active_version = version.version
    agent.name = config["name"]
    agent.description = config["description"]
    agent.metadata_ = config["metadata_"]
    await db.flush()
    return version, True


async def get_agent(db: AsyncSession, *, agent_id: str, organization_id: str) -> Agent | None:
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def list_agents(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    created_at_gte: datetime | None = None,
    created_at_lte: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(Agent).where(Agent.organization_id == organization_id)
    if not include_archived:
        stmt = stmt.where(Agent.archived_at.is_(None))
    if created_at_gte is not None:
        stmt = stmt.where(Agent.created_at >= created_at_gte)
    if created_at_lte is not None:
        stmt = stmt.where(Agent.created_at <= created_at_lte)
    return await fetch_page(
        db, stmt, sort=Agent.created_at, id_column=Agent.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )


async def count_agents(db: AsyncSession, *, organization_id: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Agent)
        .where(Agent.organization_id == organization_id, Agent.archived_at.is_(None))
    )
    return int(result.scalar_one())


async def archive_agent(db: AsyncSession, agent: Agent) -> None:
    agent.archived_at = datetime.now(timezone.utc)
    await db.flush()


async def get_agent_version(
    db: AsyncSession,
    *,
    agent_id: str,
    version: int,
    organization_id: str,
) -> AgentVersion | None:
    result = await db.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.version == version,
            AgentVersion.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def list_agent_versions(
    db: AsyncSession,
    *,
    agent_id: str,
    organization_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(AgentVersion).where(
        AgentVersion.agent_id == agent_id,
        AgentVersion.organization_id == organization_id,
    )
    # Ordered by version rather than by time: versions are the one thing here
    # with a number of their own, and it is the number a caller reasons about.
    return await fetch_page(
        db, stmt, sort=AgentVersion.version, id_column=AgentVersion.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )
