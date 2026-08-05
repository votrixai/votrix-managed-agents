"""Agent use cases.

An agent is a stable handle; its definition lives in immutable versions. Every
edit mints a new one, and a session pins the version it started with, so
changing an agent never disturbs a conversation already in flight.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, AgentVersion
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import agents as agents_q
from app.models.errors import Conflict, NotFound


async def create_agent(
    db: AsyncSession,
    *,
    organization_id: str,
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
) -> tuple[Agent, AgentVersion]:
    agent, version = await agents_q.create_agent(
        db,
        organization_id=organization_id,
        name=name,
        model=normalize_model(model),
        system=system,
        description=description,
        tools=tools,
        mcp_servers=mcp_servers,
        skills=skills,
        multiagent=multiagent,
        metadata=metadata,
        runtime=runtime,
    )
    await db.commit()
    return agent, version


async def get_agent(db: AsyncSession, *, agent_id: str, organization_id: str) -> Agent:
    agent = await agents_q.get_agent(db, agent_id=agent_id, organization_id=organization_id)
    if agent is None:
        raise NotFound(f"Agent {agent_id} not found")
    return agent


async def get_version(
    db: AsyncSession,
    *,
    agent_id: str,
    organization_id: str,
    version: int,
) -> AgentVersion:
    found = await agents_q.get_agent_version(
        db,
        agent_id=agent_id,
        version=version,
        organization_id=organization_id,
    )
    if found is None:
        raise NotFound(f"Agent {agent_id} has no version {version}")
    return found


async def get_active_version(db: AsyncSession, agent: Agent) -> AgentVersion:
    return await get_version(
        db,
        agent_id=agent.id,
        organization_id=agent.organization_id,
        version=agent.active_version,
    )


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
) -> tuple[Page, list[tuple[Agent, AgentVersion]]]:
    """The page, and each agent paired with the version it is currently on.

    The page comes back too because the cursor ids belong to the agents, not to
    the pairs — the caller needs both to answer "what next".
    """
    agents = await agents_q.list_agents(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return agents, [(agent, await get_active_version(db, agent)) for agent in agents.items]


async def list_versions(
    db: AsyncSession,
    *,
    agent_id: str,
    organization_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> list[AgentVersion]:
    await get_agent(db, agent_id=agent_id, organization_id=organization_id)
    return await agents_q.list_agent_versions(
        db,
        agent_id=agent_id,
        organization_id=organization_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def update_agent(
    db: AsyncSession,
    *,
    agent_id: str,
    organization_id: str,
    changes: dict[str, Any],
) -> tuple[Agent, AgentVersion]:
    """Apply an edit on top of the active version.

    `changes` holds only the fields the client actually sent, so anything it
    left out keeps the active version's value rather than being blanked.
    """
    agent = await get_agent(db, agent_id=agent_id, organization_id=organization_id)
    if agent.archived_at is not None:
        raise Conflict("Archived agents cannot be updated")

    active = await get_active_version(db, agent)
    expected = changes.pop("version")
    if expected != agent.active_version:
        raise Conflict(
            f"Version mismatch: the agent is on version {agent.active_version}, "
            f"the edit was made against {expected}"
        )

    if "model" in changes:
        changes["model"] = normalize_model(changes["model"])
    if "metadata" in changes:
        changes["metadata_"] = changes.pop("metadata")

    config = {field: getattr(active, field) for field in agents_q.VERSIONED_FIELDS}
    config.update({k: v for k, v in changes.items() if k in agents_q.VERSIONED_FIELDS})

    version, _created = await agents_q.add_agent_version(db, agent, active, config=config)
    await db.commit()
    return agent, version


async def archive_agent(
    db: AsyncSession,
    *,
    agent_id: str,
    organization_id: str,
) -> tuple[Agent, AgentVersion]:
    agent = await get_agent(db, agent_id=agent_id, organization_id=organization_id)
    version = await get_active_version(db, agent)
    await agents_q.archive_agent(db, agent)
    await db.commit()
    return agent, version


def normalize_model(value: str | dict[str, Any]) -> dict[str, Any]:
    """`"claude-opus-5"` and `{"id": "claude-opus-5"}` mean the same thing.

    Shared with Sessions, which accept a model on the same terms — one spelling
    of the shorthand, so the two never drift into disagreeing about what a bare
    string means.
    """
    return {"id": value} if isinstance(value, str) else value
