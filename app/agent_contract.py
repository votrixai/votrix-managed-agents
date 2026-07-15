import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import resources as res_q
from app.config import get_settings
from app.network_security import validate_public_https_url_syntax

MAX_AGENT_TOOLS = 128
MAX_CUSTOM_TOOL_NAME_CHARS = 128
MAX_CUSTOM_TOOL_DESCRIPTION_CHARS = 1024
CUSTOM_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PUBLIC_MODEL_FIELDS = {
    "id",
    "model",
    "speed",
    "provider",
    "provider_id",
    "vendor",
    "source",
}


def normalize_agent_model(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        model_id = value.strip()
        if not model_id:
            raise HTTPException(status_code=422, detail="model id must be a non-empty string")
        return {"id": model_id}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="model must be a string or object")
    normalized = dict(value)
    unsupported = set(normalized) - PUBLIC_MODEL_FIELDS
    if unsupported:
        fields = ", ".join(sorted(unsupported))
        raise HTTPException(status_code=422, detail=f"Unsupported public model fields: {fields}")
    raw_id = normalized.get("id")
    raw_model = normalized.get("model")
    if raw_id is not None and (not isinstance(raw_id, str) or not raw_id.strip()):
        raise HTTPException(status_code=422, detail="model.id must be a non-empty string")
    if raw_model is not None and (not isinstance(raw_model, str) or not raw_model.strip()):
        raise HTTPException(status_code=422, detail="model.model must be a non-empty string")
    if isinstance(raw_id, str) and isinstance(raw_model, str) and raw_id.strip() != raw_model.strip():
        raise HTTPException(status_code=422, detail="model.id and model.model must match when both are provided")
    model_id = raw_id or raw_model
    if not isinstance(model_id, str) or not model_id.strip():
        raise HTTPException(status_code=422, detail="model.id must be a non-empty string")
    normalized.pop("model", None)
    normalized["id"] = model_id.strip()
    if "speed" in normalized and normalized["speed"] not in {None, "standard", "fast"}:
        raise HTTPException(status_code=422, detail="model.speed must be standard or fast")
    return normalized


async def normalize_agent_skill_refs(
    db: AsyncSession,
    skills: list[dict],
    *,
    workspace_id: str | None = None,
    pin_latest: bool = False,
) -> list[dict]:
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
            if get_settings().vma_public_ga_only:
                raise HTTPException(
                    status_code=422,
                    detail="Anthropic system skills are not available in the Votrix public beta",
                )
            key = (skill_type, skill_id, version)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"type": "anthropic", "skill_id": skill_id, "version": version})
            continue

        if skill_type not in {"custom", "skill"}:
            raise HTTPException(status_code=422, detail='skills entries type must be "custom" or "anthropic"')

        skill = await res_q.get_resource(
            db,
            resource_id=skill_id,
            resource_type="skill",
            workspace_id=workspace_id,
        )
        if skill is None:
            raise HTTPException(status_code=422, detail=f"Skill not found: {skill_id}")

        if version == "latest":
            latest_version = (skill.data or {}).get("latest_version")
            if not latest_version:
                raise HTTPException(status_code=422, detail=f"Skill has no latest version: {skill_id}")
            if pin_latest:
                version = str(latest_version)
                skill_version = await res_q.get_resource_version(
                    db,
                    resource_type="skill_version",
                    parent_id=skill_id,
                    version=int(latest_version),
                    workspace_id=workspace_id,
                )
                if skill_version is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Skill version not found: {skill_id}@{latest_version}",
                    )
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
                workspace_id=workspace_id,
            )
            if skill_version is None:
                raise HTTPException(status_code=422, detail=f"Skill version not found: {skill_id}@{version}")

        key = ("custom", skill_id, version)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"type": "custom", "skill_id": skill_id, "version": version})
    return normalized


def normalize_agent_tools(tools: list[dict]) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=422, detail="tools must be an array")

    normalized = [_normalize_agent_tool(tool) for tool in tools]
    _validate_agent_tool_limits(normalized)
    return normalized


def _validate_agent_tool_limits(tools: list[dict]) -> None:
    count = sum(_agent_tool_count(tool) for tool in tools)
    if count > MAX_AGENT_TOOLS:
        raise HTTPException(status_code=422, detail="tools supports at most 128 tools across all toolsets")

    custom_names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "custom":
            continue
        name = str(tool.get("name") or "")
        if name in custom_names:
            raise HTTPException(status_code=422, detail=f"Duplicate custom tool name: {name}")
        custom_names.add(name)


def _agent_tool_count(tool: dict) -> int:
    if not isinstance(tool, dict):
        return 1
    if tool.get("type") == "custom":
        return 1
    configs = tool.get("configs")
    if isinstance(configs, list) and configs:
        return len(configs)
    return 1


