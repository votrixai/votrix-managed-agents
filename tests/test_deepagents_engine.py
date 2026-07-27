from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents.backends import StateBackend
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, ToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, PrivateAttr

from app.runtime.contracts import EffectiveAgentVersion
from app.runtime.deepagents_engine import (
    DeepAgentsRuntimeError,
    VmaModelSpanEmitter,
    _completed_tool_calls,
    _emit_tool_result,
    _emit_tool_use,
    _graph_input,
    _message_event_id,
    _merge_turn_evidence,
    _span_model_usage,
    _stream_graph,
    _tool_event_id,
    execute_deep_agent,
)
from app.runtime.providers import RuntimeProviderCapabilities, RuntimeProviderConfig
from app.runtime.sandbox import BackendHandle, SandboxRuntimePlan
from app.runtime.sandbox_inputs import sandbox_input_bundle


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    bound_tools: Sequence[dict[str, Any] | type | Callable | BaseTool] = ()
    _index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "vma-scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        self.bound_tools = tools
        return self


class _SimulatedModelCrash(BaseException):
    pass


class _StreamGraph:
    def __init__(self, messages):
        self.messages = messages

    async def astream(self, *_args, **_kwargs):
        for message in self.messages:
            yield (), "messages", (message, {})


def _version(*, tools=None, multiagent=None) -> EffectiveAgentVersion:
    return EffectiveAgentVersion(
        id="agtv_test",
        agent_id="agt_test",
        version=1,
        name="Test Agent",
        model={"id": "fake-model", "provider": "fake"},
        system="Be concise.",
        description=None,
        tools=tools or [],
        mcp_servers=[],
        skills=[],
        multiagent=multiagent,
        metadata_={},
        runtime={},
    )


def _event(seq: int, event_type: str, **payload):
    return SimpleNamespace(seq=seq, type=event_type, payload={"type": event_type, **payload})


def _tool_chunk(*, call_id=None, name=None, args="", index=0):
    return SimpleNamespace(
        tool_calls=[],
        tool_call_chunks=[{"id": call_id, "name": name, "args": args, "index": index}],
    )


def test_streamed_tool_calls_wait_for_complete_args_and_reset_reused_index():
    accumulator = {}

    assert _completed_tool_calls(
        _tool_chunk(call_id="call_read", name="read_file"), (), accumulator
    ) == []
    assert _completed_tool_calls(
        _tool_chunk(args='{"file_path":"/skills/example/SKILL.md"}'), (), accumulator
    ) == [
        {
            "id": "call_read",
            "name": "read_file",
            "args": {"file_path": "/skills/example/SKILL.md"},
        }
    ]

    assert _completed_tool_calls(
        _tool_chunk(call_id="call_pwd", name="execute"), (), accumulator
    ) == []
    assert _completed_tool_calls(
        _tool_chunk(args='{"command":"pwd"}'), (), accumulator
    ) == [
        {"id": "call_pwd", "name": "execute", "args": {"command": "pwd"}}
    ]

    assert _completed_tool_calls(
        _tool_chunk(call_id="call_marker", name="execute", args='{"command":"cat marker"}'),
        (),
        accumulator,
    ) == [
        {
            "id": "call_marker",
            "name": "execute",
            "args": {"command": "cat marker"},
        }
    ]


def test_runtime_event_ids_are_stable_for_logical_event_identity():
    assert _message_event_id("thread_a", 7) == _message_event_id("thread_a", 7)
    assert _message_event_id("thread_a", 7) != _message_event_id("thread_a", 8)
    assert _tool_event_id("thread_a", "call_a", "agent.tool_use") == _tool_event_id(
        "thread_a", "call_a", "agent.tool_use"
    )
    assert _tool_event_id("thread_a", "call_a", "agent.tool_use") != _tool_event_id(
        "thread_a", "call_a", "agent.tool_result"
    )


