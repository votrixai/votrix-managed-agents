from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from deepagents.backends import StateBackend

from app.db.engine import session_scope
from app.db.queries import events as events_q
from app.db.queries import resources as resources_q
from app.db.queries import sessions as sessions_q
from app.runtime.contracts import RuntimeResult
from app.runtime.deepagents_engine import (
    DeepAgentsRuntimeError,
    execute_deep_agent,
    recover_completed_deep_agent_turn,
)
from app.runtime.work_queue import (
    execute_work_item,
    heartbeat_work,
    lease_next_work_for_worker,
    stop_work,
)
from app.runtime.sandbox import BackendHandle, SandboxRuntimePlan
from tests.conftest import TEST_HEADERS
from tests.test_deepagents_engine import _ScriptedModel, _event, _patch_runtime, _version


async def _create_queue_subject(client, *, enqueue_input: bool = True):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Stage A Edge Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()

    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "Stage A Edge Worker", "config": {"type": "self_hosted"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    environment = environment_response.json()

    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    managed_session = session_response.json()

    if enqueue_input:
        event_response = await client.post(
            f"/v1/sessions/{managed_session['id']}/events",
            headers=TEST_HEADERS,
            json={"events": [{"type": "user.message", "content": "run edge case"}]},
        )
        assert event_response.status_code == 200, event_response.text

    return agent, environment, managed_session


async def _lease_queued_work(environment_id: str, worker_id: str):
    async with session_scope() as db:
        work = await lease_next_work_for_worker(
            db,
            environment_id=environment_id,
            worker_id=worker_id,
            lease_seconds=30,
        )
        assert work is not None
        work_id = work.id
        lease = dict((work.data or {}).get("lease") or {})
        await db.commit()
    return work_id, lease


async def test_completed_checkpoint_recovery_skips_external_runtime_setup(monkeypatch):
    import app.runtime.deepagents_engine as engine

    model = _ScriptedModel(responses=[AIMessage(content="durable answer")])
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)

    async def crash_after_checkpoint(payload):
        if payload["type"] == "agent.message":
            raise RuntimeError("simulated control-plane crash")
        return payload["_event_id"]

    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_stage_a_recovery",
        "checkpoint_thread_id": "thread_stage_a_recovery",
        "work_id": "work_stage_a_recovery",
    }
    with pytest.raises(RuntimeError, match="control-plane crash"):
        await execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="hello")],
            {"type": "cloud"},
            runtime_context=runtime_context,
            emit_event=crash_after_checkpoint,
        )

    def external_setup_must_not_run(*_args, **_kwargs):
        raise AssertionError("completed recovery attempted external runtime setup")

    async def async_external_setup_must_not_run(*_args, **_kwargs):
        raise AssertionError("completed recovery attempted MCP setup")

    monkeypatch.setattr(engine, "resolve_runtime_provider", external_setup_must_not_run)
    monkeypatch.setattr(engine, "open_backend", external_setup_must_not_run)
    monkeypatch.setattr(engine, "_load_mcp_tools", async_external_setup_must_not_run)

    durable_events: list[dict] = []

    async def emit_event(payload):
        durable_events.append(dict(payload))
        return payload["_event_id"]

    recovered = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context=runtime_context,
        emit_event=emit_event,
    )

    assert model._index == 1
    assert recovered.final_text == "durable answer"
    assert [event["type"] for event in durable_events] == ["agent.message"]