def _normalize_agent_tool(tool: dict) -> dict:
    if not isinstance(tool, dict):
        raise HTTPException(status_code=422, detail="tools entries must be objects")

    tool_type = tool.get("type")
    if tool_type == "agent_toolset_20260401":
        return _normalize_toolset(tool, default_policy_type="always_allow")
    if tool_type == "mcp_toolset":
        normalized = _normalize_toolset(tool, default_policy_type="always_ask")
        server_name = normalized.get("mcp_server_name")
        if not isinstance(server_name, str) or not server_name:
            raise HTTPException(status_code=422, detail="mcp_toolset entries require mcp_server_name")
        return normalized
    if tool_type == "custom":
        return _normalize_custom_tool(tool)

    return dict(tool)


def _normalize_toolset(tool: dict, *, default_policy_type: str) -> dict:
    normalized = dict(tool)
    default_config = _normalize_tool_config(
        normalized.get("default_config") if isinstance(normalized.get("default_config"), dict) else {},
        fallback_enabled=True,
        fallback_policy={"type": default_policy_type},
    )
    normalized["default_config"] = default_config
    normalized["configs"] = [
        _normalize_tool_config(
            config,
            fallback_enabled=default_config["enabled"],
            fallback_policy=default_config["permission_policy"],
            require_name=True,
        )
        for config in normalized.get("configs") or []
    ]
    return normalized


def _normalize_tool_config(
    config: dict,
    *,
    fallback_enabled: bool,
    fallback_policy: dict,
    require_name: bool = False,
) -> dict:
    if not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="tool configs entries must be objects")

    normalized = dict(config)
    if require_name and not normalized.get("name"):
        raise HTTPException(status_code=422, detail="tool configs entries require name")
    if "enabled" not in normalized or normalized["enabled"] is None:
        normalized["enabled"] = fallback_enabled
    policy = normalized.get("permission_policy")
    if not isinstance(policy, dict) or not policy.get("type"):
        normalized["permission_policy"] = dict(fallback_policy)
    return normalized


def _normalize_custom_tool(tool: dict) -> dict:
    normalized = dict(tool)
    name = normalized.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="custom tools require name")
    if len(name) > MAX_CUSTOM_TOOL_NAME_CHARS or not CUSTOM_TOOL_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="custom tool name must be 1-128 characters using letters, digits, underscores, or hyphens",
        )
    description = normalized.get("description")
    if description is None:
        normalized["description"] = f"Custom tool {name}."
    elif not isinstance(description, str) or not description:
        raise HTTPException(status_code=422, detail="custom tool description must be a non-empty string")
    elif len(description) > MAX_CUSTOM_TOOL_DESCRIPTION_CHARS:
        raise HTTPException(status_code=422, detail="custom tool description must be at most 1024 characters")
    input_schema = normalized.get("input_schema")
    if not isinstance(input_schema, dict):
        normalized["input_schema"] = {"type": "object", "properties": {}}
    elif input_schema.get("type") != "object":
        raise HTTPException(status_code=422, detail='custom tool input_schema.type must be "object"')
    return normalized


def validate_mcp_bindings(mcp_servers: list[dict], tools: list[dict]) -> None:
    if len(mcp_servers) > 20:
        raise HTTPException(status_code=422, detail="mcp_servers supports at most 20 servers")

    server_names: set[str] = set()
    for server in mcp_servers:
        if not isinstance(server, dict):
            raise HTTPException(status_code=422, detail="mcp_servers entries must be objects")
        if server.get("type") != "url":
            raise HTTPException(status_code=422, detail='mcp_servers entries must have type "url"')
        name = server.get("name")
        url = server.get("url")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="mcp_servers entries require name")
        if not isinstance(url, str) or not url:
            raise HTTPException(status_code=422, detail="mcp_servers entries require url")
        try:
            validate_public_https_url_syntax(url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid MCP server URL: {exc}") from exc
        if name in server_names:
            raise HTTPException(status_code=422, detail=f"Duplicate MCP server name: {name}")
        server_names.add(name)

    tool_refs: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp_toolset":
            continue
        server_name = tool.get("mcp_server_name")
        if not isinstance(server_name, str) or not server_name:
            raise HTTPException(status_code=422, detail="mcp_toolset entries require mcp_server_name")
        if server_name not in server_names:
            raise HTTPException(status_code=422, detail=f"mcp_toolset references undeclared MCP server: {server_name}")
        tool_refs.add(server_name)

    unreferenced = server_names - tool_refs
    if unreferenced:
        names = ", ".join(sorted(unreferenced))
        raise HTTPException(status_code=422, detail=f"MCP servers must be referenced by mcp_toolset: {names}")
