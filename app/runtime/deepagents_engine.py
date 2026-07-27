"""Deep Agents execution adapter for the VMA control plane.

The adapter deliberately keeps tenancy, secrets, event persistence, and sandbox
lifecycle outside Deep Agents. A graph is compiled for one immutable agent revision
and one run, then streamed into the Claude Managed Agents-shaped event protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any, NotRequired

import structlog
from deepagents.graph import GENERAL_PURPOSE_SUBAGENT, DeepAgentState
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, PrivateStateAttr
from langchain_core.callbacks import AsyncCallbackHandler
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
from app.session_errors import session_error_payload

logger = structlog.get_logger()


class DeepAgentsRuntimeError(RuntimeError):
    """Raised when a revision cannot safely execute through Deep Agents."""


@dataclass(frozen=True)
class TenantRunContext:
    organization_id: str
    session_id: str
    agent_id: str
    agent_version_id: str
    turn_marker: dict[str, Any] | None = None


def _merge_turn_evidence(left: Any, right: Any) -> dict[str, Any]:
    """Merge checkpointed model evidence, resetting at a new logical work item."""

    left_value = dict(left) if isinstance(left, dict) else {}
    right_value = dict(right) if isinstance(right, dict) else {}
    right_work_id = str(right_value.get("work_id") or "")
    left_work_id = str(left_value.get("work_id") or "")
    if not right_work_id:
        return left_value
    if right_work_id != left_work_id:
        return {
            "version": 1,
            "work_id": right_work_id,
            "records": dict(right_value.get("records") or {}),
        }
    records = dict(left_value.get("records") or {})
    for evidence_id, record in dict(right_value.get("records") or {}).items():
        existing = records.get(evidence_id)
        if existing is not None and existing != record:
            record = _merge_turn_evidence_record(existing, record)
        records[evidence_id] = record
    return {"version": 1, "work_id": right_work_id, "records": records}


def _merge_turn_evidence_record(left: Any, right: Any) -> dict[str, Any]:
    """Resolve the callback-placeholder race between parallel subagent branches."""

    if _is_turn_evidence_enrichment(left, right):
        return dict(right)
    if _is_turn_evidence_enrichment(right, left):
        return dict(left)
    raise DeepAgentsRuntimeError(
        "A checkpoint model evidence id was reused with different content"
    )


def _is_turn_evidence_enrichment(placeholder: Any, enriched: Any) -> bool:
    """Return whether ``enriched`` only fills a callback-created placeholder."""

    expected_keys = {"scope", "source", "text", "tool_calls", "usage"}
    if not isinstance(placeholder, dict) or not isinstance(enriched, dict):
        return False
    if set(placeholder) != expected_keys or set(enriched) != expected_keys:
        return False
    if placeholder.get("text") != "" or placeholder.get("tool_calls") != []:
        return False
    placeholder_scope = placeholder.get("scope")
    enriched_scope = enriched.get("scope")
    scope_enriched = placeholder_scope == "root" and enriched_scope == "subagent"
    if placeholder_scope != enriched_scope and not scope_enriched:
        return False
    if placeholder.get("source") != enriched.get("source"):
        return False
    if placeholder.get("usage") != enriched.get("usage"):
        return False
    text = enriched.get("text")
    tool_calls = enriched.get("tool_calls")
    if not isinstance(text, str) or not isinstance(tool_calls, list):
        return False
    return bool(text or tool_calls or scope_enriched)


class VmaDeepAgentState(DeepAgentState):
    """Deep Agents state extended with VMA's durable logical-turn identity."""

    vma_turn_marker: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]
    vma_turn_evidence: NotRequired[Annotated[dict[str, Any], _merge_turn_evidence]]


