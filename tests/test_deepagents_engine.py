from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from deepagents.backends import StateBackend
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, ToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, PrivateAttr

from app.runtime.contracts import EffectiveAgentVersion
from app.runtime.deepagents_engine import _completed_tool_calls, _graph_input, _stream_graph, execute_deep_agent
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


class _StreamGraph:
    def __init__(self, messages):
        self.messages = messages

    async def astream(self, *_args, **_kwargs):
        for message in self.messages:
            yield (), "messages", (message, {})


def _version(*, tools=None) -> EffectiveAgentVersion:
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
        multiagent=None,
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
    assert [
        (event["name"], event["tool_use_id"], event["input"])
        for event in uses
    ] == [
        ("read", "call_read", {"file_path": "/skills/example/SKILL.md"}),
        ("bash", "call_pwd", {"command": "pwd"}),
        (
            "bash",
            "call_marker",
            {"command": "cat /workspace/vma-e2e-marker.txt"},
        ),
    ]
    results = [event for event in durable if event["type"] == "agent.tool_result"]
    assert [event["tool_use_id"] for event in results] == [
        "call_read",
        "call_pwd",
        "call_marker",
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
            "workspace_id": "wrkspc_test",
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
            "workspace_id": "wrkspc_test",
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
            "workspace_id": "wrkspc_test",
            "session_id": "sess_test",
            "checkpoint_thread_id": "thread_custom",
            "previous_run_state": first.run_state,
        },
        emit_event=emit_event,
    )
    assert second.requires_action is False
    assert second.final_text == "case 42 is resolved"
    assert second.run_state["pending_actions"] == []


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
            run_state={"backend": "deepagents"},
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
