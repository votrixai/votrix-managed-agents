import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import api_keys as api_keys_q
from app.db.queries import events as events_q
from app.db.queries import governance as governance_q
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.governance import ACTIVE_GAUGE_WINDOW
from app.runtime.runner import _admit_graph_execution
from app.runtime.work_queue import WorkExecutionLease, execute_work_item, lease_next_work_for_worker
from tests.conftest import TEST_HEADERS, UNAUTHENTICATED_TEST_HEADERS


def test_turn_journal_round_trip_and_unknown_version_fail_closed():
    from app.runtime.runner import _runtime_result_from_turn_journal

    journal = {
        "version": 1,
        "input_seq": 9,
        "agent_version_id": "agtv_test",
        "run_state": {"backend": "deepagents", "last_input_event_seq": 9},
        "final_text": "journaled",
        "events_persisted": True,
        "tool_events": [],
        "requires_action": True,
        "blocking_event_ids": ["evt_action"],
        "usage": {"total_tokens": 7},
        "sandbox_state": {"runtime_backend": "deepagents"},
        "outputs_persisted": True,
    }
    result = _runtime_result_from_turn_journal(
        journal,
        expected_agent_version_id="agtv_test",
    )
    assert result.final_text == "journaled"
    assert result.events_persisted is True
    assert result.requires_action is True
    assert result.blocking_event_ids == ["evt_action"]
    assert result.usage == {"total_tokens": 7}
    assert result.run_state == {"backend": "deepagents", "last_input_event_seq": 9}
    assert result.sandbox_state == {"runtime_backend": "deepagents"}

    with pytest.raises(RuntimeError, match="version"):
        _runtime_result_from_turn_journal(
            {**journal, "version": 2},
            expected_agent_version_id="agtv_test",
        )

    invalid_fields = {
        "final_text": None,
        "events_persisted": "true",
        "tool_events": None,
        "requires_action": 1,
        "blocking_event_ids": "evt_action",
        "usage": None,
        "sandbox_state": None,
        "outputs_persisted": False,
    }
    for field, value in invalid_fields.items():
        with pytest.raises(RuntimeError):
            _runtime_result_from_turn_journal(
                {**journal, field: value},
                expected_agent_version_id="agtv_test",
            )


async def _create_agent(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Queue Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client, env_type: str):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{env_type}-queue", "config": {"type": env_type}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_session(client, agent, environment):
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _force_fourth_work_lease(
    *,
    environment_id: str,
    session_id: str,
) -> tuple[str, dict]:
    work_id = ""
    latest_lease: dict = {}
    for generation in range(1, 5):
        async with session_scope() as db:
            work = await lease_next_work_for_worker(
                db,
                environment_id=environment_id,
                worker_id=f"retry-worker-{generation}",
                lease_seconds=30,
            )
            assert work is not None
            assert int((work.data or {}).get("attempt") or 0) == generation - 1
            work_id = work.id
            latest_lease = dict((work.data or {}).get("lease") or {})
            await db.commit()
        if generation < 4:
            admitted = await _admit_graph_execution(
                session_id=session_id,
                organization_id="org_test",
                work_lease=WorkExecutionLease(
                    work_id=work_id,
                    worker_id=f"retry-worker-{generation}",
                    lease_id=str(latest_lease["lease_id"]),
                    generation=int(latest_lease["generation"]),
                    attempt=generation - 1,
                ),
            )
            assert admitted.attempt == generation
            async with session_scope() as db:
                work = await res_q.get_work_item_for_worker(db, work_id)
                assert work is not None
                data = dict(work.data or {})
                data["lease"] = {
                    **latest_lease,
                    "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
                }
                await res_q.update_resource(db, work, data=data, status="running")
                await db.commit()
    return work_id, latest_lease


