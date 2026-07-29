import pytest
from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import ManagedResource, SessionEvent, SessionEventIdempotency
from app.db.queries import events as events_q
from app.db.queries import sessions as sessions_q
from tests.conftest import TEST_HEADERS


async def _managed_session(client):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Idempotency Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "idempotency-worker", "config": {"type": "self_hosted"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent_response.json()["id"], "version": 1},
            "environment_id": environment_response.json()["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    return session_response.json()


async def _side_effect_counts(session_id: str) -> tuple[int, int, int]:
    async with session_scope() as db:
        event_count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(SessionEvent.session_id == session_id, SessionEvent.type == "user.message")
        )
        work_count = await db.scalar(
            select(func.count())
            .select_from(ManagedResource)
            .where(
                ManagedResource.resource_type == "environment_work",
                ManagedResource.name == f"session:{session_id}",
            )
        )
        idempotency_count = await db.scalar(
            select(func.count())
            .select_from(SessionEventIdempotency)
            .where(SessionEventIdempotency.session_id == session_id)
        )
    return int(event_count or 0), int(work_count or 0), int(idempotency_count or 0)


async def test_same_idempotency_key_replays_original_response_without_new_work(client):
    session = await _managed_session(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "turn-123"}
    payload = {"events": [{"type": "user.message", "content": "do this once"}]}

    first = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    replay = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert await _side_effect_counts(session["id"]) == (1, 1, 1)
    async with session_scope() as db:
        submission = await db.scalar(
            select(SessionEventIdempotency).where(SessionEventIdempotency.session_id == session["id"])
        )
    assert submission is not None
    assert submission.key_hash != "turn-123"
    assert submission.work_id is not None
    assert submission.response_body == first.json()
    async with session_scope() as db:
        work = await db.get(ManagedResource, submission.work_id)
    assert work is not None
    assert work.data["metadata"]["idempotency_key_hash"] == submission.key_hash


async def test_idempotency_key_cannot_be_reused_for_different_request(client):
    session = await _managed_session(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "turn-conflict"}

    first = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=headers,
        json={"events": [{"type": "user.message", "content": "first"}]},
    )
    conflict = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=headers,
        json={"events": [{"type": "user.message", "content": "second"}]},
    )

    assert first.status_code == 200, first.text
    assert conflict.status_code == 409, conflict.text
    assert "different request" in conflict.json()["error"]["message"]
    assert await _side_effect_counts(session["id"]) == (1, 1, 1)


async def test_replay_succeeds_after_session_state_changes(client):
    session = await _managed_session(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "turn-lost-response"}
    payload = {"events": [{"type": "user.message", "content": "accepted before state changed"}]}
    first = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    assert first.status_code == 200, first.text

    async with session_scope() as db:
        stored = await sessions_q.get_session(db, session["id"], for_update=True)
        assert stored is not None
        stored.status = "running"
        await db.commit()

    replay = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    new_turn = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers={**TEST_HEADERS, "Idempotency-Key": "turn-new"},
        json=payload,
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert new_turn.status_code == 409, new_turn.text
    assert await _side_effect_counts(session["id"]) == (1, 1, 1)


async def test_custom_tool_result_replays_after_required_action_is_resolved(client):
    session = await _managed_session(client)
    async with session_scope() as db:
        stored = await sessions_q.get_session(db, session["id"], for_update=True)
        assert stored is not None
        blocker = await events_q.append_event(
            db,
            stored,
            event_type="agent.custom_tool_use",
            payload={"type": "agent.custom_tool_use", "name": "lookup", "input": {}},
        )
        stored.stop_reason = {"type": "requires_action", "event_ids": [blocker.id]}
        await db.commit()

    headers = {**TEST_HEADERS, "Idempotency-Key": "tool-result-once"}
    payload = {
        "events": [
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": blocker.id,
                "content": [{"type": "text", "text": "found"}],
            }
        ]
    }
    first = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    replay = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    async with session_scope() as db:
        result_count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(
                SessionEvent.session_id == session["id"],
                SessionEvent.type == "user.custom_tool_result",
            )
        )
        work_count = await db.scalar(
            select(func.count())
            .select_from(ManagedResource)
            .where(
                ManagedResource.resource_type == "environment_work",
                ManagedResource.name == f"session:{session['id']}",
            )
        )
    assert (result_count, work_count) == (1, 1)


async def test_failed_submission_rolls_back_and_same_key_can_retry(client, monkeypatch):
    from app.routers import sessions as sessions_router

    session = await _managed_session(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "turn-transaction-retry"}
    payload = {"events": [{"type": "user.message", "content": "retry after rollback"}]}
    original_enqueue = sessions_router.enqueue_session_run

    async def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated enqueue failure")

    monkeypatch.setattr(sessions_router, "enqueue_session_run", fail_enqueue)
    failed = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    assert failed.status_code == 500, failed.text
    assert await _side_effect_counts(session["id"]) == (0, 0, 0)

    monkeypatch.setattr(sessions_router, "enqueue_session_run", original_enqueue)
    retried = await client.post(f"/v1/sessions/{session['id']}/events", headers=headers, json=payload)
    assert retried.status_code == 200, retried.text
    assert await _side_effect_counts(session["id"]) == (1, 1, 1)


async def test_requests_without_idempotency_key_cannot_queue_overlapping_work(client):
    session = await _managed_session(client)
    payload = {"events": [{"type": "user.message", "content": "legacy retry"}]}

    first = await client.post(f"/v1/sessions/{session['id']}/events", headers=TEST_HEADERS, json=payload)
    second = await client.post(f"/v1/sessions/{session['id']}/events", headers=TEST_HEADERS, json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert "active work" in second.json()["error"]["message"]
    assert await _side_effect_counts(session["id"]) == (1, 1, 0)


@pytest.mark.parametrize("key", ["", "x" * 256])
async def test_invalid_idempotency_keys_are_rejected_without_side_effects(client, key):
    session = await _managed_session(client)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers={**TEST_HEADERS, "Idempotency-Key": key},
        json={"events": [{"type": "user.message", "content": "must not persist"}]},
    )

    assert response.status_code == 422, response.text
    assert await _side_effect_counts(session["id"]) == (0, 0, 0)