def test_span_model_usage_separates_cached_tokens_from_fresh_input():
    projected = _span_model_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 120,
            "input_token_details": {"cache_read": 600, "cache_creation": 150},
        }
    )
    assert projected == {
        "input_tokens": 250,
        "output_tokens": 120,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 150,
    }
    # Absent, malformed, and negative counts all collapse to zero rather than
    # producing a span the SDK cannot parse.
    assert _span_model_usage(None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert _span_model_usage({"input_tokens": 10, "input_token_details": {"cache_read": 40}})[
        "input_tokens"
    ] == 0


async def test_model_spans_bracket_each_request_and_carry_its_usage():
    durable: list[dict[str, Any]] = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    emitter = VmaModelSpanEmitter(emit_event, [], thread_id="thread_a")
    await emitter.on_chat_model_start({}, [], run_id="run-1")
    # A chat model reports through both start callbacks; only one span opens.
    await emitter.on_llm_start({}, [], run_id="run-1")
    await emitter.on_llm_end(
        SimpleNamespace(
            generations=[
                [
                    SimpleNamespace(
                        message=SimpleNamespace(
                            usage_metadata={
                                "input_tokens": 900,
                                "output_tokens": 40,
                                "input_token_details": {"cache_read": 800},
                            }
                        )
                    )
                ]
            ],
            llm_output=None,
        ),
        run_id="run-1",
    )

    assert [event["type"] for event in durable] == [
        "span.model_request_start",
        "span.model_request_end",
    ]
    start, end = durable
    assert end["model_request_start_id"] == start["_event_id"]
    assert end["is_error"] is False
    assert end["model_usage"] == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 0,
    }

    # A failed request still closes its span so consumers never leak one.
    durable.clear()
    await emitter.on_chat_model_start({}, [], run_id="run-2")
    await emitter.on_llm_error(RuntimeError("boom"), run_id="run-2")
    assert [event["type"] for event in durable] == [
        "span.model_request_start",
        "span.model_request_end",
    ]
    assert durable[1]["is_error"] is True

    # An end without a matching start is dropped rather than emitted unpaired.
    durable.clear()
    await emitter.on_llm_end(SimpleNamespace(generations=[], llm_output=None), run_id="run-3")
    assert durable == []


async def test_mcp_tool_results_use_the_managed_agents_reference_field():
    durable: list[dict[str, Any]] = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    emitted = await _emit_tool_use(
        {"id": "call_mcp", "name": "linear_search", "args": {"query": "bug"}},
        emit_event=emit_event,
        tool_events=[],
        custom_names=set(),
        custom_specs={},
        mcp_tool_names={"linear_search"},
        requires_confirmation=False,
        thread_id="thread_a",
    )
    await _emit_tool_result(
        ToolMessage(content="ok", name="linear_search", tool_call_id="call_mcp"),
        {"call_mcp": emitted},
        emit_event=emit_event,
        tool_events=[],
        custom_names=set(),
        mcp_tool_names={"linear_search"},
        thread_id="thread_a",
    )

    use, result = durable
    assert use["type"] == "agent.mcp_tool_use"
    assert result["type"] == "agent.mcp_tool_result"
    # MCP results reference the use event through `mcp_tool_use_id`, not the
    # `tool_use_id` used by built-in tool results.
    assert result["mcp_tool_use_id"] == use["_event_id"]
    assert "tool_use_id" not in result


async def test_harness_internal_tools_stay_off_the_public_event_stream():
    durable: list[dict[str, Any]] = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    emitted = await _emit_tool_use(
        {"id": "call_todo", "name": "write_todos", "args": {"todos": []}},
        emit_event=emit_event,
        tool_events=[],
        custom_names=set(),
        custom_specs={},
        mcp_tool_names=set(),
        requires_confirmation=False,
        thread_id="thread_a",
    )
    await _emit_tool_result(
        ToolMessage(content="ok", name="write_todos", tool_call_id="call_todo"),
        {"call_todo": emitted},
        emit_event=emit_event,
        tool_events=[],
        custom_names=set(),
        mcp_tool_names=set(),
        thread_id="thread_a",
    )

    # Neither half is published, so no consumer sees a call that never finishes.
    assert durable == []
    # The call is still tracked in-process for interrupt matching.
    assert emitted.internal_id == "call_todo"