async def test_inline_environment_queues_and_completes_work(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "cloud")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "run inline"}]},
    )
    assert response.status_code == 200, response.text

    for _ in range(20):
        response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
        assert response.status_code == 200, response.text
        stats = response.json()
        if stats["completed"] == 1:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"work did not complete; stats={stats}")


async def test_self_hosted_environment_leases_work_without_inline_execution(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "lease me"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 1

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    work = response.json()
    assert work["status"] == "leased"
    assert work["session_id"] == session["id"]
    assert work["lease"]["worker_id"] == "worker-1"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/ack",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_id": work["lease"]["lease_id"]},
    )
    assert response.status_code == 409
    assert "does not own" in response.json()["error"]["message"]

    # Ack is the one compatibility exception: Anthropic's generated SDK method
    # has no lease_id parameter.  Worker ownership is still checked, and an
    # explicitly supplied stale lease remains fenced.
    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/ack",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
        json={"progress": 0.25},
    )
    assert response.status_code == 409
    assert "lease_id is required" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "worker-1",
            "lease_id": work["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.5},
    )
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["progress"] == 0.5
    assert response.json()["state"] == "active"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "worker-2",
            "lease_id": work["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.9},
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/stop",
        headers=TEST_HEADERS,
        json={"reason": "test stop"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    assert response.json()["stop"]["reason"] == "test stop"


async def test_environment_work_update_only_accepts_metadata(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "metadata update"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    work = response.json()

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"status": "completed"},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": None},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": {"phase": "queued", "drop": "soon"}},
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["phase"] == "queued"
    assert metadata["drop"] == "soon"
    assert response.json()["status"] == "leased"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": {"drop": None}},
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["phase"] == "queued"
    assert "drop" not in metadata


async def test_expired_work_lease_can_be_recovered_by_next_worker(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "recover lease"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    first_lease = response.json()
    assert first_lease["attempt"] == 0

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=first_lease["id"],
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_seconds": 30},
    )

    assert response.status_code == 200, response.text
    recovered = response.json()
    assert recovered["id"] == first_lease["id"]
    assert recovered["status"] == "leased"
    assert recovered["attempt"] == 0
    assert recovered["lease"]["worker_id"] == "worker-2"


async def test_fourth_execution_attempt_exhausts_work_and_terminates_session(
    client,
    monkeypatch,
):
    monkeypatch.setenv("VMA_WORK_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "exhaust retries"}]},
    )
    assert response.status_code == 200, response.text

    work_id, lease = await _force_fourth_work_lease(
        environment_id=environment["id"],
        session_id=session["id"],
    )

    async def fail_if_graph_executes(session_id, *, organization_id, work_lease, **_kwargs):
        await _admit_graph_execution(
            session_id=session_id,
            organization_id=organization_id,
            work_lease=work_lease,
        )
        raise AssertionError("exhausted work must not execute the graph")

    monkeypatch.setattr("app.runtime.runner.run_session_turn", fail_if_graph_executes)
    result = await execute_work_item(
        work_id,
        worker_id="retry-worker-4",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
    )

    assert result == "exhausted"
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        stored_session = await sessions_q.get_session(
            db,
            session["id"],
            organization_id="org_test",
        )
        events = await events_q.list_events(
            db,
            session_id=session["id"],
            organization_id="org_test",
        )
        active_work = await governance_q.get_counter_value(
            db,
            organization_id="org_test",
            metric=governance_q.ACTIVE_WORK_METRIC,
            window_start=ACTIVE_GAUGE_WINDOW,
        )
        reservation = await governance_q.get_quota_reservation(
            db,
            organization_id="org_test",
            quota_name=governance_q.ACTIVE_WORK_METRIC,
            reference_id=work_id,
        )

    assert work is not None
    assert work.status == "error"
    assert work.data["attempt"] == 3
    assert work.data["error"] == {
        "type": "max_attempts_exceeded",
        "message": "Work item exceeded 3 execution attempts",
        "attempt": 4,
        "max_attempts": 3,
    }
    assert stored_session is not None
    assert stored_session.status == "terminated"
    assert stored_session.stop_reason == {
        "type": "error",
        "error_type": "max_attempts_exceeded",
        "attempt": 4,
        "max_attempts": 3,
        "work_id": work_id,
    }
    terminal_events = [
        event
        for event in events
        if event.type in {"session.error", "session.status_terminated"}
    ]
    assert [event.type for event in terminal_events] == [
        "session.error",
        "session.status_terminated",
    ]
    assert terminal_events[0].payload["error_type"] == "max_attempts_exceeded"
    assert terminal_events[0].payload["attempt"] == 4
    assert terminal_events[0].payload["max_attempts"] == 3
    assert terminal_events[1].payload["stop_reason"] == stored_session.stop_reason
    assert active_work == 0
    assert reservation is not None
    assert reservation.state == "released"


async def test_zero_max_attempts_allows_fourth_execution(client, monkeypatch):
    monkeypatch.setenv("VMA_WORK_MAX_ATTEMPTS", "0")
    get_settings.cache_clear()
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "disable retry cap"}]},
    )
    assert response.status_code == 200, response.text

    work_id, lease = await _force_fourth_work_lease(
        environment_id=environment["id"],
        session_id=session["id"],
    )
    executed: list[str] = []

    async def fake_run_session_turn(session_id, *, organization_id, work_lease, **_kwargs):
        await _admit_graph_execution(
            session_id=session_id,
            organization_id=organization_id,
            work_lease=work_lease,
        )
        executed.append(session_id)
        return True

    monkeypatch.setattr("app.runtime.runner.run_session_turn", fake_run_session_turn)
    result = await execute_work_item(
        work_id,
        worker_id="retry-worker-4",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
    )

    assert result == "completed"
    assert executed == [session["id"]]
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
    assert work is not None
    assert work.status == "completed"
    assert work.data["attempt"] == 4
    assert "error" not in work.data


