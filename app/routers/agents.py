from datetime import datetime

from fastapi import APIRouter, Query, status

from app.db.models import Agent, AgentVersion
from app.models.agents import (
    AgentCreateRequest,
    AgentResponse,
    AgentUpdateRequest,
    AgentVersionResponse,
)
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import ListResponse, page_of
from app.routers.deps import Db, OrganizationId
from app.services import agents as service

router = APIRouter(prefix="/v1/agents", tags=["agents"])


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentCreateRequest, db: Db, organization_id: OrganizationId):
    agent, version = await service.create_agent(
        db,
        organization_id=organization_id,
        **body.model_dump(),
    )
    return to_agent(agent, version)


@router.get("", response_model=ListResponse[AgentResponse])
async def list_agents(
    db: Db,
    organization_id: OrganizationId,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
    include_archived: bool = False,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
):
    page, pairs = await service.list_agents(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return ListResponse[AgentResponse](
        data=[to_agent(a, v) for a, v in pairs],
        has_more=page.has_more,
        first_id=page.first_id,
        last_id=page.last_id,
    )


@router.get("/{agent_id}/versions", response_model=ListResponse[AgentVersionResponse])
async def list_agent_versions(
    agent_id: str,
    db: Db,
    organization_id: OrganizationId,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    versions = await service.list_versions(
        db,
        agent_id=agent_id,
        organization_id=organization_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(versions, to_agent_version)


@router.get("/{agent_id}", response_model=AgentResponse)
async def retrieve_agent(
    agent_id: str,
    db: Db,
    organization_id: OrganizationId,
    version: int | None = None,
):
    """The active version, or a named one via `?version=`."""
    agent = await service.get_agent(db, agent_id=agent_id, organization_id=organization_id)
    found = (
        await service.get_version(
            db, agent_id=agent_id, organization_id=organization_id, version=version
        )
        if version is not None
        else await service.get_active_version(db, agent)
    )
    return to_agent(agent, found)


@router.post("/{agent_id}", response_model=AgentResponse)
@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Edit the agent, minting a new version.

    `exclude_unset` is what makes this a patch: only the fields the client
    actually sent are applied, so omitting one keeps its current value instead
    of clearing it.
    """
    agent, version = await service.update_agent(
        db,
        agent_id=agent_id,
        organization_id=organization_id,
        changes=body.model_dump(exclude_unset=True),
    )
    return to_agent(agent, version)


@router.post("/{agent_id}/archive", response_model=AgentResponse)
async def archive_agent(agent_id: str, db: Db, organization_id: OrganizationId):
    agent, version = await service.archive_agent(
        db, agent_id=agent_id, organization_id=organization_id
    )
    return to_agent(agent, version)


def to_agent(agent: Agent, version: AgentVersion) -> AgentResponse:
    """An agent as seen through one of its versions.

    Almost everything comes off the version, because that is where an agent's
    definition actually lives — the row named `agents` is only the handle.
    """
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
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        archived_at=agent.archived_at,
    )


def to_agent_version(version: AgentVersion) -> AgentVersionResponse:
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