def test_turn_evidence_merge_only_accepts_callback_placeholder_enrichment():
    placeholder = {
        "scope": "root",
        "source": "agent",
        "text": "",
        "tool_calls": [],
        "usage": {"total_tokens": 5},
    }
    enriched = {
        "scope": "subagent",
        "source": "agent",
        "text": "",
        "tool_calls": [{"id": "call_a", "name": "write_todos", "args": {}}],
        "usage": {"total_tokens": 5},
    }

    def envelope(record):
        return {"version": 1, "work_id": "work_parallel", "records": {"response_a": record}}

    assert _merge_turn_evidence(envelope(placeholder), envelope(enriched))["records"][
        "response_a"
    ] == enriched
    assert _merge_turn_evidence(envelope(enriched), envelope(placeholder))["records"][
        "response_a"
    ] == enriched

    conflicting = {
        **enriched,
        "tool_calls": [{"id": "call_a", "name": "write_todos", "args": {"changed": True}}],
    }
    with pytest.raises(DeepAgentsRuntimeError, match="reused with different content"):
        _merge_turn_evidence(envelope(enriched), envelope(conflicting))


async def test_stream_graph_keeps_tool_names_inputs_and_ids_aligned():
    graph = _StreamGraph(
        [
            AIMessageChunk(
                content="",
                id="response_read",
                tool_call_chunks=[{"id": "call_read", "name": "read_file", "args": "", "index": 0}],
            ),
            AIMessageChunk(
                content="",
                id="response_read",
                tool_call_chunks=[
                    {
                        "id": None,
                        "name": None,
                        "args": '{"file_path":"/skills/example/SKILL.md"}',
                        "index": 0,
                    }
                ],
                chunk_position="last",
            ),
            ToolMessage(
                content="skill content",
                name="read_file",
                tool_call_id="call_read",
            ),
            AIMessageChunk(
                content="",
                id="response_pwd",
                tool_call_chunks=[{"id": "call_pwd", "name": "execute", "args": "", "index": 0}],
            ),
            AIMessageChunk(
                content="",
                id="response_pwd",
                tool_call_chunks=[
                    {"id": None, "name": None, "args": '{"command":"pwd"}', "index": 0}
                ],
                chunk_position="last",
            ),
            ToolMessage(content="/workspace", name="execute", tool_call_id="call_pwd"),
            AIMessageChunk(
                content="",
                id="response_marker",
                tool_call_chunks=[
                    {
                        "id": "call_marker",
                        "name": "execute",
                        "args": '{"command":"cat /workspace/vma-e2e-marker.txt"}',
                        "index": 0,
                    }
                ],
                chunk_position="last",
            ),
            ToolMessage(
                content="VMA_E2E_PERSISTED",
                name="execute",
                tool_call_id="call_marker",
            ),
            AIMessageChunk(content="done", id="response_final", chunk_position="last"),
        ]
    )
    durable = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    result = await _stream_graph(
        graph,
        {"messages": []},
        config={},
        context=SimpleNamespace(),
        emit_event=emit_event,
        emit_preview=None,
        tool_events=[],
        custom_names=set(),
        custom_specs={},
        mcp_tool_names=set(),
        interrupt_on={},
    )

    uses = [event for event in durable if event["type"] == "agent.tool_use"]
    assert [(event["name"], event["input"]) for event in uses] == [
        ("read", {"file_path": "/skills/example/SKILL.md"}),
        ("bash", {"command": "pwd"}),
        ("bash", {"command": "cat /workspace/vma-e2e-marker.txt"}),
    ]
    # The public tool-use event carries no provider-internal call id.
    assert all("tool_use_id" not in event for event in uses)
    # Each result points at the id of the tool-use *event* it completes, which
    # is what the Managed Agents contract defines `tool_use_id` to be.
    results = [event for event in durable if event["type"] == "agent.tool_result"]
    assert [event["tool_use_id"] for event in results] == [
        event["_event_id"] for event in uses
    ]
    assert result["final_text"] == "done"