async def test_deferred_work_does_not_consume_execution_attempts(client, monkeypatch):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "stay deferred"}]},
    )
    assert response.status_code == 200, response.text

    async def defer_without_admission(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.runtime.runner.run_session_turn", defer_without_admission)
    async with session_scope() as db:
        queued = await lease_next_work_for_worker(
            db,
            environment_id=environment["id"],
            worker_id="deferred-worker-0",
        )
        assert queued is not None
        work_id = queued.id
        initial_lease = dict((queued.data or {}).get("lease") or {})
        await db.commit()

    for index in range(3):
        result = await execute_work_item(
            work_id,
            worker_id=f"deferred-worker-{index}",
            lease_id=str(initial_lease["lease_id"]) if index == 0 else None,
            lease_generation=int(initial_lease["generation"]) if index == 0 else None,
        )
        assert result == "deferred"

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
    assert work is not None
    assert work.status == "queued"
    assert work.data["attempt"] == 0


async def test_admitted_failure_consumes_attempt_and_exposes_retry_identity(client, monkeypatch):
    import app.runtime.runner as runner

    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "fail after admission"}]},
    )
    assert response.status_code == 200, response.text

    async with session_scope() as db:
        work = (
            await res_q.list_resources(
                db,
                resource_type="environment_work",
                parent_id=environment["id"],
                limit=10,
            )
        )[0]
        work_id = work.id

    async def fail_after_admission(*_args, admit_execution, **_kwargs):
        await admit_execution()
        raise RuntimeError("provider failed after admission")

    monkeypatch.setattr(runner, "_execute", fail_after_admission)
    result = await execute_work_item(work_id, worker_id="admitted-failure-worker")
    assert result == "error"

    async with session_scope() as db:
        stored_work = await res_q.get_work_item_for_worker(db, work_id)
        events = await events_q.list_events(
            db,
            session_id=session["id"],
            organization_id="org_test",
            limit=100,
        )
    assert stored_work is not None
    assert stored_work.status == "error"
    assert stored_work.data["attempt"] == 1
    running = [event for event in events if event.type == "session.status_running"]
    assert len(running) == 1
    assert running[0].payload["attempt"] == 1
    assert running[0].payload["work_id"] == work_id
    assert running[0].payload["lease_generation"] == 1


