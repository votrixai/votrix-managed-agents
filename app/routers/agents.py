from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_contract import normalize_agent_tools, validate_mcp_bindings
from app.auth import require_api_access
from app.db.engine import get_session
from app.db.queries import agents as agents_q
from app.db.queries import resources as res_q
from app.metadata import merge_metadata, normalize_metadata
from app.models.agents import (
    AgentCreateRequest,
    AgentResponse,
    AgentUpdateRequest,
    agent_to_response,
    version_to_agent_response,
)
from app.models.common import ListResponse
from app.pagination import filter_created_at, paginate, sort_by_created_at

router = APIRouter(
    prefix="/v1/agents",
    tags=["agents"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=AgentResponse, status_code=201)
async def create_agent(
    body: AgentCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    name = _normalize_agent_name(body.name)
    model = _normalize_agent_model(body.model)
    tools = normalize_agent_tools(body.tools)
    validate_mcp_bindings(body.mcp_servers, tools)
    multiagent = await _normalize_multiagent_roster(db, body.multiagent)
    skills = await _normalize_skill_refs(db, body.skills)
    agent, version = await agents_q.create_agent(
        db,
        name=name,
        model=model,
        system=body.system,
        description=body.description,
        tools=tools,
        mcp_servers=body.mcp_servers,
        skills=skills,
        multiagent=multiagent,
        metadata=normalize_metadata(body.metadata),
        runtime=body.runtime,
    )
    if multiagent is not None:
        version.multiagent = _resolve_multiagent_self_entries(multiagent, agent_id=agent.id, version=version.version)
    await db.commit()
    return agent_to_response(agent, version)


@router.get("", response_model=ListResponse[AgentResponse])
async def list_agents(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    agents = await agents_q.list_agents(db, limit=1000, include_archived=include_archived)
    agents = filter_created_at(agents, created_at_gte=created_at_gte, created_at_lte=created_at_lte)
    agents = sort_by_created_at(agents, order="desc")
    responses: list[AgentResponse] = []
    for agent in agents:
        version = await agents_q.get_active_agent_version(db, agent)
        if version is not None:
            responses.append(agent_to_response(agent, version))
    return paginate(responses, limit=limit, page=page, max_limit=100)


@router.get("/{agent_id}/versions", response_model=ListResponse[AgentResponse])
async def list_agent_versions(
    agent_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = await db.execute(agents_q.agent_versions_query(agent_id))
    versions = [version_to_agent_response(agent, v) for v in result.scalars().all()]
    return paginate(versions, limit=limit, page=page, max_limit=100)


@router.get("/{agent_id}", response_model=AgentResponse)
async def retrieve_agent(
    agent_id: str,
    version: int | None = None,
    db: AsyncSession = Depends(get_session),
):
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent_version = (
        await agents_q.get_agent_version(db, agent_id=agent.id, version=version)
        if version is not None
        else await agents_q.get_active_agent_version(db, agent)
    )
    if agent_version is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    if version is not None:
        return version_to_agent_response(agent, agent_version)
    return agent_to_response(agent, agent_version)


@router.post("/{agent_id}", response_model=AgentResponse)
@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.archived_at is not None:
        raise HTTPException(status_code=409, detail="Archived agents cannot be updated")

    active = await agents_q.get_active_agent_version(db, agent)
    if active is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    if body.version != agent.active_version:
        raise HTTPException(
            status_code=409,
            detail=f"Version mismatch: expected {agent.active_version}, got {body.version}",
        )

    update = body.model_dump(exclude_unset=True)
    update.pop("version", None)
    if "multiagent" in update:
        update["multiagent"] = await _normalize_multiagent_roster(db, update["multiagent"], self_agent_id=agent.id)
    if "skills" in update:
        update["skills"] = await _normalize_skill_refs(db, update["skills"] or [])
    if "tools" in update:
        update["tools"] = normalize_agent_tools(update["tools"] or [])
    next_config = _merge_agent_update(active, agent, update)
    validate_mcp_bindings(next_config["mcp_servers"], next_config["tools"])
    version, _created = await agents_q.update_agent(
        db,
        agent,
        **next_config,
    )
    if version.multiagent is not None:
        version.multiagent = _resolve_multiagent_self_entries(version.multiagent, agent_id=agent.id, version=version.version)
    await db.commit()
    return agent_to_response(agent, version)


@router.post("/{agent_id}/archive", response_model=AgentResponse)
async def archive_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_session),
):
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    version = await agents_q.get_active_agent_version(db, agent)
    if version is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    await agents_q.archive_agent(db, agent)
    await db.commit()
    return agent_to_response(agent, version)


async def _normalize_multiagent_roster(
    db: AsyncSession,
    multiagent: dict | None,
    *,
    self_agent_id: str | None = None,
) -> dict | None:
    if multiagent is None:
        return None
    if not isinstance(multiagent, dict):
        raise HTTPException(status_code=422, detail="multiagent must be an object")

    normalized = dict(multiagent)
    if normalized.get("type") != "coordinator":
        raise HTTPException(status_code=422, detail='multiagent.type must be "coordinator"')

    agents = normalized.get("agents", [])
    if not isinstance(agents, list):
        raise HTTPException(status_code=422, detail="multiagent.agents must be an array")
    if not agents:
        raise HTTPException(status_code=422, detail="multiagent.agents must contain at least one agent")
    if len(agents) > 20:
        raise HTTPException(status_code=422, detail="multiagent.agents supports at most 20 agents")

    seen: set[str] = set()
    saw_self = False
    normalized_agents: list[dict] = []
    for entry in agents:
        if isinstance(entry, str):
            entry = {"type": "agent", "id": entry}
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail="multiagent.agents entries must be strings or objects")

        entry_type = entry.get("type")
        if entry_type == "self":
            if saw_self:
                raise HTTPException(status_code=422, detail="multiagent.agents may include at most one self entry")
            saw_self = True
            resolved_key = self_agent_id or "self"
            if resolved_key in seen:
                raise HTTPException(status_code=422, detail="multiagent.agents entries must reference distinct agents")
            seen.add(resolved_key)
            normalized_agents.append(dict(entry))
            continue

        if entry_type != "agent":
            raise HTTPException(status_code=422, detail='multiagent.agents entries must be "agent" or "self"')

        agent_id = entry.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            raise HTTPException(status_code=422, detail="multiagent agent entries require id")

        referenced_agent = await agents_q.get_agent(db, agent_id)
        if referenced_agent is None or referenced_agent.archived_at is not None:
            raise HTTPException(status_code=422, detail=f"Referenced agent not found: {agent_id}")

        raw_version = entry.get("version")
        try:
            pinned_version = referenced_agent.active_version if raw_version is None else int(raw_version)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="multiagent agent version must be an integer") from exc
        if pinned_version < 1:
            raise HTTPException(status_code=422, detail="multiagent agent version must be at least 1")
        referenced_version = await agents_q.get_agent_version(
            db,
            agent_id=agent_id,
            version=pinned_version,
            workspace_id=referenced_agent.workspace_id,
        )
        if referenced_version is None:
            raise HTTPException(status_code=422, detail=f"Referenced agent version not found: {agent_id}@{pinned_version}")
        if referenced_version.multiagent is not None:
            raise HTTPException(status_code=422, detail="multiagent agents cannot reference another multiagent agent")

        if agent_id in seen:
            raise HTTPException(status_code=422, detail="multiagent.agents entries must reference distinct agents")
        seen.add(agent_id)
        pinned_entry = dict(entry)
        pinned_entry["type"] = "agent"
        pinned_entry["id"] = agent_id
        pinned_entry["version"] = pinned_version
        normalized_agents.append(pinned_entry)

    normalized["agents"] = normalized_agents
    return normalized