def _patch_runtime(monkeypatch, model: _ScriptedModel, saver: InMemorySaver):
    import app.runtime.deepagents_engine as engine

    provider = RuntimeProviderConfig(
        provider="fake",
        model_id="fake-model",
        adapter="fake",
        api_key=None,
        base_url=None,
        capabilities=RuntimeProviderCapabilities(tool_calls=True),
    )
    monkeypatch.setattr(engine, "resolve_runtime_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(engine, "build_chat_model", lambda _provider: model)

    @asynccontextmanager
    async def fake_backend(**kwargs):
        plan = SandboxRuntimePlan(
            enabled=True,
            backend="langgraph_state",
            supports_execute=False,
            policy_enforced=False,
            summary={"enabled": True, "backend": "langgraph_state"},
        )
        yield BackendHandle(backend=StateBackend(), plan=plan)

    @asynccontextmanager
    async def fake_saver():
        yield saver

    monkeypatch.setattr(engine, "open_backend", fake_backend)
    monkeypatch.setattr(engine, "checkpoint_saver", fake_saver)


async def test_deepagents_engine_streams_and_persists_exact_message_id(monkeypatch):
    model = _ScriptedModel(responses=[AIMessage(content="hello from deep agents")])
    _patch_runtime(monkeypatch, model, InMemorySaver())
    durable = []
    previews = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    async def emit_preview(payload):
        previews.append(dict(payload))

    result = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context={
            "organization_id": "org_test",
            "session_id": "sess_test",
            "checkpoint_thread_id": "thread_private",
        },
        emit_event=emit_event,
        emit_preview=emit_preview,
    )

    message = next(event for event in durable if event["type"] == "agent.message")
    assert result.final_text == "hello from deep agents"
    assert result.events_persisted is True
    assert previews[0] == {
        "type": "event_start",
        "event": {"type": "agent.message", "id": message["_event_id"]},
    }
    assert "hello from deep agents" in previews[1]["delta"]["content"]["text"]
    assert result.run_state["last_input_event_seq"] == 1


async def test_completed_checkpoint_recovers_without_second_model_call(monkeypatch):
    model = _ScriptedModel(responses=[AIMessage(content="checkpointed answer")])
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    admissions = 0
    recoveries = 0

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def begin_recovery():
        nonlocal recoveries
        recoveries += 1

    async def crash_before_control_plane_journal(payload):
        if payload["type"] == "agent.message":
            raise RuntimeError("simulated crash after completed graph checkpoint")
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_completed_recovery",
        "work_id": "work_stable_turn",
    }
    try:
        await execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="hello")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=crash_before_control_plane_journal,
            admit_execution=admit_execution,
            begin_recovery=begin_recovery,
        )
    except RuntimeError as exc:
        assert "simulated crash" in str(exc)
    else:
        raise AssertionError("the simulated crash did not fire")

    durable = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    recovered = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )

    assert model._index == 1
    assert admissions == 1
    assert recoveries == 1
    assert recovered.final_text == "checkpointed answer"
    assert recovered.run_state["last_input_event_seq"] == 1
    assert [event["type"] for event in durable] == ["agent.message"]