async def test_turn_journal_recovers_without_invoking_model_runner(client, monkeypatch):
    from app.runtime.contracts import RuntimeResult
    import app.runtime.runner as runner

    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "journal recovery"}]},
    )
    assert response.status_code == 200, response.text

    async with session_scope() as db:
        leased = await lease_next_work_for_worker(
            db,
            environment_id=environment["id"],
            worker_id="journal-worker-1",
            lease_seconds=30,
        )
        assert leased is not None
        work_id = leased.id
        first_lease = dict((leased.data or {}).get("lease") or {})
        await db.commit()

    async def first_execution(
        _version,
        history,
        _environment_config,
        *,
        emit_event,
        admit_execution,
        **_kwargs,
    ):
        await admit_execution()
        await emit_event(
            {
                "type": "agent.message",
                "content": [{"type": "text", "text": "durable answer"}],
                "_event_id": "evt_journal_recovery",
            }
        )
        input_seq = max(event.seq for event in history if event.type == "user.message")
        return RuntimeResult(
            final_text="durable answer",
            events_persisted=True,
            run_state={
                "backend": "deepagents",
                "last_input_event_seq": input_seq,
            },
            sandbox_state={"runtime_backend": "deepagents"},
            usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )

    class SimulatedCrash(BaseException):
        pass

    async def crash_after_journal(**_kwargs):
        raise SimulatedCrash()

    original_record_usage = runner._record_model_usage_after_result
    monkeypatch.setattr(runner, "_execute", first_execution)
    monkeypatch.setattr(runner, "_record_model_usage_after_result", crash_after_journal)
    try:
        await execute_work_item(
            work_id,
            worker_id="journal-worker-1",
            lease_id=str(first_lease["lease_id"]),
            lease_generation=int(first_lease["generation"]),
            lease_seconds=30,
        )
    except SimulatedCrash:
        pass
    else:
        raise AssertionError("the simulated post-journal crash did not fire")

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        assert work is not None
        assert work.data["turn_journal"]["final_text"] == "durable answer"
        assert work.data["attempt"] == 1
        data = dict(work.data or {})
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    async with session_scope() as db:
        recovered = await lease_next_work_for_worker(
            db,
            environment_id=environment["id"],
            worker_id="journal-worker-2",
            lease_seconds=30,
        )
        assert recovered is not None
        second_lease = dict((recovered.data or {}).get("lease") or {})
        await db.commit()

    async def fail_if_model_runner_is_called(*_args, **_kwargs):
        raise AssertionError("journal recovery must not invoke the model runner")

    monkeypatch.setattr(runner, "_execute", fail_if_model_runner_is_called)
    monkeypatch.setattr(runner, "_record_model_usage_after_result", original_record_usage)
    result = await execute_work_item(
        work_id,
        worker_id="journal-worker-2",
        lease_id=str(second_lease["lease_id"]),
        lease_generation=int(second_lease["generation"]),
        lease_seconds=30,
    )
    assert result == "completed"

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        stored_session = await sessions_q.get_session(
            db,
            session["id"],
            organization_id="org_test",
        )
        events = await events_q.list_events(
            db,
            session_id=session["id"],
            organization_id="org_test",
            limit=100,
        )
    assert work is not None
    assert work.status == "completed"
    assert work.data["attempt"] == 1
    assert "turn_journal" not in work.data
    assert stored_session is not None
    assert stored_session.status == "idle"
    assert stored_session.run_state["last_input_event_seq"] > 0
    messages = [event for event in events if event.type == "agent.message"]
    assert len(messages) == 1
    assert messages[0].id == "evt_journal_recovery"