def _resolve_multiagent_self_entries(multiagent: dict[str, Any], *, agent_id: str, version: int) -> dict[str, Any]:
    resolved = dict(multiagent)
    agents: list[dict[str, Any]] = []
    for entry in multiagent.get("agents") or []:
        if isinstance(entry, dict) and entry.get("type") == "self":
            agents.append({"type": "agent", "id": agent_id, "version": version})
        else:
            agents.append(dict(entry))
    resolved["agents"] = agents
    return resolved


async def _normalize_skill_refs(db: AsyncSession, skills: list[dict]) -> list[dict]:
    if not isinstance(skills, list):
        raise HTTPException(status_code=422, detail="skills must be an array")
    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in skills:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail="skills entries must be objects")
        skill_type = str(entry.get("type") or "custom")
        skill_id = entry.get("id") or entry.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise HTTPException(status_code=422, detail="skills entries require skill_id")

        requested_version = entry.get("version", "latest")
        version = "latest" if requested_version in (None, "", "latest") else str(requested_version)

        if skill_type == "anthropic":
            key = (skill_type, skill_id, version)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"type": "anthropic", "skill_id": skill_id, "version": version})
            continue

        if skill_type not in {"custom", "skill"}:
            raise HTTPException(status_code=422, detail='skills entries type must be "custom" or "anthropic"')

        skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
        if skill is None:
            raise HTTPException(status_code=422, detail=f"Skill not found: {skill_id}")

        if version == "latest":
            if not (skill.data or {}).get("latest_version"):
                raise HTTPException(status_code=422, detail=f"Skill has no latest version: {skill_id}")
        else:
            try:
                version_int = int(version)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="skill version must be an integer or latest") from exc
            skill_version = await res_q.get_resource_version(
                db,
                resource_type="skill_version",
                parent_id=skill_id,
                version=version_int,
            )
            if skill_version is None:
                raise HTTPException(status_code=422, detail=f"Skill version not found: {skill_id}@{version}")

        key = ("custom", skill_id, version)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"type": "custom", "skill_id": skill_id, "version": version})
    return normalized