async def test_multi_call_usage_matches_completed_checkpoint_recovery(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todos",
                        name="write_todos",
                        args={"todos": [{"content": "Verify recovery", "status": "in_progress"}]},
                    )
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ),
            AIMessage(
                content="usage-stable answer",
                usage_metadata={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
            ),
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    admissions = 0

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def crash_after_checkpoint(payload):
        if payload["type"] == "agent.message":
            raise RuntimeError("crash after multi-call completion")
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_multi_call_usage",
        "work_id": "work_multi_call_usage",
    }
    try:
        await execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="track two calls")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=crash_after_checkpoint,
            admit_execution=admit_execution,
        )
    except RuntimeError as exc:
        assert "multi-call completion" in str(exc)
    else:
        raise AssertionError("the simulated multi-call crash did not fire")

    async def emit_recovered(payload):
        return payload["_event_id"]

    recovered = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="track two calls")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_recovered,
        admit_execution=admit_execution,
    )

    assert model._index == 2
    assert admissions == 1
    assert recovered.final_text == "usage-stable answer"
    assert recovered.usage == {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}


async def test_subagent_usage_matches_completed_checkpoint_recovery(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_task",
                        name="task",
                        args={
                            "description": "Return the delegated answer.",
                            "subagent_type": "general-purpose",
                        },
                    )
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ),
            AIMessage(
                content="delegated answer",
                usage_metadata={"input_tokens": 6, "output_tokens": 7, "total_tokens": 13},
            ),
            AIMessage(
                content="coordinator answer",
                usage_metadata={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
            ),
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    version = _version(multiagent={"type": "coordinator", "agents": []})
    admissions = 0

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def crash_after_checkpoint(payload):
        if payload["type"] == "agent.message":
            raise RuntimeError("crash after subagent completion")
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_subagent_usage",
        "work_id": "work_subagent_usage",
    }
    try:
        await execute_deep_agent(
            version,
            [_event(1, "user.message", content="delegate once")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=crash_after_checkpoint,
            admit_execution=admit_execution,
        )
    except RuntimeError as exc:
        assert "subagent completion" in str(exc)
    else:
        raise AssertionError("the simulated subagent crash did not fire")

    async def emit_recovered(payload):
        return payload["_event_id"]

    recovered = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="delegate once")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_recovered,
        admit_execution=admit_execution,
    )

    assert model._index == 3
    assert admissions == 1
    assert recovered.final_text == "coordinator answer"
    assert recovered.usage == {"input_tokens": 11, "output_tokens": 14, "total_tokens": 25}


async def test_subagent_usage_is_checkpointed_before_next_root_model(monkeypatch):
    import app.runtime.deepagent_tools as deepagent_tools

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_task_midturn",
                        name="task",
                        args={
                            "description": "Return the delegated answer.",
                            "subagent_type": "general-purpose",
                        },
                    )
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ),
            AIMessage(
                content="delegated answer",
                usage_metadata={"input_tokens": 6, "output_tokens": 7, "total_tokens": 13},
            ),
            AIMessage(
                content="coordinator recovered",
                usage_metadata={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
            ),
        ],
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    version = _version(multiagent={"type": "coordinator", "agents": []})
    admissions = 0
    crashed = False
    original_wrap_model_call = deepagent_tools.ToolFilterMiddleware.awrap_model_call

    async def crash_before_root_followup(self, request, handler):
        nonlocal crashed
        if not crashed and any(
            isinstance(message, ToolMessage) and message.name == "task"
            for message in request.messages
        ):
            crashed = True
            raise _SimulatedModelCrash("simulated worker loss before root follow-up")
        return await original_wrap_model_call(self, request, handler)

    monkeypatch.setattr(
        deepagent_tools.ToolFilterMiddleware,
        "awrap_model_call",
        crash_before_root_followup,
    )

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def emit_event(payload):
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_midturn_subagent",
        "checkpoint_thread_id": "thread_midturn_subagent",
        "work_id": "work_midturn_subagent",
    }
    try:
        await execute_deep_agent(
            version,
            [_event(1, "user.message", content="delegate and recover")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=emit_event,
            admit_execution=admit_execution,
        )
    except _SimulatedModelCrash:
        pass
    else:
        raise AssertionError("the simulated mid-turn crash did not fire")

    checkpoint = await saver.aget_tuple(
        {"configurable": {"thread_id": "thread_midturn_subagent"}}
    )
    assert checkpoint is not None
    evidence = checkpoint.checkpoint["channel_values"]["vma_turn_evidence"]
    assert sum(record["usage"].get("total_tokens", 0) for record in evidence["records"].values()) == 16

    recovered = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="delegate and recover")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
        admit_execution=admit_execution,
    )

    assert model._index == 3
    assert admissions == 2
    assert recovered.final_text == "coordinator recovered"
    assert recovered.usage == {"input_tokens": 11, "output_tokens": 14, "total_tokens": 25}


