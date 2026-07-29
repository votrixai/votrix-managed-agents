"""What the agent's declared tools mean at runtime.

Nothing here ever removes a tool. Whatever `create_deep_agent()` installs is
exactly what the model gets; toolset config only decides which of those tools
must stop and ask the user before running.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

TOOLSET_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "agent_toolset_20260401": (
        "execute",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "write_todos",
        "task",
    ),
    "web_toolset_20260401": ("web_fetch", "web_search"),
}

AGENT_TOOLSET = "agent_toolset_20260401"
WEB_TOOLSET = "web_toolset_20260401"


def resolve_tool_interrupts(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Which of DeepAgents' always-on native tools need a decision first.

    Nothing is ever excluded: every native tool stays available regardless of
    what the agent's config says. Config only controls whether a given tool
    needs an approve/reject before it runs.
    """
    interrupt_on: dict[str, Any] = {}
    for toolset in tools:
        if not isinstance(toolset, dict):
            continue
        names = TOOLSET_TOOL_NAMES.get(toolset.get("type"))
        if names is None:
            continue
        default_policy = _policy(dict(toolset.get("default_config") or {}), "always_allow")
        resolved = dict.fromkeys(names, default_policy)
        for entry in toolset.get("configs") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name in names:
                resolved[name] = _policy(entry, default_policy)
        for name, policy in resolved.items():
            if policy == "always_ask":
                interrupt_on[name] = {"allowed_decisions": ["approve", "reject"]}
    return interrupt_on


def custom_tool(spec: dict[str, Any]) -> StructuredTool:
    """A tool the client runs, not us.

    The body never executes in practice — these are always paired with a
    respond-only interrupt, so the graph stops and waits for the client. It is
    here as a loud failure in case that ever stops being true.
    """
    name = str(spec["name"])

    async def _client_owned_tool(**kwargs: Any) -> str:
        return json.dumps({"error": "client_tool_result_required", "tool": name, "input": kwargs})

    return StructuredTool.from_function(
        coroutine=_client_owned_tool,
        name=name,
        description=str(spec.get("description") or f"Custom tool {name}."),
        args_schema=dict(spec.get("input_schema") or {"type": "object", "properties": {}}),
    )


def web_fetch_tool() -> StructuredTool:
    async def web_fetch(url: str) -> str:
        """Fetch a public HTTP(S) URL and return its text content."""
        raise NotImplementedError("web_fetch is not implemented yet")

    return StructuredTool.from_function(
        coroutine=web_fetch,
        name="web_fetch",
        description=web_fetch.__doc__ or "Fetch a URL.",
    )


def web_search_tool() -> StructuredTool:
    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the web and return the top results."""
        raise NotImplementedError("web_search is not implemented yet")

    return StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description=web_search.__doc__ or "Search the web.",
    )


def _policy(config: dict[str, Any], default: str) -> str:
    policy = config.get("permission_policy")
    if isinstance(policy, dict) and policy.get("type") in {"always_allow", "always_ask"}:
        return str(policy["type"])
    return default


__all__ = [
    "AGENT_TOOLSET",
    "TOOLSET_TOOL_NAMES",
    "WEB_TOOLSET",
    "custom_tool",
    "resolve_tool_interrupts",
    "web_fetch_tool",
    "web_search_tool",
]