def _merge_agent_update(active, agent, update: dict) -> dict:
    name = active.name
    model = active.model
    system = active.system
    description = active.description
    tools = active.tools
    mcp_servers = active.mcp_servers
    skills = active.skills
    multiagent = active.multiagent
    metadata = dict(active.metadata_)
    runtime = active.runtime

    if "name" in update:
        name = _normalize_agent_name(update["name"], field_name="name")
    if "model" in update:
        model = _normalize_agent_model(update["model"])
    if "system" in update:
        system = update["system"]
    if "description" in update:
        description = update["description"]
    if "tools" in update:
        tools = update["tools"] or []
    if "mcp_servers" in update:
        mcp_servers = update["mcp_servers"] or []
    if "skills" in update:
        skills = update["skills"] or []
    if "multiagent" in update:
        multiagent = update["multiagent"]
    if "metadata" in update:
        metadata = merge_metadata(metadata, update["metadata"])
    if "runtime" in update:
        runtime = update["runtime"] or {}

    return {
        "name": name,
        "model": model,
        "system": system,
        "description": description,
        "tools": tools,
        "mcp_servers": mcp_servers,
        "skills": skills,
        "multiagent": multiagent,
        "metadata": metadata,
        "runtime": runtime,
    }


def _normalize_agent_name(value: Any, *, field_name: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_agent_model(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        model_id = value.strip()
        if not model_id:
            raise HTTPException(status_code=422, detail="model id must be a non-empty string")
        return {"id": model_id}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="model must be a string or object")
    normalized = dict(value)
    model_id = normalized.get("id") or normalized.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise HTTPException(status_code=422, detail="model.id must be a non-empty string")
    normalized["id"] = model_id.strip()
    if "speed" in normalized and normalized["speed"] not in {None, "standard", "fast"}:
        raise HTTPException(status_code=422, detail="model.speed must be standard or fast")
    return normalized