async def test_parallel_subagents_merge_enriched_model_evidence(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                id="response_parallel_dispatch",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_task_a",
                        name="task",
                        args={
                            "description": "Complete delegated task A.",
                            "subagent_type": "general-purpose",
                        },
                    ),
                    ToolCall(
                        id="call_task_b",
                        name="task",
                        args={
                            "description": "Complete delegated task B.",
                            "subagent_type": "general-purpose",
                        },
                    ),
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            ),
            AIMessage(
                id="response_subagent_action_a",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todos_a",
                        name="write_todos",
                        args={
                            "todos": [
                                {"content": "Complete task A", "status": "in_progress"}
                            ]
                        },
                    )
                ],
                usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            ),
            AIMessage(
                id="response_subagent_action_b",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todos_b",
                        name="write_todos",
                        args={
                            "todos": [
                                {"content": "Complete task B", "status": "in_progress"}
                            ]
                        },
                    )
                ],
                usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            ),
            AIMessage(
                id="response_subagent_final_a",
                content="delegated result A",
                usage_metadata={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
            ),
            AIMessage(
                id="response_subagent_final_b",
                content="delegated result B",
                usage_metadata={"input_tokens": 6, "output_tokens": 7, "total_tokens": 13},
            ),
            AIMessage(
                id="response_parallel_final",
                content="parallel coordinator answer",
                usage_metadata={"input_tokens": 8, "output_tokens": 9, "total_tokens": 17},
            ),
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_parallel_subagents",
        "checkpoint_thread_id": "thread_parallel_subagents",
        "work_id": "work_parallel_subagents",
    }

    async def emit_event(payload):
        return payload["_event_id"]

    result = await execute_deep_agent(
        _version(multiagent={"type": "coordinator", "agents": []}),
        [_event(1, "user.message", content="delegate two tasks in parallel")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
    )

    checkpoint = await saver.aget_tuple(
        {"configurable": {"thread_id": "thread_parallel_subagents"}}
    )
    assert checkpoint is not None
    evidence = checkpoint.checkpoint["channel_values"]["vma_turn_evidence"]
    records = evidence["records"]
    assert records["response_subagent_action_a"]["tool_calls"] == [
        {
            "id": "call_todos_a",
            "name": "write_todos",
            "args": {"todos": [{"content": "Complete task A", "status": "in_progress"}]},
        }
    ]
    assert records["response_subagent_action_b"]["tool_calls"] == [
        {
            "id": "call_todos_b",
            "name": "write_todos",
            "args": {"todos": [{"content": "Complete task B", "status": "in_progress"}]},
        }
    ]
    assert result.final_text == "parallel coordinator answer"
    assert result.usage == {"input_tokens": 25, "output_tokens": 31, "total_tokens": 56}


async def test_final_completion_node_resumes_without_second_model_call(monkeypatch):
    import app.runtime.deepagents_engine as engine

    model = _ScriptedModel(responses=[AIMessage(content="final node answer")])
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    admissions = 0
    recoveries = 0
    completion_calls = 0
    original_after_agent = engine.VmaTurnCompletionMiddleware.aafter_agent

    async def fail_first_completion(self, state, runtime):
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise RuntimeError("simulated crash before completion marker")
        return await original_after_agent(self, state, runtime)

    monkeypatch.setattr(
        engine.VmaTurnCompletionMiddleware,
        "aafter_agent",
        fail_first_completion,
    )

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def begin_recovery():
        nonlocal recoveries
        recoveries += 1

    async def emit_event(payload):
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_final_node_recovery",
        "work_id": "work_final_node",
    }
    try:
        await execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="hello")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=emit_event,
            admit_execution=admit_execution,
            begin_recovery=begin_recovery,
        )
    except RuntimeError as exc:
        assert "completion marker" in str(exc)
    else:
        raise AssertionError("the simulated final-node crash did not fire")

    recovered = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )

    assert model._index == 1
    assert admissions == 1
    assert recoveries == 1
    assert completion_calls == 2
    assert recovered.final_text == "final node answer"