class VmaModelEvidenceCollector(AsyncCallbackHandler):
    """Collect nested model usage, including subagent and summarization calls."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        metadata = kwargs.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        scope = "subagent" if metadata.get("ls_agent_type") == "subagent" else "root"
        source = "summarization" if metadata.get("lc_source") == "summarization" else "agent"
        found = False
        for batch_index, generations in enumerate(getattr(response, "generations", None) or []):
            for generation_index, generation in enumerate(generations or []):
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                usage: dict[str, Any] = {}
                _merge_usage(usage, getattr(message, "usage_metadata", None))
                if not usage:
                    continue
                evidence_id = str(getattr(message, "id", "") or "")
                if not evidence_id:
                    evidence_id = f"llm_{run_id}_{batch_index}_{generation_index}"
                    try:
                        message.id = evidence_id
                    except (AttributeError, TypeError, ValueError):
                        pass
                self._record(
                    evidence_id,
                    scope=scope,
                    source=source,
                    usage=usage,
                )
                found = True
        llm_output = getattr(response, "llm_output", None)
        if found or not isinstance(llm_output, dict):
            return
        usage = {}
        _merge_usage(usage, llm_output.get("token_usage"))
        _merge_usage(usage, llm_output.get("usage"))
        if usage:
            self._record(
                f"llm_{run_id}",
                scope=scope,
                source=source,
                usage=usage,
            )

    def _record(
        self,
        evidence_id: str,
        *,
        scope: str,
        source: str,
        usage: dict[str, Any],
    ) -> None:
        record = {
            "scope": scope,
            "source": source,
            "text": "",
            "tool_calls": [],
            "usage": usage,
        }
        existing = self.records.get(evidence_id)
        if existing is not None and existing != record:
            raise DeepAgentsRuntimeError(
                "A model callback evidence id was reused with different content"
            )
        self.records[evidence_id] = record

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "scope": value["scope"],
                "source": value["source"],
                "text": value["text"],
                "tool_calls": [dict(item) for item in value["tool_calls"]],
                "usage": dict(value["usage"]),
            }
            for key, value in self.records.items()
        }


class VmaModelSpanEmitter(AsyncCallbackHandler):
    """Publish a Managed Agents model-request span around every model call.

    Claude brackets each model request with ``span.model_request_start`` /
    ``span.model_request_end``, and the end event carries that request's token
    usage. It is the only per-request usage signal on the public event stream,
    so metering consumers read it rather than the turn-level totals. Subagent
    and summarization calls are included: they draw the same tokens as the
    coordinator's own requests.
    """

    def __init__(
        self,
        emit_event: RuntimeEventEmitter | None,
        tool_events: list[dict[str, Any]],
        *,
        thread_id: str,
    ) -> None:
        self._emit_event = emit_event
        self._tool_events = tool_events
        self._thread_id = thread_id
        self._open_spans: dict[str, str] = {}

    def _span_event_id(self, run_key: str, event_type: str) -> str:
        return _tool_event_id(self._thread_id, f"llm:{run_key}", event_type)

    async def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any) -> None:
        await self._open_span(run_id)

    async def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any, **kwargs: Any) -> None:
        await self._open_span(run_id)

    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        await self._close_span(run_id, response=response, is_error=False)

    async def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        await self._close_span(run_id, response=None, is_error=True)

    async def _open_span(self, run_id: Any) -> None:
        run_key = str(run_id)
        if run_key in self._open_spans:
            # A chat model reports through both callbacks; bracket it once.
            return
        event_id = self._span_event_id(run_key, "span.model_request_start")
        self._open_spans[run_key] = event_id
        await _emit(
            {
                "type": "span.model_request_start",
                "source": "deepagents",
                "_event_id": event_id,
            },
            self._emit_event,
            self._tool_events,
        )

    async def _close_span(self, run_id: Any, *, response: Any, is_error: bool) -> None:
        run_key = str(run_id)
        start_event_id = self._open_spans.pop(run_key, None)
        if start_event_id is None:
            return
        usage: dict[str, Any] = {}
        for generations in getattr(response, "generations", None) or []:
            for generation in generations or []:
                message = getattr(generation, "message", None)
                if message is not None:
                    _merge_usage(usage, getattr(message, "usage_metadata", None))
        llm_output = getattr(response, "llm_output", None)
        if not usage and isinstance(llm_output, dict):
            _merge_usage(usage, llm_output.get("token_usage"))
            _merge_usage(usage, llm_output.get("usage"))
        await _emit(
            {
                "type": "span.model_request_end",
                "model_request_start_id": start_event_id,
                "is_error": is_error,
                "model_usage": _span_model_usage(usage),
                "source": "deepagents",
                "_event_id": self._span_event_id(run_key, "span.model_request_end"),
            },
            self._emit_event,
            self._tool_events,
        )


class VmaTurnEvidenceMiddleware(AgentMiddleware[VmaDeepAgentState, TenantRunContext, Any]):
    """Checkpoint one deduplicated usage/text record per completed model call."""

    state_schema = VmaDeepAgentState

    def __init__(self, *, scope: str, collector: VmaModelEvidenceCollector) -> None:
        self.scope = scope
        self.collector = collector

    async def aafter_model(
        self,
        state: VmaDeepAgentState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        evidence = state.get("vma_turn_evidence")
        if not isinstance(evidence, dict) or evidence.get("version") != 1:
            return None
        work_id = str(evidence.get("work_id") or "")
        if not work_id:
            return None
        messages = state.get("messages") or []
        message = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
        if message is None:
            return None
        response_id = str(getattr(message, "id", "") or new_id("resp"))
        existing = evidence.get("records")
        records = dict(existing) if isinstance(existing, dict) else {}
        collected_records = self.collector.snapshot()
        for evidence_id, collected_record in collected_records.items():
            if evidence_id not in records:
                records[evidence_id] = collected_record
        usage: dict[str, Any] = {}
        collected = collected_records.get(response_id)
        if isinstance(collected, dict):
            _merge_usage(usage, collected.get("usage"))
        else:
            _merge_usage(usage, getattr(message, "usage_metadata", None))
        tool_calls: list[dict[str, Any]] = []
        for raw_call in getattr(message, "tool_calls", None) or []:
            if not isinstance(raw_call, dict):
                continue
            call_id = str(raw_call.get("id") or "")
            name = str(raw_call.get("name") or "")
            args = raw_call.get("args")
            if not call_id or not name or not isinstance(args, dict):
                raise DeepAgentsRuntimeError("A checkpointed model tool call has no stable identity")
            tool_calls.append({"id": call_id, "name": name, "args": dict(args)})
        records[response_id] = {
            "scope": self.scope,
            "source": str((collected or {}).get("source") or "agent"),
            "text": _message_text(message) if self.scope == "root" else "",
            "tool_calls": tool_calls,
            "usage": usage,
        }
        if isinstance(existing, dict) and existing == records:
            return None
        return {
            "vma_turn_evidence": {
                "version": 1,
                "work_id": work_id,
                "records": records,
            }
        }


class VmaTurnCompletionMiddleware(AgentMiddleware[VmaDeepAgentState, TenantRunContext, Any]):
    """Checkpoint logical turn completion in the graph's terminal middleware node."""

    state_schema = VmaDeepAgentState

    async def abefore_agent(
        self,
        state: VmaDeepAgentState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        marker = getattr(runtime.context, "turn_marker", None)
        if not isinstance(marker, dict):
            return None
        current = state.get("vma_turn_marker")
        if isinstance(current, dict) and _marker_matches(current, marker):
            return None
        return {
            "vma_turn_marker": dict(marker),
            "vma_turn_evidence": {
                "version": 1,
                "work_id": marker["work_id"],
                "records": {},
            },
        }

    async def aafter_agent(
        self,
        state: VmaDeepAgentState,
        runtime: Any,
    ) -> dict[str, Any] | None:
        marker = state.get("vma_turn_marker")
        if not isinstance(marker, dict) or marker.get("phase") != "started":
            return None
        evidence = _validated_turn_evidence(state.get("vma_turn_evidence"), marker)
        usage: dict[str, Any] = {}
        text_parts: list[str] = []
        for record in evidence["records"].values():
            _merge_usage(usage, record.get("usage"))
            if record.get("scope") == "root" and isinstance(record.get("text"), str):
                text_parts.append(record["text"])
        return {
            "vma_turn_marker": {
                **marker,
                "phase": "completed",
                "completion": {
                    "version": 1,
                    "final_text": "".join(text_parts),
                    "usage": usage,
                },
            }
        }


@dataclass
class _EmittedToolCall:
    event_id: str
    event_type: str
    internal_id: str
    internal_name: str
    public_name: str
    args: dict[str, Any]


async def recover_completed_deep_agent_turn(
    version: EffectiveAgentVersion,
    history: list[Any],
    previous_state: dict[str, Any],
    *,
    thread_id: str,
    work_id: str,
    emit_event: RuntimeEventEmitter | None = None,
    begin_recovery: Callable[[], Awaitable[None]] | None = None,
) -> RuntimeResult | None:
    """Finalize a completed checkpoint without reconnecting model, MCP, or sandbox services."""

    if not thread_id or not work_id:
        return None
    processed_seq = _processed_input_seq(history, previous_state)
    expected_marker = {
        "version": 1,
        "work_id": work_id,
        "input_seq": processed_seq,
        "agent_version_id": version.id,
    }
    async with checkpoint_saver() as saver:
        checkpoint_tuple = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint_tuple is None:
        return None
    checkpoint = checkpoint_tuple.checkpoint
    values = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
    marker = values.get("vma_turn_marker") if isinstance(values, dict) else None
    marker = dict(marker) if isinstance(marker, dict) else None
    if _marker_conflicts(marker, expected_marker):
        raise DeepAgentsRuntimeError(
            "The durable checkpoint turn marker conflicts with the current work item"
        )
    if not _marker_matches(marker, expected_marker):
        return None
    phase = _validated_turn_marker_phase(marker)
    if phase != "completed":
        return None
    if checkpoint_tuple.pending_writes or any(
        str(key).startswith("branch:to:") for key in values or {}
    ):
        raise DeepAgentsRuntimeError("A completed checkpoint turn marker still has pending graph work")
    evidence = _validated_turn_evidence((values or {}).get("vma_turn_evidence"), marker)
    completion = _validated_completion(marker)
    usage: dict[str, Any] = {}
    for record in evidence["records"].values():
        _merge_usage(usage, record["usage"])
    if completion["usage"] != usage:
        raise DeepAgentsRuntimeError("The checkpoint turn completion usage does not match its evidence")
    runtime = marker.get("runtime")
    if not isinstance(runtime, dict):
        raise DeepAgentsRuntimeError("The checkpoint turn runtime identity is invalid")
    provider = runtime.get("provider")
    model = runtime.get("model")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise DeepAgentsRuntimeError("The checkpoint turn runtime identity is invalid")
    sandbox_state = marker.get("sandbox_state")
    if not isinstance(sandbox_state, dict):
        raise DeepAgentsRuntimeError("The checkpoint turn sandbox state is invalid")
    if begin_recovery is not None:
        await begin_recovery()

    tool_events: list[dict[str, Any]] = []
    final_text = completion["final_text"]
    if final_text:
        await _emit(
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": final_text}],
                "source": "deepagents",
                "_event_id": _message_event_id(thread_id, processed_seq),
            },
            emit_event,
            tool_events,
        )

    warnings: list[dict[str, Any]] = []
    if sandbox_state.get("backend") == "e2b":
        warning = {
            "type": "sandbox_output_rediscovery_skipped",
            "message": "Completed-turn recovery skipped bounded sandbox output rediscovery",
        }
        warnings.append(warning)
        logger.warning(
            "completed_turn_sandbox_output_rediscovery_skipped",
            work_id=work_id,
            thread_id=thread_id,
        )
    return RuntimeResult(
        final_text=final_text,
        tool_events=tool_events,
        events_persisted=emit_event is not None,
        run_state={
            "backend": "deepagents",
            "agent_version_id": version.id,
            "provider": provider,
            "model": model,
            "last_input_event_seq": processed_seq,
            "pending_actions": [],
            "warnings": warnings,
        },
        sandbox_state=dict(sandbox_state),
        usage=usage,
    )


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
    admit_execution: Callable[[], Awaitable[int]] | None = None,
    begin_recovery: Callable[[], Awaitable[None]] | None = None,
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
    work_id = str(runtime_context.get("work_id") or "")
    if not session_id or not thread_id:
        raise DeepAgentsRuntimeError("Deep Agents execution requires tenant, session, and checkpoint thread ids")

    previous_state = runtime_context.get("previous_run_state")
    previous_state = dict(previous_state) if isinstance(previous_state, dict) else {}
    if not runtime_context.get("_completed_checkpoint_recovery_checked"):
        recovered = await recover_completed_deep_agent_turn(
            version,
            history,
            previous_state,
            thread_id=thread_id,
            work_id=work_id,
            emit_event=emit_event,
            begin_recovery=begin_recovery,
        )
        if recovered is not None:
            return recovered

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
        run_state = dict(previous_state)
        run_state.update(
            {
                "backend": "deepagents",
                "agent_version_id": version.id,
                "provider": provider.provider,
                "model": provider.model_id,
                "last_input_event_seq": processed_seq,
            }
        )
        if work_id:
            run_state["_vma_noop"] = True
        return RuntimeResult(
            events_persisted=emit_event is not None,
            run_state=run_state,
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

        evidence_collector = VmaModelEvidenceCollector()
        span_emitter = VmaModelSpanEmitter(emit_event, tool_events, thread_id=thread_id)
        subagents = _materialize_subagents(runtime_context, secrets if isinstance(secrets, dict) else {})
        for subagent in subagents:
            middleware = list(subagent.get("middleware") or [])
            subagent_interrupts = subagent.pop("interrupt_on", None)
            if subagent_interrupts:
                middleware.append(HumanInTheLoopMiddleware(interrupt_on=subagent_interrupts))
            middleware.append(
                VmaTurnEvidenceMiddleware(scope="subagent", collector=evidence_collector)
            )
            subagent["middleware"] = middleware
        if version.multiagent and not any(item.get("name") == "general-purpose" for item in subagents):
            subagents.append(
                {
                    "name": "general-purpose",
                    "description": GENERAL_PURPOSE_SUBAGENT["description"],
                    "system_prompt": GENERAL_PURPOSE_SUBAGENT["system_prompt"],
                    "model": model,
                    "middleware": [
                        HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
                        VmaTurnEvidenceMiddleware(
                            scope="subagent",
                            collector=evidence_collector,
                        ),
                    ]
                    if interrupt_on
                    else [
                        VmaTurnEvidenceMiddleware(
                            scope="subagent",
                            collector=evidence_collector,
                        )
                    ],
                }
            )

        from deepagents import create_deep_agent

        graph = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=version.system or "You are a helpful managed agent.",
            middleware=[
                ToolFilterMiddleware(excluded=excluded),
                HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
                VmaTurnEvidenceMiddleware(scope="root", collector=evidence_collector),
                VmaTurnCompletionMiddleware(),
            ]
            if interrupt_on
            else [
                ToolFilterMiddleware(excluded=excluded),
                VmaTurnEvidenceMiddleware(scope="root", collector=evidence_collector),
                VmaTurnCompletionMiddleware(),
            ],
            subagents=subagents or None,
            skills=skill_sources or None,
            memory=memory_sources or None,
            permissions=permissions or None,
            backend=backend_handle.backend,
            interrupt_on=None,
            state_schema=VmaDeepAgentState,
            context_schema=TenantRunContext,
            checkpointer=saver,
            name=_graph_name(version.name, version.agent_id),
        )

        if isinstance(graph_input, dict) and state_files:
            graph_input = {**graph_input, "files": state_files}

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(10, int(get_settings().vma_max_graph_steps)),
            "callbacks": [evidence_collector, span_emitter],
        }
        checkpoint = await graph.aget_state(config) if work_id else None
        marker = _checkpoint_turn_marker(checkpoint)
        seed_messages: list[Any] = []
        recover_without_graph = False
        resume_finalize_only = False
        recovery_interrupts: list[Any] = []
        recovery_usage: dict[str, Any] | None = None
        recovery_final_text: str | None = None
        if work_id:
            expected_marker = {
                "version": 1,
                "work_id": work_id,
                "input_seq": processed_seq,
                "agent_version_id": version.id,
            }
            marker_match = _marker_matches(marker, expected_marker)
            if _marker_conflicts(marker, expected_marker):
                raise DeepAgentsRuntimeError(
                    "The durable checkpoint turn marker conflicts with the current work item"
                )
            if marker_match:
                phase = _validated_turn_marker_phase(marker)
                seed_messages = _checkpoint_turn_messages(checkpoint, marker)
                recovery_usage = _checkpoint_turn_usage(checkpoint, marker)
                recovery_interrupts = _checkpoint_interrupts(checkpoint)
                pending_nodes = tuple(getattr(checkpoint, "next", ()) or ())
                if phase == "completed":
                    if pending_nodes or recovery_interrupts:
                        raise DeepAgentsRuntimeError(
                            "A completed checkpoint turn marker still has pending graph work"
                        )
                    completion = _validated_completion(marker)
                    if completion["usage"] != recovery_usage:
                        raise DeepAgentsRuntimeError(
                            "The checkpoint turn completion usage does not match its evidence"
                        )
                    recovery_final_text = completion["final_text"]
                    recover_without_graph = True
                elif recovery_interrupts:
                    recover_without_graph = True
                else:
                    if not pending_nodes:
                        raise DeepAgentsRuntimeError(
                            "A started checkpoint turn marker has no pending graph work"
                        )
                    graph_input = None
                    resume_finalize_only = _checkpoint_only_completion_pending(checkpoint)
            else:
                marker = {
                    **expected_marker,
                    "phase": "started",
                    "input_message_id": _turn_input_message_id(work_id, processed_seq),
                    "runtime": {
                        "provider": provider.provider,
                        "model": provider.model_id,
                    },
                    "sandbox_state": {
                        **backend_handle.plan.summary,
                        "runtime_backend": "deepagents",
                    },
                }
                graph_input = _graph_input_with_turn_marker(graph_input, marker)

        context = TenantRunContext(
            organization_id=organization_id,
            session_id=session_id,
            agent_id=version.agent_id,
            agent_version_id=version.id,
            turn_marker=marker if work_id else None,
        )

        timeout_seconds = max(1, int(get_settings().vma_run_timeout_seconds))
        if recover_without_graph:
            if begin_recovery is not None:
                await begin_recovery()
            streamed = await _recover_checkpoint_stream(
                seed_messages,
                interrupts=recovery_interrupts,
                thread_id=thread_id,
                processed_seq=processed_seq,
                emit_event=emit_event,
                tool_events=tool_events,
                custom_names=custom_names,
                custom_specs=custom_specs,
                mcp_tool_names=mcp_tool_names,
                interrupt_on=interrupt_on,
                usage_override=recovery_usage,
                final_text_override=recovery_final_text,
            )
        else:
            if resume_finalize_only and begin_recovery is not None:
                await begin_recovery()
            else:
                if admit_execution is not None:
                    await admit_execution()
                await _emit_mcp_connection_warnings(warnings, emit_event, tool_events)
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
                    thread_id=thread_id,
                    processed_seq=processed_seq,
                    seed_messages=seed_messages,
                )
            if work_id:
                latest_checkpoint = await graph.aget_state(config)
                latest_marker = _checkpoint_turn_marker(latest_checkpoint)
                if not _marker_matches(latest_marker, expected_marker):
                    raise DeepAgentsRuntimeError(
                        "The durable checkpoint lost the current turn marker"
                    )
                latest_phase = _validated_turn_marker_phase(latest_marker)
                checkpoint_usage = _checkpoint_turn_usage(latest_checkpoint, latest_marker)
                streamed["usage"] = checkpoint_usage
                if latest_phase == "completed":
                    completion = _validated_completion(latest_marker)
                    if completion["usage"] != checkpoint_usage:
                        raise DeepAgentsRuntimeError(
                            "The checkpoint turn completion usage does not match its evidence"
                        )
                    if completion["final_text"] != streamed["final_text"]:
                        raise DeepAgentsRuntimeError(
                            "The streamed completion text does not match its checkpoint evidence"
                        )
        if backend_handle.plan.backend == "e2b":
            try:
                if backend_handle.connection is None:
                    raise DeepAgentsRuntimeError(
                        "E2B output discovery requires the live sandbox connection"
                    )
                from app.runtime.sandbox_lifecycle import build_e2b_provider

                sandbox_outputs = await build_e2b_provider().discover_outputs(
                    backend_handle.connection,
                    root=SANDBOX_OUTPUT_ROOT,
                    max_files=MAX_DISCOVERED_OUTPUT_FILES,
                    max_file_bytes=MAX_OUTPUT_FILE_BYTES,
                    max_total_bytes=MAX_OUTPUT_TOTAL_BYTES,
                )
            except Exception as exc:
                if not (recover_without_graph or resume_finalize_only):
                    raise
                warning = {
                    "type": "sandbox_output_rediscovery_skipped",
                    "message": "Recovery skipped unavailable bounded sandbox output discovery",
                }
                warnings.append(warning)
                logger.warning(
                    "recovery_sandbox_output_rediscovery_skipped",
                    work_id=work_id,
                    thread_id=thread_id,
                    error=str(exc),
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


def _message_event_id(thread_id: str, processed_seq: int) -> str:
    material = f"{thread_id}:{processed_seq}:final"
    return "evt_" + hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]