async def test_runner_checkpoint_recovery_skips_runtime_context_construction(client, monkeypatch):
    import app.runtime.deepagents_engine as engine
    import app.runtime.runner as runner

    _agent, environment, _managed_session = await _create_queue_subject(client)
    work_id, lease = await _lease_queued_work(environment["id"], "recovery-worker")

    async def recover_from_checkpoint(
        _version,
        history,
        _previous_state,
        *,
        emit_event,
        begin_recovery,
        **_kwargs,
    ):
        await begin_recovery()
        await emit_event(
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": "recovered without setup"}],
                "_event_id": "evt_stage_a_fast_recovery",
            }
        )
        input_seq = max(event.seq for event in history if event.type == "user.message")
        return RuntimeResult(
            final_text="recovered without setup",
            events_persisted=True,
            run_state={
                "backend": "deepagents",
                "last_input_event_seq": input_seq,
            },
            sandbox_state={"runtime_backend": "deepagents"},
        )

    async def runtime_setup_must_not_run(*_args, **_kwargs):
        raise AssertionError("checkpoint recovery constructed runtime context")

    monkeypatch.setattr(engine, "recover_completed_deep_agent_turn", recover_from_checkpoint)
    monkeypatch.setattr(runner, "_runtime_context_for_session", runtime_setup_must_not_run)
    monkeypatch.setattr(runner, "_execute", runtime_setup_must_not_run)

    result = await execute_work_item(
        work_id,
        worker_id="recovery-worker",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
        lease_seconds=30,
    )
    assert result == "completed"


async def test_corrupt_checkpoint_marker_phase_fails_closed(monkeypatch):
    import app.runtime.deepagents_engine as engine

    checkpoint_tuple = SimpleNamespace(
        checkpoint={
            "channel_values": {
                "vma_turn_marker": {
                    "version": 1,
                    "work_id": "work_corrupt_marker",
                    "input_seq": 1,
                    "agent_version_id": "agtv_test",
                    "phase": "mystery",
                }
            }
        },
        pending_writes=(),
    )

    class CorruptMarkerSaver:
        async def aget_tuple(self, _config):
            return checkpoint_tuple

    @asynccontextmanager
    async def corrupt_marker_saver():
        yield CorruptMarkerSaver()

    monkeypatch.setattr(engine, "checkpoint_saver", corrupt_marker_saver)

    with pytest.raises(DeepAgentsRuntimeError, match="phase"):
        await recover_completed_deep_agent_turn(
            _version(),
            [_event(1, "user.message", content="hello")],
            {},
            thread_id="thread_corrupt_marker",
            work_id="work_corrupt_marker",
        )


async def test_same_input_checkpoint_marker_conflict_fails_closed(monkeypatch):
    import app.runtime.deepagents_engine as engine

    checkpoint_tuple = SimpleNamespace(
        checkpoint={
            "channel_values": {
                "vma_turn_marker": {
                    "version": 1,
                    "work_id": "work_other",
                    "input_seq": 1,
                    "agent_version_id": "agtv_test",
                    "phase": "completed",
                }
            }
        },
        pending_writes=(),
    )

    class ConflictingMarkerSaver:
        async def aget_tuple(self, _config):
            return checkpoint_tuple

    @asynccontextmanager
    async def conflicting_marker_saver():
        yield ConflictingMarkerSaver()

    monkeypatch.setattr(engine, "checkpoint_saver", conflicting_marker_saver)

    with pytest.raises(DeepAgentsRuntimeError, match="conflicts"):
        await recover_completed_deep_agent_turn(
            _version(),
            [_event(1, "user.message", content="hello")],
            {},
            thread_id="thread_conflicting_marker",
            work_id="work_current",
        )