async def test_interrupted_checkpoint_recovers_pending_action_without_second_model_call(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[ToolCall(id="call_lookup", name="lookup", args={"case_id": "42"})],
            )
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    admissions = 0
    recoveries = 0
    durable = []

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def begin_recovery():
        nonlocal recoveries
        recoveries += 1

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    version = _version(
        tools=[
            {
                "type": "custom",
                "name": "lookup",
                "description": "Look up a case.",
                "input_schema": {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}},
                    "required": ["case_id"],
                },
            }
        ]
    )
    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_interrupt_recovery",
        "work_id": "work_interrupt_recovery",
    }
    first = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="look up 42")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )
    recovered = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="look up 42")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )

    assert model._index == 1
    assert admissions == 1
    assert recoveries == 1
    assert recovered.requires_action is True
    assert recovered.blocking_event_ids == first.blocking_event_ids
    assert len({event["_event_id"] for event in durable if event["type"] == "agent.custom_tool_use"}) == 1


async def test_custom_tool_interrupt_resumes_with_client_result(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[ToolCall(id="call_lookup", name="lookup", args={"case_id": "42"})],
            ),
            AIMessage(content="case 42 is resolved"),
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    durable = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    version = _version(
        tools=[
            {
                "type": "custom",
                "name": "lookup",
                "description": "Look up a case.",
                "input_schema": {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}},
                    "required": ["case_id"],
                },
            }
        ]
    )
    first = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="look up 42")],
        {"type": "cloud"},
        runtime_context={
            "organization_id": "org_test",
            "session_id": "sess_test",
            "checkpoint_thread_id": "thread_custom",
        },
        emit_event=emit_event,
    )
    assert first.requires_action is True
    assert len(first.blocking_event_ids) == 1
    use_id = first.blocking_event_ids[0]
    assert next(event for event in durable if event["_event_id"] == use_id)["type"] == "agent.custom_tool_use"

    second = await execute_deep_agent(
        version,
        [
            _event(1, "user.message", content="look up 42"),
            _event(
                2,
                "user.custom_tool_result",
                custom_tool_use_id=use_id,
                content=[{"type": "text", "text": "resolved"}],
            ),
        ],
        {"type": "cloud"},
        runtime_context={
            "organization_id": "org_test",
            "session_id": "sess_test",
            "checkpoint_thread_id": "thread_custom",
            "previous_run_state": first.run_state,
        },
        emit_event=emit_event,
    )
    assert second.requires_action is False
    assert second.final_text == "case 42 is resolved"
    assert second.run_state["pending_actions"] == []