def _turn_input_message_id(work_id: str, processed_seq: int) -> str:
    material = f"{work_id}:{processed_seq}:input"
    return "msg_" + hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]


def _tool_event_id(thread_id: str, tool_use_id: str, event_type: str) -> str:
    material = f"{thread_id}:{tool_use_id}:{event_type}"
    return "evt_" + hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]


# Harness-internal Deep Agents tools. They have no Claude Managed Agents
# equivalent, and their results are deliberately withheld, so publishing their
# calls would leave every consumer with a tool use that never completes.
_HARNESS_INTERNAL_TOOLS = frozenset({"write_todos", "task"})


def _tool_use_event_type(
    internal_name: str,
    *,
    custom_names: set[str],
    mcp_tool_names: set[str],
) -> str:
    if internal_name in custom_names:
        return "agent.custom_tool_use"
    if internal_name in mcp_tool_names:
        return "agent.mcp_tool_use"
    return "agent.tool_use"


def _graph_input_with_turn_marker(
    graph_input: dict[str, Any] | Command,
    marker: dict[str, Any],
) -> dict[str, Any] | Command:
    evidence = {"version": 1, "work_id": marker["work_id"], "records": {}}
    if isinstance(graph_input, dict):
        messages = list(graph_input.get("messages") or [])
        if messages and isinstance(messages[0], dict):
            messages[0] = {**messages[0], "id": marker.get("input_message_id")}
        return {**graph_input, "messages": messages, "vma_turn_evidence": evidence}
    update = graph_input.update
    if update is None:
        update = {}
    if not isinstance(update, dict):
        raise DeepAgentsRuntimeError("The graph resume command has an unsupported state update")
    return Command(
        graph=graph_input.graph,
        update={
            **update,
            "vma_turn_marker": marker,
            "vma_turn_evidence": evidence,
        },
        resume=graph_input.resume,
        goto=graph_input.goto,
    )


