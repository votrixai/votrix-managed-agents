from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_contract import (
    normalize_agent_model,
    normalize_agent_skill_refs,
    normalize_agent_tools,
    validate_mcp_bindings,
)
from app.runtime.contracts import EffectiveAgentVersion


def effective_agent_version(
    version,
    status_details: dict[str, Any] | None,
) -> EffectiveAgentVersion:
    """Resolve the immutable Agent version plus Session-local overrides."""
    overlay: dict[str, Any] = {}
    if isinstance(status_details, dict) and isinstance(status_details.get("agent"), dict):
        overlay = status_details["agent"]

    model = overlay.get("model", version.model)
    tools = overlay.get("tools", version.tools)
    mcp_servers = overlay.get("mcp_servers", version.mcp_servers)
    skills = overlay.get("skills", version.skills)
    return EffectiveAgentVersion(
        id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        name=version.name,
        model=model if isinstance(model, dict) else version.model,
        system=overlay["system"] if "system" in overlay else version.system,
        description=version.description,
        tools=tools if isinstance(tools, list) else version.tools,
        mcp_servers=mcp_servers if isinstance(mcp_servers, list) else version.mcp_servers,
        skills=skills if isinstance(skills, list) else version.skills,
        multiagent=version.multiagent,
        metadata_=version.metadata_,
        runtime=version.runtime,
    )


async def resolve_session_agent_config(
    db: AsyncSession,
    version,
    overrides: Any | None,
    *,
    organization_id: str,
) -> dict[str, Any]:
    """Resolve and validate CMA create-time Agent overrides for one Session."""
    fields = overrides.model_fields_set if overrides is not None else set()

    if "model" in fields:
        if overrides is None or overrides.model is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "type": "error",
                    "error": {
                        "type": "agent_model_required",
                        "message": "A session always requires an agent model",
                    },
                },
            )
        model = normalize_agent_model(overrides.model)
    else:
        model = dict(version.model or {})

    system = overrides.system if overrides is not None and "system" in fields else version.system
    if system is not None and len(system) > 100_000:
        raise HTTPException(status_code=422, detail="agent.system must be at most 100000 characters")

    tools = list(version.tools or [])
    if overrides is not None and "tools" in fields:
        tools = normalize_agent_tools(overrides.tools)

    mcp_servers = list(version.mcp_servers or [])
    if overrides is not None and "mcp_servers" in fields:
        mcp_servers = list(overrides.mcp_servers)

    raw_skills = list(version.skills or [])
    if overrides is not None and "skills" in fields:
        raw_skills = list(overrides.skills)
    skills = await normalize_agent_skill_refs(
        db,
        raw_skills,
        organization_id=organization_id,
        pin_latest=True,
    )

    if overrides is not None and "tools" in fields and not tools and skills:
        raise HTTPException(
            status_code=400,
            detail="agent.tools cannot be cleared while the effective agent has skills",
        )
    validate_mcp_bindings(mcp_servers, tools)
    return {
        "model": model,
        "system": system,
        "tools": tools,
        "mcp_servers": mcp_servers,
        "skills": skills,
    }