async def test_interrupted_checkpoint_recovers_action_without_second_model_call(monkeypatch):
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[ToolCall(id="call_recover", name="lookup", args={"case_id": "9"})],
            )
        ]
    )
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)
    version = _version(
        tools=[
            {
                "type": "custom",
                "name": "lookup",
                "description": "Look up a case.",
                "input_schema": {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}},
                    "required": ["case_id"],
                },
            }
        ]
    )
    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_test",
        "checkpoint_thread_id": "thread_interrupt_recovery",
        "work_id": "work_interrupt_recovery",
    }
    admissions = 0
    recoveries = 0
    first_events = []
    recovered_events = []

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def begin_recovery():
        nonlocal recoveries
        recoveries += 1

    async def emit_first(payload):
        first_events.append(dict(payload))
        return payload["_event_id"]

    first = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="look up 9")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_first,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )
    assert first.requires_action is True

    async def emit_recovered(payload):
        recovered_events.append(dict(payload))
        return payload["_event_id"]

    recovered = await execute_deep_agent(
        version,
        [_event(1, "user.message", content="look up 9")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_recovered,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )

    assert model._index == 1
    assert admissions == 1
    assert recoveries == 1
    assert recovered.requires_action is True
    assert recovered.blocking_event_ids == first.blocking_event_ids
    first_use = next(event for event in first_events if event["type"] == "agent.custom_tool_use")
    recovered_use = next(
        event for event in recovered_events if event["type"] == "agent.custom_tool_use"
    )
    assert recovered_use["_event_id"] == first_use["_event_id"]


def test_resume_input_waits_until_every_pending_action_is_present():
    previous = {
        "pending_actions": [
            {"event_id": "evt_a", "interrupt_id": "int_1", "action_index": 0},
            {"event_id": "evt_b", "interrupt_id": "int_1", "action_index": 1},
        ]
    }
    graph_input, _seq = _graph_input(
        [_event(3, "user.tool_confirmation", tool_use_id="evt_a", result="allow")],
        previous,
    )
    assert graph_input is None


def test_skill_archive_rejects_path_traversal():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../SKILL.md", "unsafe")
    try:
        sandbox_input_bundle(
            {
                "skill_archives": [
                    {
                        "skill_id": "skill_unsafe",
                        "version": 1,
                        "archive": buffer.getvalue(),
                    }
                ]
            }
        )
    except Exception as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe skill archive was accepted")


async def test_runner_persists_engine_events_once_with_reserved_id(client, monkeypatch):
    from app.runtime.contracts import RuntimeResult
    from app.runtime.sandbox_outputs import DiscoveredSandboxOutput
    from tests.conftest import TEST_HEADERS

    async def fake_execute(
        version,
        history,
        environment_config,
        *,
        emit_event,
        emit_preview,
        **kwargs,
    ):
        admit_execution = kwargs.get("admit_execution")
        if admit_execution is not None:
            await admit_execution()
        await emit_preview(
            {"type": "event_start", "event": {"type": "agent.message", "id": "evt_reserved"}}
        )
        event_id = await emit_event(
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": "engine result"}],
                "_event_id": "evt_reserved",
            }
        )
        return RuntimeResult(
            final_text="engine result",
            events_persisted=True,
            run_state={"backend": "deepagents", "last_input_event_seq": history[-1].seq},
            blocking_event_ids=[],
            sandbox_outputs=[
                DiscoveredSandboxOutput(
                    path="/mnt/session/outputs/runner-result.txt",
                    content=b"durable generated result",
                    mime_type="text/plain",
                )
            ],
        )

    monkeypatch.setattr("app.runtime.deepagents_engine.execute_deep_agent", fake_execute)

    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Runner Bridge", "model": {"id": "fake", "provider": "fake"}},
    )
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "runner-bridge", "config": {"type": "cloud"}},
    )
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent_response.json()["id"],
            "environment_id": environment_response.json()["id"],
        },
    )
    session_id = session_response.json()["id"]
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "run"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/sessions/{session_id}/events", headers=TEST_HEADERS)
    messages = [event for event in response.json()["data"] if event["type"] == "agent.message"]
    assert len(messages) == 1

    files = await client.get(
        "/v1/files",
        headers=TEST_HEADERS,
        params={"scope_id": session_id},
    )
    assert files.status_code == 200, files.text
    generated = next(
        item for item in files.json()["data"] if item.get("filename") == "runner-result.txt"
    )
    assert generated["downloadable"] is True
    downloaded = await client.get(
        f"/v1/files/{generated['id']}/content",
        headers=TEST_HEADERS,
    )
    assert downloaded.content == b"durable generated result"
    assert messages[0]["id"] == "evt_reserved"
    assert messages[0]["content"][0]["text"] == "engine result"