async def test_stale_lease_id_is_fenced_when_worker_id_is_reused(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "fence stale lease"}]},
    )
    assert response.status_code == 200, response.text

    first_response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "reused-worker", "lease_seconds": 30},
    )
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=first["id"],
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    second_response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "reused-worker", "lease_seconds": 30},
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["lease"]["lease_id"] != first["lease"]["lease_id"]
    assert second["lease"]["generation"] == first["lease"]["generation"] + 1

    stale_ack = await client.post(
        f"/v1/environments/{environment['id']}/work/{first['id']}/ack",
        headers=TEST_HEADERS,
        params={
            "worker_id": "reused-worker",
            "lease_id": first["lease"]["lease_id"],
        },
    )
    assert stale_ack.status_code == 409
    assert "current work lease generation" in stale_ack.json()["error"]["message"]

    stale = await client.post(
        f"/v1/environments/{environment['id']}/work/{first['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "reused-worker",
            "lease_id": first["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.9},
    )
    assert stale.status_code == 409
    assert "current work lease generation" in stale.json()["error"]["message"]


async def test_rescheduled_work_is_not_leased_until_retry_at(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    future_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    async with session_scope() as db:
        work = await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id=environment["id"],
            name=f"session:{session['id']}",
            status="rescheduling",
            data={"session_id": session["id"], "attempt": 1, "retry_at": future_retry_at},
        )
        await db.commit()
        work_id = work.id

    response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["rescheduling"] == 1

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    assert response.json() is None

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=work_id,
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await res_q.update_resource(db, work, data=data, status="rescheduling")
        await db.commit()

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    leased = response.json()
    assert leased["id"] == work_id
    assert leased["status"] == "leased"
    assert leased["lease"]["worker_id"] == "worker-2"


async def test_work_routes_require_database_api_key_worker_scope(
    client,
    database_api_key_factory,
):
    api_token = await database_api_key_factory(
        token="api-only-key",
        organization_id="org_test",
        scopes=(api_keys_q.API_SCOPE,),
    )
    worker_token = await database_api_key_factory(
        token="worker-only-key",
        organization_id="org_test",
        scopes=(api_keys_q.WORKER_SCOPE,),
    )

    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "secure lease"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=UNAUTHENTICATED_TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 401

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers={**TEST_HEADERS, "x-api-key": api_token},
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 403
    assert "worker" in response.json()["error"]["message"]

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers={**TEST_HEADERS, "x-api-key": worker_token},
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "leased"


async def test_trusted_worker_can_atomically_lease_another_organization_item():
    async with session_scope() as db:
        work = await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id="env_tenant_b",
            status="queued",
            data={"session_id": "sess_tenant_b", "organization_id": "org_tenant_b"},
            organization_id="org_tenant_b",
        )
        await db.commit()
        work_id = work.id

    async with session_scope() as db:
        leased = await lease_next_work_for_worker(
            db,
            environment_id=None,
            worker_id="global-worker",
        )
        await db.commit()

    assert leased is not None
    assert leased.id == work_id
    assert leased.organization_id == "org_tenant_b"
    assert leased.status == "leased"


async def test_work_execution_uses_row_organization_not_payload(monkeypatch):
    async with session_scope() as db:
        work = await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id="env_test",
            status="queued",
            data={
                "session_id": "sess_missing",
                "organization_id": "org_attacker",
            },
            organization_id="org_test",
        )
        await db.commit()
        work_id = work.id

    seen: dict[str, str] = {}

    async def fake_run_session_turn(session_id, *, organization_id, **_kwargs):
        seen["session_id"] = session_id
        seen["organization_id"] = organization_id
        return True

    monkeypatch.setattr("app.runtime.runner.run_session_turn", fake_run_session_turn)

    result = await execute_work_item(work_id, worker_id="trusted-worker")

    assert result == "completed"
    assert seen == {
        "session_id": "sess_missing",
        "organization_id": "org_test",
    }
