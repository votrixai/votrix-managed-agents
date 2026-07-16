"""Deep Agents execution adapter for the VMA control plane.

The adapter deliberately keeps tenancy, secrets, event persistence, and sandbox
lifecycle outside Deep Agents. A graph is compiled for one immutable agent revision
and one run, then streamed into the Claude Managed Agents-shaped event protocol.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from app.config import get_settings
from app.ids import new_id
from app.organization import resolve_organization_id
from app.runtime.checkpoints import checkpoint_saver
from app.runtime.contracts import (
    EffectiveAgentVersion,
    RuntimeEventEmitter,
    RuntimePreviewEmitter,
    RuntimeResult,
)
from app.runtime.deepagent_tools import (
    DEEP_TO_CLAUDE_TOOL,
    ToolFilterMiddleware,
    custom_tool,
    deep_tool_policy,
    effective_agent_tool_config,
    web_fetch_tool,
    web_search_tool,
)
from app.runtime.model_inputs import ModelInputValidationError, adapt_user_message_content
from app.runtime.providers import build_chat_model, resolve_runtime_provider
from app.runtime.sandbox_inputs import sandbox_input_bundle
from app.runtime.sandbox import open_backend
from app.runtime.sandbox_outputs import (
    MAX_DISCOVERED_OUTPUT_FILES,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_TOTAL_BYTES,
    SANDBOX_OUTPUT_ROOT,
)


class DeepAgentsRuntimeError(RuntimeError):
    """Raised when a revision cannot safely execute through Deep Agents."""


@dataclass(frozen=True)
class TenantRunContext:
    organization_id: str
    session_id: str
    agent_id: str
    agent_version_id: str


@dataclass
class _EmittedToolCall:
    event_id: str
    event_type: str
    internal_id: str
    internal_name: str
    public_name: str
    args: dict[str, Any]


async def execute_deep_agent(
    version: EffectiveAgentVersion,
    history: list[Any],
    environment_config: dict[str, Any] | None,
    *,
    runtime_context: dict[str, Any] | None = None,
    organization_id: str | None = None,
    session_id: str | None = None,
    emit_event: RuntimeEventEmitter | None = None,
    emit_preview: RuntimePreviewEmitter | None = None,
) -> RuntimeResult:
    """Execute one durable session turn with a run-scoped Deep Agents graph."""
    runtime_context = dict(runtime_context or {})
    context_organization_id = runtime_context.get("organization_id")
    organization_id = resolve_organization_id(
        str(context_organization_id)
        if organization_id is None and context_organization_id is not None
        else organization_id
    )
    session_id = session_id or str(runtime_context.get("session_id") or "")
    thread_id = str(runtime_context.get("checkpoint_thread_id") or "")
    if not session_id or not thread_id:
        raise DeepAgentsRuntimeError("Deep Agents execution requires tenant, session, and checkpoint thread ids")

    secrets = runtime_context.get("provider_secrets")
    provider = resolve_runtime_provider(
        version.model,
        runtime=version.runtime,
        secrets=secrets if isinstance(secrets, dict) else None,
    )
    if not provider.capabilities.tool_calls:
        raise DeepAgentsRuntimeError(
            f"Model {provider.model_id} cannot run the Deep Agents harness because it does not support tool calls"
        )
    model = build_chat_model(provider)

    previous_state = runtime_context.get("previous_run_state")
    previous_state = dict(previous_state) if isinstance(previous_state, dict) else {}
    raw_session_files = runtime_context.get("session_files")
    session_files = (
        [item for item in raw_session_files if isinstance(item, dict)]
        if isinstance(raw_session_files, list)
        else []
    )
    graph_input, processed_seq = _graph_input(
        history,
        previous_state,
        session_files=session_files,
        multimodal_input=provider.capabilities.multimodal_input,
    )
    if graph_input is None:
        return RuntimeResult(
            events_persisted=emit_event is not None,
            run_state=previous_state or {
                "backend": "deepagents",
                "agent_version_id": version.id,
                "provider": provider.provider,
                "model": provider.model_id,
            },
        )

    tool_events: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    sandbox_outputs: list[Any] = []
    persisted_inputs = _persisted_sandbox_inputs(runtime_context)
    # E2B Sessions were fully materialized and sealed at Session creation.
    # Rebuilding the bundle here would re-download and unzip immutable inputs
    # only to rediscover the identity already stored in the sandbox binding.
    input_bundle = None if persisted_inputs is not None else sandbox_input_bundle(runtime_context)
    async with AsyncExitStack() as stack:
        backend_handle = await stack.enter_async_context(
            open_backend(
                organization_id=organization_id,
                session_id=session_id,
                environment_config=environment_config,
                input_bundle=input_bundle,
            )
        )
        saver = await stack.enter_async_context(checkpoint_saver())

        mcp_tools, mcp_tool_names, mcp_interrupts = await _load_mcp_tools(
            version,
            runtime_context,
            warnings,
        )
        for warning in warnings:
            if warning.get("type") == "mcp_connection_error":
                await _emit(
                    {
                        "type": "session.error",
                        "error_type": "mcp_connection_error",
                        "message": warning.get("message") or "MCP connection failed",
                        "mcp_server_name": warning.get("server_name"),
                        "source": "deepagents",
                    },
                    emit_event,
                    tool_events,
                )
        tools, custom_names, custom_specs = _materialize_tools(version, mcp_tools)
        excluded, interrupt_on, _tool_config = deep_tool_policy(
            version.tools,
            supports_execute=backend_handle.plan.supports_execute,
            has_multiagent=bool(version.multiagent),
        )
        interrupt_on.update(mcp_interrupts)
        for name in custom_names:
            interrupt_on[name] = {"allowed_decisions": ["respond"]}

        if backend_handle.plan.backend == "e2b" and persisted_inputs is not None:
            virtual_files: list[tuple[str, bytes]] = []
            state_files: dict[str, dict[str, Any]] = {}
            skill_sources = list(persisted_inputs["skill_sources"])
            memory_sources = list(persisted_inputs["memory_sources"])
            read_only_paths: list[str] = []
        else:
            virtual_files, state_files, skill_sources, memory_sources, read_only_paths = _virtual_files(
                runtime_context
            )
        if backend_handle.plan.backend not in {"langgraph_state", "e2b"} and virtual_files:
            await _upload_virtual_files(backend_handle.backend, virtual_files, warnings)
            state_files = {}
        elif backend_handle.plan.backend == "e2b":
            state_files = {}

        permissions = []
        if not backend_handle.plan.supports_execute and read_only_paths:
            from deepagents import FilesystemPermission

            permissions = [FilesystemPermission(operations=["write"], paths=read_only_paths, mode="deny")]

        subagents = _materialize_subagents(runtime_context, secrets if isinstance(secrets, dict) else {})

        from deepagents import create_deep_agent

        graph = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=version.system or "You are a helpful managed agent.",
            middleware=[ToolFilterMiddleware(excluded=excluded)],
            subagents=subagents or None,
            skills=skill_sources or None,
            memory=memory_sources or None,
            permissions=permissions or None,
            backend=backend_handle.backend,
            interrupt_on=interrupt_on or None,
            context_schema=TenantRunContext,
            checkpointer=saver,
            name=_graph_name(version.name, version.agent_id),
        )

        if isinstance(graph_input, dict) and state_files:
            graph_input = {**graph_input, "files": state_files}

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(10, int(get_settings().vma_max_graph_steps)),
        }
        context = TenantRunContext(
            organization_id=organization_id,
            session_id=session_id,
            agent_id=version.agent_id,
            agent_version_id=version.id,
        )
        timeout_seconds = max(1, int(get_settings().vma_run_timeout_seconds))
        async with asyncio.timeout(timeout_seconds):
            streamed = await _stream_graph(
                graph,
                graph_input,
                config=config,
                context=context,
                emit_event=emit_event,
                emit_preview=emit_preview,
                tool_events=tool_events,
                custom_names=custom_names,
                custom_specs=custom_specs,
                mcp_tool_names=mcp_tool_names,
                interrupt_on=interrupt_on,
            )
        if backend_handle.plan.backend == "e2b":
            if backend_handle.connection is None:
                raise DeepAgentsRuntimeError("E2B output discovery requires the live sandbox connection")
            from app.runtime.sandbox_lifecycle import build_e2b_provider

            sandbox_outputs = await build_e2b_provider().discover_outputs(
                backend_handle.connection,
                root=SANDBOX_OUTPUT_ROOT,
                max_files=MAX_DISCOVERED_OUTPUT_FILES,
                max_file_bytes=MAX_OUTPUT_FILE_BYTES,
                max_total_bytes=MAX_OUTPUT_TOTAL_BYTES,
            )

    pending_actions = streamed["pending_actions"]
    run_state = {
        "backend": "deepagents",
        "agent_version_id": version.id,
        "provider": provider.provider,
        "model": provider.model_id,
        "last_input_event_seq": processed_seq,
        "pending_actions": pending_actions,
        "warnings": warnings,
    }
    return RuntimeResult(
        final_text=streamed["final_text"],
        tool_events=tool_events,
        requires_action=bool(pending_actions),
        blocking_event_ids=[item["event_id"] for item in pending_actions],
        events_persisted=emit_event is not None,
        run_state=run_state,
        sandbox_state={**backend_handle.plan.summary, "runtime_backend": "deepagents"},
        sandbox_outputs=sandbox_outputs,
        usage=streamed["usage"],
    )


async def _stream_graph(
    graph,
    graph_input,
    *,
    config: dict[str, Any],
    context: TenantRunContext,
    emit_event: RuntimeEventEmitter | None,
    emit_preview: RuntimePreviewEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    custom_specs: dict[str, dict[str, Any]],
    mcp_tool_names: set[str],
    interrupt_on: dict[str, Any],
) -> dict[str, Any]:
    text_parts: list[str] = []
    message_event_id: str | None = None
    tool_accumulator: dict[tuple[tuple[str, ...], str, int], dict[str, Any]] = {}
    emitted_calls: dict[str, _EmittedToolCall] = {}
    pending_interrupts: list[Any] = []
    usage: dict[str, Any] = defaultdict(int)

    async for item in graph.astream(
        graph_input,
        config=config,
        context=context,
        stream_mode=["messages", "updates"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        namespace, mode, data = item
        namespace_key = tuple(str(part) for part in namespace) if isinstance(namespace, tuple) else ()
        if mode == "updates":
            if isinstance(data, dict) and data.get("__interrupt__"):
                pending_interrupts.extend(data["__interrupt__"])
            continue
        if mode != "messages" or not isinstance(data, tuple) or len(data) != 2:
            continue
        message, _metadata = data
        _merge_usage(usage, getattr(message, "usage_metadata", None))

        completed_calls = _completed_tool_calls(message, namespace_key, tool_accumulator)
        for call in completed_calls:
            internal_id = call["id"]
            if internal_id in emitted_calls:
                continue
            emitted = await _emit_tool_use(
                call,
                emit_event=emit_event,
                tool_events=tool_events,
                custom_names=custom_names,
                custom_specs=custom_specs,
                mcp_tool_names=mcp_tool_names,
                requires_confirmation=call["name"] in interrupt_on,
            )
            emitted_calls[internal_id] = emitted

        if isinstance(message, ToolMessage):
            if namespace_key:
                continue
            await _emit_tool_result(
                message,
                emitted_calls,
                emit_event=emit_event,
                tool_events=tool_events,
                custom_names=custom_names,
                mcp_tool_names=mcp_tool_names,
            )
            continue

        if namespace_key or not isinstance(message, (AIMessage, AIMessageChunk)):
            continue
        text = _message_text(message)
        if not text:
            continue
        if message_event_id is None:
            message_event_id = new_id("evt")
            if emit_preview is not None:
                await emit_preview(
                    {"type": "event_start", "event": {"type": "agent.message", "id": message_event_id}}
                )
        text_parts.append(text)
        if emit_preview is not None:
            await emit_preview(
                {
                    "type": "event_delta",
                    "event_id": message_event_id,
                    "delta": {
                        "type": "content_delta",
                        "index": 0,
                        "content": {"type": "text", "text": text},
                    },
                }
            )

    pending_actions = await _persist_interrupt_actions(
        pending_interrupts,
        emitted_calls,
        emit_event=emit_event,
        tool_events=tool_events,
        custom_names=custom_names,
        custom_specs=custom_specs,
        mcp_tool_names=mcp_tool_names,
    )
    final_text = "".join(text_parts)
    if final_text:
        payload = {
            "type": "agent.message",
            "content": [{"type": "text", "text": final_text}],
            "source": "deepagents",
            "_event_id": message_event_id or new_id("evt"),
        }
        await _emit(payload, emit_event, tool_events)
    return {
        "final_text": final_text,
        "pending_actions": pending_actions,
        "usage": dict(usage),
    }


async def _emit_tool_use(
    call: dict[str, Any],
    *,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    custom_specs: dict[str, dict[str, Any]],
    mcp_tool_names: set[str],
    requires_confirmation: bool,
) -> _EmittedToolCall:
    internal_name = call["name"]
    public_name = DEEP_TO_CLAUDE_TOOL.get(internal_name, internal_name)
    if internal_name in custom_names:
        event_type = "agent.custom_tool_use"
    elif internal_name in mcp_tool_names:
        event_type = "agent.mcp_tool_use"
    else:
        event_type = "agent.tool_use"
    payload: dict[str, Any] = {
        "type": event_type,
        "name": public_name,
        "input": call["args"],
        "tool_use_id": call["id"],
        "source": "deepagents",
        "_event_id": new_id("evt"),
    }
    if internal_name in custom_specs:
        payload["tool"] = custom_specs[internal_name]
    if requires_confirmation:
        payload["requires_confirmation"] = True
    event_id = await _emit(payload, emit_event, tool_events)
    return _EmittedToolCall(
        event_id=event_id,
        event_type=event_type,
        internal_id=call["id"],
        internal_name=internal_name,
        public_name=public_name,
        args=call["args"],
    )


async def _emit_tool_result(
    message: ToolMessage,
    emitted_calls: dict[str, _EmittedToolCall],
    *,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    mcp_tool_names: set[str],
) -> None:
    internal_id = str(message.tool_call_id or "")
    emitted = emitted_calls.get(internal_id)
    name = str(getattr(message, "name", "") or (emitted.internal_name if emitted else "tool"))
    if name in custom_names or name in {"write_todos", "task"}:
        return
    event_type = "agent.mcp_tool_result" if name in mcp_tool_names else "agent.tool_result"
    await _emit(
        {
            "type": event_type,
            "name": DEEP_TO_CLAUDE_TOOL.get(name, name),
            "tool_use_id": internal_id or None,
            "content": [{"type": "text", "text": _content_text(message.content)}],
            "source": "deepagents",
        },
        emit_event,
        tool_events,
    )


async def _persist_interrupt_actions(
    interrupts: list[Any],
    emitted_calls: dict[str, _EmittedToolCall],
    *,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    custom_specs: dict[str, dict[str, Any]],
    mcp_tool_names: set[str],
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    used_calls: set[str] = set()
    for interrupt in interrupts:
        value = getattr(interrupt, "value", None)
        interrupt_id = str(getattr(interrupt, "id", "") or "")
        if not isinstance(value, dict):
            raise DeepAgentsRuntimeError("Free-form LangGraph interrupts are not supported by the VMA API")
        actions = value.get("action_requests") or []
        reviews = value.get("review_configs") or []
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            name = str(action.get("name") or "tool")
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            emitted = _match_emitted_call(emitted_calls, used_calls, name, args)
            if emitted is None:
                emitted = await _emit_tool_use(
                    {"id": new_id("tool"), "name": name, "args": args},
                    emit_event=emit_event,
                    tool_events=tool_events,
                    custom_names=custom_names,
                    custom_specs=custom_specs,
                    mcp_tool_names=mcp_tool_names,
                    requires_confirmation=True,
                )
            used_calls.add(emitted.internal_id)
            allowed = []
            if index < len(reviews) and isinstance(reviews[index], dict):
                allowed = list(reviews[index].get("allowed_decisions") or [])
            pending.append(
                {
                    "event_id": emitted.event_id,
                    "interrupt_id": interrupt_id,
                    "action_index": index,
                    "tool_call_id": emitted.internal_id,
                    "name": name,
                    "kind": "custom" if name in custom_names else "confirmation",
                    "allowed_decisions": allowed,
                }
            )
    return pending


def _match_emitted_call(
    emitted_calls: dict[str, _EmittedToolCall],
    used: set[str],
    name: str,
    args: dict[str, Any],
) -> _EmittedToolCall | None:
    for call in emitted_calls.values():
        if call.internal_id not in used and call.internal_name == name and call.args == args:
            return call
    for call in emitted_calls.values():
        if call.internal_id not in used and call.internal_name == name:
            return call
    return None


async def _emit(
    payload: dict[str, Any],
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
) -> str:
    preferred = str(payload.get("_event_id") or new_id("evt"))
    payload["_event_id"] = preferred
    if emit_event is not None:
        return await emit_event(payload)
    tool_events.append(dict(payload))
    return preferred


def _materialize_tools(
    version: EffectiveAgentVersion,
    mcp_tools: list[Any],
) -> tuple[list[Any], set[str], dict[str, dict[str, Any]]]:
    tools = list(mcp_tools)
    custom_names: set[str] = set()
    custom_specs: dict[str, dict[str, Any]] = {}
    for spec in version.tools or []:
        if not isinstance(spec, dict) or spec.get("type") != "custom":
            continue
        tool = custom_tool(spec)
        tools.append(tool)
        custom_names.add(tool.name)
        custom_specs[tool.name] = {key: value for key, value in spec.items() if key not in {"authorization", "headers"}}
    config = effective_agent_tool_config(version.tools)
    if config["web_fetch"]["enabled"]:
        tools.append(web_fetch_tool())
    if config["web_search"]["enabled"]:
        tools.append(web_search_tool())
    return tools, custom_names, custom_specs


async def _load_mcp_tools(
    version: EffectiveAgentVersion,
    runtime_context: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> tuple[list[Any], set[str], dict[str, Any]]:
    toolsets = [
        item for item in version.tools or [] if isinstance(item, dict) and item.get("type") == "mcp_toolset"
    ]
    if not toolsets:
        return [], set(), {}
    selected = {str(item.get("mcp_server_name") or "") for item in toolsets}
    auth_entries = (runtime_context.get("mcp_auth") or {}).get("servers") or []
    auth_by_url = {
        str(item.get("mcp_server_url") or "").rstrip("/"): item
        for item in auth_entries
        if isinstance(item, dict)
    }
    all_tools: list[Any] = []
    names: set[str] = set()
    interrupts: dict[str, Any] = {}
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from app.network_security import create_restricted_http_client, validate_public_https_url

    for server in version.mcp_servers or []:
        if not isinstance(server, dict):
            continue
        server_name = str(server.get("name") or "")
        if selected and server_name not in selected:
            continue
        url = str(server.get("url") or "")
        if not server_name or not url:
            continue
        try:
            await validate_public_https_url(url)
        except ValueError as exc:
            warnings.append(
                {
                    "type": "mcp_connection_blocked",
                    "server_name": server_name,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                }
            )
            continue
        auth = auth_by_url.get(url.rstrip("/"), {})
        connection: dict[str, Any] = {
            "transport": "streamable_http",
            "url": url,
            "httpx_client_factory": create_restricted_http_client,
        }
        if isinstance(auth.get("headers"), dict):
            connection["headers"] = dict(auth["headers"])
        client = MultiServerMCPClient({server_name: connection})
        try:
            async with asyncio.timeout(30):
                server_tools = await client.get_tools(server_name=server_name)
        except Exception as exc:
            warnings.append(
                {
                    "type": "mcp_connection_error",
                    "server_name": server_name,
                    "error_type": exc.__class__.__name__,
                    "message": "MCP server connection failed",
                }
            )
            continue
        all_tools.extend(server_tools)
        names.update(tool.name for tool in server_tools)
        policy = _mcp_policy(toolsets, server_name)
        if policy == "always_ask":
            for tool in server_tools:
                interrupts[tool.name] = {"allowed_decisions": ["approve", "reject"]}
    return all_tools, names, interrupts


def _mcp_policy(toolsets: list[dict[str, Any]], server_name: str) -> str:
    for toolset in toolsets:
        if str(toolset.get("mcp_server_name") or "") != server_name:
            continue
        default = toolset.get("default_config") or {}
        policy = default.get("permission_policy") if isinstance(default, dict) else None
        if isinstance(policy, dict) and policy.get("type") == "always_allow":
            return "always_allow"
    return "always_ask"


def _materialize_subagents(runtime_context: dict[str, Any], secrets: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in runtime_context.get("subagents") or []:
        if not isinstance(spec, dict):
            continue
        base_name = _graph_name(str(spec.get("name") or "subagent"), str(spec.get("agent_id") or "agent"))
        name = base_name
        suffix = 2
        while name in seen:
            name = f"{base_name}-{suffix}"
            suffix += 1
        seen.add(name)
        try:
            provider = resolve_runtime_provider(
                dict(spec.get("model") or {}),
                secrets=secrets,
            )
            if not provider.capabilities.tool_calls:
                continue
            model = build_chat_model(provider)
        except Exception as exc:
            raise DeepAgentsRuntimeError(
                f"Subagent {name} model could not be configured from the Session Vault credential"
            ) from exc
        custom_tools = [
            custom_tool(tool)
            for tool in spec.get("tools") or []
            if isinstance(tool, dict) and tool.get("type") == "custom"
        ]
        entry: dict[str, Any] = {
            "name": name,
            "description": str(spec.get("description") or f"Managed subagent {name}."),
            "system_prompt": str(spec.get("system_prompt") or "You are a helpful managed subagent."),
            "model": model,
        }
        if custom_tools:
            entry["tools"] = custom_tools
            entry["interrupt_on"] = {
                tool.name: {"allowed_decisions": ["respond"]}
                for tool in custom_tools
            }
        result.append(entry)
    return result


def _virtual_files(
    runtime_context: dict[str, Any],
) -> tuple[list[tuple[str, bytes]], dict[str, dict[str, Any]], list[str], list[str], list[str]]:
    bundle = sandbox_input_bundle(runtime_context)
    read_only = [item.path for item in bundle.immutable_files]
    for root in bundle.skill_sources:
        read_only.extend([root.rstrip("/"), root.rstrip("/") + "/**"])
    immutable_paths = {item.path for item in bundle.immutable_files}
    for source in bundle.memory_sources:
        if source in immutable_paths:
            root = str(PurePosixPath(source).parent)
            read_only.extend([root, root.rstrip("/") + "/**"])
    return (
        bundle.upload_pairs(),
        bundle.state_files(),
        list(bundle.skill_sources),
        list(bundle.memory_sources),
        read_only,
    )


def _persisted_sandbox_inputs(runtime_context: dict[str, Any]) -> dict[str, tuple[str, ...]] | None:
    """Return validated path metadata for an already sealed E2B filesystem."""

    raw = runtime_context.get("persisted_sandbox_inputs")
    if not isinstance(raw, dict) or raw.get("provider") != "e2b":
        return None

    normalized: dict[str, tuple[str, ...]] = {}
    for name in ("skill_sources", "memory_sources", "mutable_roots"):
        value = raw.get(name)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise DeepAgentsRuntimeError(f"Persisted E2B {name} metadata is invalid")
        normalized[name] = tuple(value)
    return normalized


async def _upload_virtual_files(backend, files: list[tuple[str, bytes]], warnings: list[dict[str, Any]]) -> None:
    upload = getattr(backend, "aupload_files", None)
    if upload is None:
        warnings.append({"type": "sandbox_upload_unsupported", "file_count": len(files)})
        return
    responses = await upload(files)
    for response in responses or []:
        error = response.get("error") if isinstance(response, dict) else getattr(response, "error", None)
        if error:
            warnings.append({"type": "sandbox_upload_error", "message": str(error)})


def _graph_input(
    history: list[Any],
    previous_state: dict[str, Any],
    *,
    session_files: list[dict[str, Any]] | None = None,
    multimodal_input: bool = False,
) -> tuple[dict[str, Any] | Command | None, int]:
    pending = previous_state.get("pending_actions")
    if isinstance(pending, list) and pending:
        command, seq = _resume_command(history, pending)
        if command is not None:
            return command, seq

    last_seq = int(previous_state.get("last_input_event_seq") or 0)
    candidate = None
    for event in history:
        if event.seq > last_seq and event.type in {"user.message", "user.define_outcome"}:
            candidate = event
    if candidate is None:
        return None, last_seq
    content: Any
    if candidate.type == "user.define_outcome":
        objective = candidate.payload.get("description") or candidate.payload.get("objective") or "Complete the outcome."
        rubric = candidate.payload.get("rubric")
        content = f"{objective}\nRubric: {rubric}" if rubric else str(objective)
    else:
        content = candidate.payload.get("content") or candidate.payload.get("text") or ""
    contexts = [
        _content_text(event.payload.get("content"))
        for event in history
        if event.seq > candidate.seq and event.type == "system.message"
    ]
    contexts = [item for item in contexts if item]
    if contexts:
        suffix = "\n\n<system_context>\n" + "\n".join(contexts) + "\n</system_context>"
        if isinstance(content, str):
            content += suffix
        elif isinstance(content, list):
            content = [*content, {"type": "text", "text": suffix}]
    try:
        content = adapt_user_message_content(
            content,
            session_files=session_files or [],
            multimodal_input=multimodal_input,
        )
    except ModelInputValidationError as exc:
        raise DeepAgentsRuntimeError(str(exc)) from exc
    return {"messages": [{"role": "user", "content": content}]}, candidate.seq


def _resume_command(history: list[Any], pending: list[dict[str, Any]]) -> tuple[Command | None, int]:
    by_reference: dict[str, Any] = {}
    for event in history:
        if event.type == "user.custom_tool_result":
            ref = event.payload.get("custom_tool_use_id")
        elif event.type in {"user.tool_confirmation", "user.tool_result"}:
            ref = event.payload.get("tool_use_id")
        else:
            continue
        if ref:
            by_reference[str(ref)] = event
    if any(str(item.get("event_id")) not in by_reference for item in pending):
        return None, 0

    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    max_seq = 0
    for item in pending:
        event = by_reference[str(item["event_id"])]
        max_seq = max(max_seq, int(event.seq))
        if event.type in {"user.custom_tool_result", "user.tool_result"}:
            decision = {"type": "respond", "message": _content_text(event.payload.get("content"))}
        elif event.payload.get("result") == "allow":
            decision = {"type": "approve"}
        else:
            decision = {
                "type": "reject",
                "message": str(event.payload.get("deny_message") or "Denied by the caller"),
            }
        grouped[str(item.get("interrupt_id") or "")].append((int(item.get("action_index") or 0), decision))
    ordered = {key: [value for _, value in sorted(items)] for key, items in grouped.items()}
    if len(ordered) == 1:
        decisions = next(iter(ordered.values()))
        return Command(resume={"decisions": decisions}), max_seq
    return Command(resume={key: {"decisions": decisions} for key, decisions in ordered.items()}), max_seq


def _completed_tool_calls(
    message: Any,
    namespace: tuple[str, ...],
    accumulator: dict[tuple[tuple[str, ...], str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    is_stream_chunk = isinstance(message, AIMessageChunk)
    # AIMessageChunk.tool_calls is derived with a partial-JSON parser. An
    # id/name-only chunk therefore looks like a complete call with args={},
    # even though the real arguments arrive in later tool_call_chunks.
    if not is_stream_chunk:
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict) or not call.get("name"):
                continue
            call_id = str(call.get("id") or new_id("tool"))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            completed[call_id] = {"id": call_id, "name": str(call["name"]), "args": args}

    response_id = str(getattr(message, "id", "") or "")
    response_scope = response_id or "__anonymous__"
    for chunk in getattr(message, "tool_call_chunks", None) or []:
        if not isinstance(chunk, dict):
            continue
        index = int(chunk.get("index") or 0)
        key = (namespace, response_scope, index)
        incoming_id = str(chunk.get("id") or "")
        incoming_name = str(chunk.get("name") or "")
        item = accumulator.get(key)
        if item is None or (
            incoming_id
            and item.get("id") != incoming_id
            and any(item.get(field) for field in ("id", "name", "args"))
        ):
            item = {"id": "", "name": "", "args": ""}
            accumulator[key] = item
        if incoming_id:
            item["id"] = incoming_id
        if incoming_name:
            item["name"] = incoming_name
        raw_args = chunk.get("args")
        if isinstance(raw_args, str):
            item["args"] += raw_args
        elif isinstance(raw_args, dict):
            item["args"] = json.dumps(raw_args)
        if not item["id"] or not item["name"]:
            continue
        if not item["args"] and getattr(message, "chunk_position", None) != "last":
            continue
        try:
            args = json.loads(item["args"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(args, dict):
            completed[item["id"]] = {"id": item["id"], "name": item["name"], "args": args}
            accumulator.pop(key, None)

    if is_stream_chunk and getattr(message, "chunk_position", None) == "last":
        for key, item in list(accumulator.items()):
            if key[:2] != (namespace, response_scope):
                continue
            accumulator.pop(key, None)
            if not item["id"] or not item["name"]:
                continue
            try:
                args = json.loads(item["args"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(args, dict):
                completed[item["id"]] = {"id": item["id"], "name": item["name"], "args": args}
    return list(completed.values())


def _message_text(message: Any) -> str:
    return _content_text(getattr(message, "content", ""))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _merge_usage(total: dict[str, Any], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            current = total.get(key, 0)
            if isinstance(current, int) and not isinstance(current, bool):
                total[key] = current + value
        elif isinstance(value, dict):
            current = total.get(key)
            if not isinstance(current, dict):
                current = {}
                total[key] = current
            _merge_usage(current, value)


def _graph_name(name: str, identifier: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower() or "agent"
    suffix = re.sub(r"[^a-zA-Z0-9]+", "", identifier)[-8:].lower()
    return f"{normalized[:48]}-{suffix}" if suffix else normalized[:56]


__all__ = ["DeepAgentsRuntimeError", "TenantRunContext", "execute_deep_agent"]