async def test_final_node_recovery_skips_failed_e2b_output_rediscovery(monkeypatch):
    import app.runtime.deepagents_engine as engine
    import app.runtime.sandbox_lifecycle as sandbox_lifecycle

    model = _ScriptedModel(responses=[AIMessage(content="recovered E2B answer")])
    saver = InMemorySaver()
    _patch_runtime(monkeypatch, model, saver)

    @asynccontextmanager
    async def fake_e2b_backend(**_kwargs):
        plan = SandboxRuntimePlan(
            enabled=True,
            backend="e2b",
            supports_execute=False,
            policy_enforced=True,
            summary={"enabled": True, "backend": "e2b"},
        )
        yield BackendHandle(backend=StateBackend(), plan=plan, connection=object())

    class UnavailableOutputProvider:
        async def discover_outputs(self, *_args, **_kwargs):
            raise RuntimeError("sandbox output scan unavailable")

    monkeypatch.setattr(engine, "open_backend", fake_e2b_backend)
    monkeypatch.setattr(
        sandbox_lifecycle,
        "build_e2b_provider",
        lambda: UnavailableOutputProvider(),
    )
    original_after_agent = engine.VmaTurnCompletionMiddleware.aafter_agent
    completion_calls = 0

    async def fail_first_completion(self, state, runtime):
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise RuntimeError("crash before E2B completion marker")
        return await original_after_agent(self, state, runtime)

    monkeypatch.setattr(
        engine.VmaTurnCompletionMiddleware,
        "aafter_agent",
        fail_first_completion,
    )
    runtime_context = {
        "organization_id": "org_test",
        "session_id": "sess_e2b_recovery",
        "checkpoint_thread_id": "thread_e2b_recovery",
        "work_id": "work_e2b_recovery",
    }

    with pytest.raises(RuntimeError, match="E2B completion marker"):
        await execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="hello")],
            {"type": "cloud"},
            runtime_context=runtime_context,
        )

    recovered = await execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context=runtime_context,
    )

    assert model._index == 1
    assert completion_calls == 2
    assert recovered.final_text == "recovered E2B answer"
    assert recovered.sandbox_outputs == []
    assert recovered.run_state["warnings"] == [
        {
            "type": "sandbox_output_rediscovery_skipped",
            "message": "Recovery skipped unavailable bounded sandbox output discovery",
        }
    ]


async def test_internal_heartbeat_preserves_leased_status_and_attempt(client):
    _agent, environment, _managed_session = await _create_queue_subject(client)
    work_id, lease = await _lease_queued_work(environment["id"], "heartbeat-worker")

    async with session_scope() as db:
        work = await resources_q.get_work_item_for_worker(db, work_id)
        assert work is not None
        assert work.status == "leased"
        assert int((work.data or {}).get("attempt") or 0) == 0

        await heartbeat_work(
            db,
            work,
            worker_id="heartbeat-worker",
            lease_id=str(lease["lease_id"]),
            lease_seconds=30,
            payload={"type": "execution"},
            preserve_status=True,
        )
        await db.commit()

    async with session_scope() as db:
        stored = await resources_q.get_work_item_for_worker(db, work_id)
    assert stored is not None
    assert stored.status == "leased"
    assert int((stored.data or {}).get("attempt") or 0) == 0
    assert stored.data["last_heartbeat"] == {"type": "execution"}


async def test_concurrent_stop_is_not_requeued_by_deferred_cleanup(client, monkeypatch):
    _agent, environment, _managed_session = await _create_queue_subject(client)
    work_id, lease = await _lease_queued_work(environment["id"], "stop-race-worker")

    async def stop_during_execution(_session_id, *, work_lease, **_kwargs):
        async with session_scope() as db:
            work = await resources_q.get_work_item_for_worker(db, work_lease.work_id)
            assert work is not None
            await stop_work(db, work, payload={"reason": "operator cancelled"})
            await db.commit()
        return False

    monkeypatch.setattr("app.runtime.runner.run_session_turn", stop_during_execution)
    outcome = await execute_work_item(
        work_id,
        worker_id="stop-race-worker",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
        lease_seconds=30,
    )
    assert outcome == "stopped"

    async with session_scope() as db:
        stored = await resources_q.get_work_item_for_worker(db, work_id)
    assert stored is not None
    assert stored.status == "stopped"
    assert stored.data["stop"] == {"reason": "operator cancelled"}


