from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.network_security import create_restricted_http_client, validate_public_https_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse, ResponseT
    from langchain_core.messages import AIMessage


CLAUDE_TO_DEEP_TOOLS: dict[str, tuple[str, ...]] = {
    "bash": ("execute",),
    "read": ("ls", "read_file"),
    "write": ("write_file",),
    "edit": ("edit_file",),
    "glob": ("glob",),
    "grep": ("grep",),
    "web_fetch": ("web_fetch",),
    "web_search": ("web_search",),
}

DEEP_TO_CLAUDE_TOOL = {
    deep_name: public_name
    for public_name, deep_names in CLAUDE_TO_DEEP_TOOLS.items()
    for deep_name in deep_names
}


class ToolFilterMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide disabled harness tools after all Deep Agents middleware is assembled."""

    def __init__(self, *, excluded: set[str] | frozenset[str]) -> None:
        self._excluded = frozenset(excluded)

    def wrap_model_call(self, request, handler):
        if self._excluded:
            request = request.override(tools=[tool for tool in request.tools if _tool_name(tool) not in self._excluded])
        return handler(request)

    async def awrap_model_call(self, request, handler):
        if self._excluded:
            request = request.override(tools=[tool for tool in request.tools if _tool_name(tool) not in self._excluded])
        return await handler(request)


def effective_agent_tool_config(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve Claude toolset defaults and per-tool overrides."""
    resolved = {
        name: {"enabled": False, "permission_policy": {"type": "always_allow"}}
        for name in CLAUDE_TO_DEEP_TOOLS
    }
    for toolset in tools:
        if not isinstance(toolset, dict) or toolset.get("type") != "agent_toolset_20260401":
            continue
        default = dict(toolset.get("default_config") or {})
        default_enabled = bool(default.get("enabled", True))
        default_policy = _policy(default, "always_allow")
        for name in resolved:
            resolved[name] = {"enabled": default_enabled, "permission_policy": {"type": default_policy}}
        for entry in toolset.get("configs") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name not in resolved:
                continue
            resolved[name] = {
                "enabled": bool(entry.get("enabled", default_enabled)),
                "permission_policy": {"type": _policy(entry, default_policy)},
            }
    return resolved


def deep_tool_policy(
    tools: list[dict[str, Any]],
    *,
    supports_execute: bool,
    has_multiagent: bool,
) -> tuple[set[str], dict[str, Any], dict[str, dict[str, Any]]]:
    config = effective_agent_tool_config(tools)
    excluded: set[str] = set()
    interrupt_on: dict[str, Any] = {}
    for public_name, deep_names in CLAUDE_TO_DEEP_TOOLS.items():
        item = config[public_name]
        enabled = bool(item["enabled"])
        if public_name == "bash" and not supports_execute:
            enabled = False
        for deep_name in deep_names:
            if not enabled:
                excluded.add(deep_name)
            elif item["permission_policy"]["type"] == "always_ask":
                interrupt_on[deep_name] = {"allowed_decisions": ["approve", "reject"]}
    if not has_multiagent:
        excluded.add("task")
    return excluded, interrupt_on, config


def custom_tool(spec: dict[str, Any]) -> StructuredTool:
    name = str(spec["name"])

    async def _client_owned_tool(**kwargs: Any) -> str:
        # This callable is protected by HITL with a respond-only decision. It is
        # retained as a defensive fallback if middleware behavior changes.
        return json.dumps({"error": "client_tool_result_required", "tool": name, "input": kwargs})

    return StructuredTool.from_function(
        coroutine=_client_owned_tool,
        name=name,
        description=str(spec.get("description") or f"Custom tool {name}."),
        args_schema=dict(spec.get("input_schema") or {"type": "object", "properties": {}}),
    )


def web_fetch_tool() -> StructuredTool:
    async def web_fetch(url: str) -> str:
        """Fetch a public HTTP(S) URL and return bounded text content."""
        settings = get_settings()
        max_bytes = int(getattr(settings, "vma_web_fetch_max_bytes", 1_000_000))
        allow_private = bool(getattr(settings, "vma_web_allow_private_networks", False))
        current = url
        async with _outbound_http_client(allow_private=allow_private) as client:
            for _ in range(6):
                await _validate_public_url(current, allow_private=allow_private)
                async with client.stream(
                    "GET",
                    current,
                    headers={"user-agent": "Votrix-Managed-Agents/1.0"},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return "Fetch failed: redirect without location."
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    body, truncated = await _read_bounded_body(response, max_bytes=max_bytes)
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    if truncated:
                        text += "\n\n[Response truncated by VMA_WEB_FETCH_MAX_BYTES]"
                    return text
        return "Fetch failed: too many redirects."

    return StructuredTool.from_function(coroutine=web_fetch, name="web_fetch", description=web_fetch.__doc__ or "Fetch a URL.")


def web_search_tool() -> StructuredTool:
    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the web using the operator-configured JSON search endpoint."""
        endpoint = str(getattr(get_settings(), "vma_web_search_endpoint", "") or "").strip()
        if not endpoint:
            return "Web search is not configured. Set VMA_WEB_SEARCH_ENDPOINT to a SearXNG-compatible JSON endpoint."
        await _validate_public_url(
            endpoint,
            allow_private=bool(getattr(get_settings(), "vma_web_allow_private_networks", False)),
        )
        limit = max(1, min(int(max_results), 10))
        max_bytes = int(getattr(get_settings(), "vma_web_fetch_max_bytes", 1_000_000))
        allow_private = bool(getattr(get_settings(), "vma_web_allow_private_networks", False))
        async with _outbound_http_client(allow_private=allow_private) as client:
            async with client.stream(
                "GET",
                endpoint,
                params={"q": query, "format": "json"},
            ) as response:
                response.raise_for_status()
                body, truncated = await _read_bounded_body(response, max_bytes=max_bytes)
                if truncated:
                    raise ValueError("Web search response exceeds VMA_WEB_FETCH_MAX_BYTES")
                payload = json.loads(body)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return json.dumps(payload, ensure_ascii=False)[:20_000]
        compact = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
            }
            for item in results[:limit]
            if isinstance(item, dict)
        ]
        return json.dumps(compact, ensure_ascii=False)

    return StructuredTool.from_function(coroutine=web_search, name="web_search", description=web_search.__doc__ or "Search the web.")


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        value = tool.get("name")
    else:
        value = getattr(tool, "name", None)
    return value if isinstance(value, str) else None


def _policy(config: dict[str, Any], default: str) -> str:
    policy = config.get("permission_policy")
    if isinstance(policy, dict) and policy.get("type") in {"always_allow", "always_ask"}:
        return str(policy["type"])
    return default


async def _validate_public_url(value: str, *, allow_private: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute http and https URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain embedded credentials")
    if allow_private:
        return
    await validate_public_https_url(value)


def _outbound_http_client(*, allow_private: bool) -> httpx.AsyncClient:
    timeout = httpx.Timeout(30.0)
    if not allow_private:
        return create_restricted_http_client(timeout=timeout)
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


async def _read_bounded_body(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> tuple[bytes, bool]:
    limit = max(1, max_bytes)
    content = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = limit + 1 - len(content)
        if remaining <= 0:
            truncated = True
            break
        content.extend(chunk[:remaining])
        if len(content) > limit:
            truncated = True
            del content[limit:]
            break
    return bytes(content), truncated