def _checkpoint_turn_marker(checkpoint: Any) -> dict[str, Any] | None:
    values = getattr(checkpoint, "values", None)
    if not isinstance(values, dict):
        return None
    marker = values.get("vma_turn_marker")
    return dict(marker) if isinstance(marker, dict) else None


def _validated_turn_marker_phase(marker: dict[str, Any]) -> str:
    phase = marker.get("phase")
    if phase not in {"started", "completed"}:
        raise DeepAgentsRuntimeError("The checkpoint turn marker phase is invalid")
    return str(phase)


def _validated_completion(marker: dict[str, Any]) -> dict[str, Any]:
    completion = marker.get("completion")
    if not isinstance(completion, dict) or completion.get("version") != 1:
        raise DeepAgentsRuntimeError("The checkpoint turn completion envelope is invalid")
    if not isinstance(completion.get("final_text"), str):
        raise DeepAgentsRuntimeError("The checkpoint turn completion text is invalid")
    if not isinstance(completion.get("usage"), dict):
        raise DeepAgentsRuntimeError("The checkpoint turn completion usage is invalid")
    return dict(completion)


def _validated_turn_evidence(value: Any, marker: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise DeepAgentsRuntimeError("The checkpoint turn evidence is invalid")
    if str(value.get("work_id") or "") != str(marker.get("work_id") or ""):
        raise DeepAgentsRuntimeError("The checkpoint turn evidence belongs to another work item")
    raw_records = value.get("records")
    if not isinstance(raw_records, dict):
        raise DeepAgentsRuntimeError("The checkpoint turn evidence records are invalid")
    records: dict[str, dict[str, Any]] = {}
    for raw_id, raw_record in raw_records.items():
        response_id = str(raw_id or "")
        if not response_id or not isinstance(raw_record, dict):
            raise DeepAgentsRuntimeError("A checkpoint turn evidence record is invalid")
        scope = raw_record.get("scope")
        text = raw_record.get("text")
        tool_calls = raw_record.get("tool_calls")
        usage = raw_record.get("usage")
        if scope not in {"root", "subagent"} or not isinstance(text, str):
            raise DeepAgentsRuntimeError("A checkpoint turn evidence response is invalid")
        if not isinstance(tool_calls, list) or any(not isinstance(item, dict) for item in tool_calls):
            raise DeepAgentsRuntimeError("A checkpoint turn evidence tool call is invalid")
        if not isinstance(usage, dict):
            raise DeepAgentsRuntimeError("A checkpoint turn evidence usage record is invalid")
        normalized_calls: list[dict[str, Any]] = []
        for item in tool_calls:
            call_id = str(item.get("id") or "")
            name = str(item.get("name") or "")
            args = item.get("args")
            if not call_id or not name or not isinstance(args, dict):
                raise DeepAgentsRuntimeError("A checkpoint turn evidence tool call has no stable identity")
            normalized_calls.append({"id": call_id, "name": name, "args": dict(args)})
        records[response_id] = {
            "scope": scope,
            "text": text,
            "tool_calls": normalized_calls,
            "usage": dict(usage),
        }
    return {"version": 1, "work_id": str(value["work_id"]), "records": records}


def _marker_matches(marker: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    return marker is not None and all(marker.get(key) == value for key, value in expected.items())


def _marker_conflicts(marker: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if marker is None or marker.get("input_seq") != expected["input_seq"]:
        return False
    return not _marker_matches(marker, expected)


def _checkpoint_turn_messages(checkpoint: Any, marker: dict[str, Any]) -> list[Any]:
    values = getattr(checkpoint, "values", None)
    if not isinstance(values, dict):
        raise DeepAgentsRuntimeError("The checkpoint turn state is invalid")
    evidence = _validated_turn_evidence(values.get("vma_turn_evidence"), marker)
    messages: list[Any] = []
    for response_id, record in evidence["records"].items():
        if record["scope"] != "root":
            continue
        messages.append(
            AIMessage(
                content=record["text"],
                id=response_id,
                tool_calls=record["tool_calls"],
            )
        )
    return messages


def _checkpoint_turn_usage(checkpoint: Any, marker: dict[str, Any]) -> dict[str, Any]:
    values = getattr(checkpoint, "values", None)
    if not isinstance(values, dict):
        raise DeepAgentsRuntimeError("The checkpoint turn state is invalid")
    evidence = _validated_turn_evidence(values.get("vma_turn_evidence"), marker)
    usage: dict[str, Any] = {}
    for record in evidence["records"].values():
        _merge_usage(usage, record["usage"])
    return usage


def _checkpoint_interrupts(checkpoint: Any) -> list[Any]:
    found: list[Any] = []
    seen: set[tuple[str, str]] = set()
    sources = [getattr(checkpoint, "interrupts", ())]
    for task in getattr(checkpoint, "tasks", ()) or ():
        sources.append(getattr(task, "interrupts", ()))
    for source in sources:
        for interrupt in source or ():
            key = (
                str(getattr(interrupt, "id", "") or ""),
                repr(getattr(interrupt, "value", None)),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(interrupt)
    return found


def _checkpoint_only_completion_pending(checkpoint: Any) -> bool:
    pending = tuple(str(item) for item in (getattr(checkpoint, "next", ()) or ()))
    return bool(pending) and set(pending) == {"VmaTurnCompletionMiddleware.after_agent"}


async def _emit_mcp_connection_warnings(
    warnings: list[dict[str, Any]],
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
) -> None:
    for warning in warnings:
        if warning.get("type") != "mcp_connection_error":
            continue
        await _emit(
            session_error_payload(
                warning.get("message") or "MCP connection failed",
                error_type="mcp_connection_error",
                retry_status="exhausted",
                mcp_server_name=warning.get("server_name"),
                source="deepagents",
            ),
            emit_event,
            tool_events,
        )


async def _seed_stream_state(
    messages: list[Any],
    *,
    thread_id: str,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    custom_specs: dict[str, dict[str, Any]],
    mcp_tool_names: set[str],
    interrupt_on: dict[str, Any],
) -> tuple[list[str], dict[str, _EmittedToolCall], dict[str, Any]]:
    text_parts: list[str] = []
    emitted_calls: dict[str, _EmittedToolCall] = {}
    usage: dict[str, Any] = defaultdict(int)
    for message in messages:
        _merge_usage(usage, getattr(message, "usage_metadata", None))
        if isinstance(message, (AIMessage, AIMessageChunk)):
            for raw_call in getattr(message, "tool_calls", None) or []:
                if not isinstance(raw_call, dict):
                    continue
                internal_id = str(raw_call.get("id") or "")
                name = str(raw_call.get("name") or "")
                args = raw_call.get("args")
                if not internal_id or not name or internal_id in emitted_calls:
                    continue
                call = {
                    "id": internal_id,
                    "name": name,
                    "args": args if isinstance(args, dict) else {},
                }
                emitted_calls[internal_id] = await _emit_tool_use(
                    call,
                    emit_event=emit_event,
                    tool_events=tool_events,
                    custom_names=custom_names,
                    custom_specs=custom_specs,
                    mcp_tool_names=mcp_tool_names,
                    requires_confirmation=name in interrupt_on,
                    thread_id=thread_id,
                )
            text = _message_text(message)
            if text:
                text_parts.append(text)
        elif isinstance(message, ToolMessage):
            await _emit_tool_result(
                message,
                emitted_calls,
                emit_event=emit_event,
                tool_events=tool_events,
                custom_names=custom_names,
                mcp_tool_names=mcp_tool_names,
                thread_id=thread_id,
            )
    return text_parts, emitted_calls, usage


async def _recover_checkpoint_stream(
    messages: list[Any],
    *,
    interrupts: list[Any],
    thread_id: str,
    processed_seq: int,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    custom_specs: dict[str, dict[str, Any]],
    mcp_tool_names: set[str],
    interrupt_on: dict[str, Any],
    usage_override: dict[str, Any] | None = None,
    final_text_override: str | None = None,
) -> dict[str, Any]:
    text_parts, emitted_calls, usage = await _seed_stream_state(
        messages,
        thread_id=thread_id,
        emit_event=emit_event,
        tool_events=tool_events,
        custom_names=custom_names,
        custom_specs=custom_specs,
        mcp_tool_names=mcp_tool_names,
        interrupt_on=interrupt_on,
    )
    pending_actions = await _persist_interrupt_actions(
        interrupts,
        emitted_calls,
        emit_event=emit_event,
        tool_events=tool_events,
        custom_names=custom_names,
        custom_specs=custom_specs,
        mcp_tool_names=mcp_tool_names,
        thread_id=thread_id,
    )
    final_text = final_text_override if final_text_override is not None else "".join(text_parts)
    if final_text:
        await _emit(
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": final_text}],
                "source": "deepagents",
                "_event_id": _message_event_id(thread_id, processed_seq),
            },
            emit_event,
            tool_events,
        )
    return {
        "final_text": final_text,
        "pending_actions": pending_actions,
        "usage": dict(usage_override) if usage_override is not None else dict(usage),
    }


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
    thread_id: str = "",
    processed_seq: int = 0,
    seed_messages: list[Any] | None = None,
) -> dict[str, Any]:
    text_parts, emitted_calls, usage = await _seed_stream_state(
        list(seed_messages or []),
        thread_id=thread_id,
        emit_event=emit_event,
        tool_events=tool_events,
        custom_names=custom_names,
        custom_specs=custom_specs,
        mcp_tool_names=mcp_tool_names,
        interrupt_on=interrupt_on,
    )
    message_event_id: str | None = (
        _message_event_id(thread_id, processed_seq) if text_parts else None
    )
    preview_started = False
    tool_accumulator: dict[tuple[tuple[str, ...], str, int], dict[str, Any]] = {}
    pending_interrupts: list[Any] = []

    async for item in graph.astream(
        graph_input,
        config=config,
        context=context,
        stream_mode=["messages", "updates"],
        subgraphs=True,
        durability="sync",
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
                thread_id=thread_id,
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
                thread_id=thread_id,
            )
            continue

        if namespace_key or not isinstance(message, (AIMessage, AIMessageChunk)):
            continue
        text = _message_text(message)
        if not text:
            continue
        if message_event_id is None:
            message_event_id = _message_event_id(thread_id, processed_seq)
        if not preview_started and emit_preview is not None:
            await emit_preview(
                {"type": "event_start", "event": {"type": "agent.message", "id": message_event_id}}
            )
            preview_started = True
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
        thread_id=thread_id,
    )
    final_text = "".join(text_parts)
    if final_text:
        payload = {
            "type": "agent.message",
            "content": [{"type": "text", "text": final_text}],
            "source": "deepagents",
            "_event_id": message_event_id or _message_event_id(thread_id, processed_seq),
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
    thread_id: str = "",
) -> _EmittedToolCall:
    internal_name = call["name"]
    public_name = DEEP_TO_CLAUDE_TOOL.get(internal_name, internal_name)
    event_type = _tool_use_event_type(
        internal_name,
        custom_names=custom_names,
        mcp_tool_names=mcp_tool_names,
    )
    event_id = _tool_event_id(thread_id, str(call["id"]), event_type)
    emitted = _EmittedToolCall(
        event_id=event_id,
        event_type=event_type,
        internal_id=call["id"],
        internal_name=internal_name,
        public_name=public_name,
        args=call["args"],
    )
    if internal_name in _HARNESS_INTERNAL_TOOLS:
        # Tracked for interrupt matching and result suppression, never published.
        return emitted
    # The Managed Agents tool-use event carries only `id`, `name`, and `input`;
    # the provider-internal call id stays in ``_EmittedToolCall`` and, for
    # interrupts, in the run state's ``tool_call_id``.
    payload: dict[str, Any] = {
        "type": event_type,
        "name": public_name,
        "input": call["args"],
        "source": "deepagents",
        "_event_id": event_id,
    }
    if internal_name in custom_specs:
        payload["tool"] = custom_specs[internal_name]
    if requires_confirmation:
        payload["requires_confirmation"] = True
    await _emit(payload, emit_event, tool_events)
    return emitted


async def _emit_tool_result(
    message: ToolMessage,
    emitted_calls: dict[str, _EmittedToolCall],
    *,
    emit_event: RuntimeEventEmitter | None,
    tool_events: list[dict[str, Any]],
    custom_names: set[str],
    mcp_tool_names: set[str],
    thread_id: str = "",
) -> None:
    internal_id = str(message.tool_call_id or "")
    if not internal_id:
        raise DeepAgentsRuntimeError("A tool result has no stable tool call identity")
    emitted = emitted_calls.get(internal_id)
    name = str(getattr(message, "name", "") or (emitted.internal_name if emitted else "tool"))
    if name in custom_names or name in _HARNESS_INTERNAL_TOOLS:
        return
    is_mcp = name in mcp_tool_names
    event_type = "agent.mcp_tool_result" if is_mcp else "agent.tool_result"
    use_event_type = "agent.mcp_tool_use" if is_mcp else "agent.tool_use"
    # Managed Agents results reference the *event* that opened the call, not the
    # provider-internal call id, and the field is named per result type:
    # `tool_use_id` on agent.tool_result, `mcp_tool_use_id` on
    # agent.mcp_tool_result. The tool-use event id is derived from the same
    # material, so it resolves even when the use landed in an earlier batch.
    use_event_id = (
        emitted.event_id
        if emitted is not None
        else _tool_event_id(thread_id, internal_id, use_event_type)
    )
    reference_field = "mcp_tool_use_id" if is_mcp else "tool_use_id"
    await _emit(
        {
            "type": event_type,
            "name": DEEP_TO_CLAUDE_TOOL.get(name, name),
            reference_field: use_event_id,
            "content": [{"type": "text", "text": _content_text(message.content)}],
            "source": "deepagents",
            "_event_id": _tool_event_id(thread_id, internal_id, event_type),
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
    thread_id: str = "",
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
                raise DeepAgentsRuntimeError(
                    "A checkpointed interrupt has no matching stable tool call"
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
    command, candidate, processed_seq = _next_graph_input(history, previous_state)
    if command is not None:
        return command, processed_seq
    if candidate is None:
        return None, processed_seq


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
    return {"messages": [{"role": "user", "content": content}]}, processed_seq


def _processed_input_seq(history: list[Any], previous_state: dict[str, Any]) -> int:
    _command, _candidate, processed_seq = _next_graph_input(history, previous_state)
    return processed_seq


def _next_graph_input(
    history: list[Any],
    previous_state: dict[str, Any],
) -> tuple[Command | None, Any | None, int]:
    pending = previous_state.get("pending_actions")
    if isinstance(pending, list) and pending:
        command, seq = _resume_command(history, pending)
        if command is not None:
            return command, None, seq

    last_seq = int(previous_state.get("last_input_event_seq") or 0)
    candidate = None
    for event in history:
        if event.seq > last_seq and event.type in {"user.message", "user.define_outcome"}:
            candidate = event
    if candidate is None:
        return None, None, last_seq
    return None, candidate, int(candidate.seq)


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
            call_id = str(call.get("id") or "")
            if not call_id:
                raise DeepAgentsRuntimeError("A model tool call has no stable identity")
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


def _span_token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _span_model_usage(usage: Any) -> dict[str, int]:
    """Project LangChain usage metadata onto the Managed Agents span shape.

    LangChain counts cached tokens inside ``input_tokens``; Managed Agents
    reports them alongside an input count that excludes them, so the cached
    portions are subtracted rather than double-counted.
    """
    data = usage if isinstance(usage, dict) else {}
    details = data.get("input_token_details")
    details = details if isinstance(details, dict) else {}
    cache_read = _span_token_count(details.get("cache_read"))
    cache_creation = _span_token_count(details.get("cache_creation"))
    input_tokens = _span_token_count(data.get("input_tokens")) - cache_read - cache_creation
    return {
        "input_tokens": max(input_tokens, 0),
        "output_tokens": _span_token_count(data.get("output_tokens")),
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def _graph_name(name: str, identifier: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower() or "agent"
    suffix = re.sub(r"[^a-zA-Z0-9]+", "", identifier)[-8:].lower()
    return f"{normalized[:48]}-{suffix}" if suffix else normalized[:56]


__all__ = ["DeepAgentsRuntimeError", "TenantRunContext", "execute_deep_agent"]