async def test_malformed_turn_journal_terminates_session(client):
    _agent, environment, managed_session = await _create_queue_subject(client)

    async with session_scope() as db:
        queued = await lease_next_work_for_worker(
            db,
            environment_id=environment["id"],
            worker_id="malformed-journal-worker",
            lease_seconds=30,
        )
        assert queued is not None
        work_id = queued.id
        data = dict(queued.data or {})
        lease = dict(data.get("lease") or {})
        data["turn_journal"] = {"version": 1}
        await resources_q.update_resource(db, queued, data=data, status=queued.status)
        await db.commit()

    result = await execute_work_item(
        work_id,
        worker_id="malformed-journal-worker",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
        lease_seconds=30,
    )
    assert result == "error"

    async with session_scope() as db:
        stored_session = await sessions_q.get_session(
            db,
            managed_session["id"],
            organization_id="org_test",
        )
        stored_work = await resources_q.get_work_item_for_worker(db, work_id)
        events = await events_q.list_events(
            db,
            session_id=managed_session["id"],
            organization_id="org_test",
            limit=100,
        )
    assert stored_session is not None
    assert stored_session.status == "terminated"
    assert stored_session.stop_reason == {"type": "error"}
    assert stored_work is not None
    assert stored_work.status == "error"
    assert any(event.type == "session.error" for event in events)
    assert any(event.type == "session.status_terminated" for event in events)


async def test_no_input_work_completes_without_consuming_attempt(client, monkeypatch):
    import app.runtime.runner as runner

    _agent, environment, managed_session = await _create_queue_subject(
        client,
        enqueue_input=False,
    )
    async with session_scope() as db:
        work = await resources_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id=environment["id"],
            name=f"session:{managed_session['id']}",
            status="queued",
            data={
                "session_id": managed_session["id"],
                "organization_id": "org_test",
                "attempt": 0,
            },
            organization_id="org_test",
        )
        await db.commit()
        work_id = work.id

    async def no_input_result(_version, history, _environment_config, **_kwargs):
        assert not any(event.type == "user.message" for event in history)
        return RuntimeResult(
            run_state={
                "backend": "deepagents",
                "last_input_event_seq": 0,
                "_vma_noop": True,
            }
        )

    monkeypatch.setattr(runner, "_execute", no_input_result)

    result = await execute_work_item(work_id, worker_id="no-input-worker", lease_seconds=30)
    assert result == "completed"

    async with session_scope() as db:
        stored_work = await resources_q.get_work_item_for_worker(db, work_id)
        stored_session = await sessions_q.get_session(
            db,
            managed_session["id"],
            organization_id="org_test",
        )
    assert stored_work is not None
    assert stored_work.status == "completed"
    assert int((stored_work.data or {}).get("attempt") or 0) == 0
    assert stored_session is not None
    assert stored_session.status == "idle"


async def test_already_finalized_journal_clears_without_execution(client, monkeypatch):
    import app.runtime.runner as runner

    _agent, environment, managed_session = await _create_queue_subject(
        client,
        enqueue_input=False,
    )
    async with session_scope() as db:
        session = await sessions_q.get_session(
            db,
            managed_session["id"],
            organization_id="org_test",
            for_update=True,
        )
        assert session is not None
        session.run_state = {"backend": "deepagents", "last_input_event_seq": 7}
        work = await resources_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id=environment["id"],
            name=f"session:{managed_session['id']}",
            status="queued",
            data={
                "session_id": managed_session["id"],
                "attempt": 0,
                "turn_journal": {
                    "version": 1,
                    "input_seq": 7,
                    "agent_version_id": "already-finalized",
                },
            },
            organization_id="org_test",
        )
        await db.commit()
        work_id = work.id

    async def execution_must_not_run(*_args, **_kwargs):
        raise AssertionError("an already-finalized journal attempted execution")

    monkeypatch.setattr(runner, "_execute", execution_must_not_run)
    result = await execute_work_item(work_id, worker_id="stale-journal-worker")
    assert result == "completed"

    async with session_scope() as db:
        stored_work = await resources_q.get_work_item_for_worker(db, work_id)
    assert stored_work is not None
    assert stored_work.status == "completed"
    assert stored_work.data["attempt"] == 0
    assert "turn_journal" not in stored_work.data
