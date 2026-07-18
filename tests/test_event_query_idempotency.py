from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.engine import session_scope
from app.db.queries import events as events_q
from app.db.queries import sessions as sessions_q
from tests.conftest import TEST_HEADERS


async def _create_session(client, *, name: str) -> dict:
    agent = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": f"{name} Agent", "model": {"id": "test-model"}},
    )
    assert agent.status_code == 201, agent.text
    environment = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{name}-environment", "config": {"type": "self_hosted"}},
    )
    assert environment.status_code == 201, environment.text
    session = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"id": agent.json()["id"], "version": 1},
            "environment_id": environment.json()["id"],
        },
    )
    assert session.status_code == 201, session.text
    return session.json()


async def test_append_event_returns_existing_row_for_duplicate_id(client):
    session_data = await _create_session(client, name="event-idempotency")
    payload = {
        "type": "agent.message",
        "content": [{"type": "text", "text": "stable response"}],
    }

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_data["id"], for_update=True)
        assert session is not None
        first = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload=payload,
            event_id="evt_stable_duplicate",
        )
        await db.commit()
        first_id = first.id
        first_seq = first.seq
        first_payload = dict(first.payload)

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_data["id"], for_update=True)
        assert session is not None
        duplicate = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload=payload,
            event_id="evt_stable_duplicate",
        )
        following = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload={
                "type": "agent.message",
                "content": [{"type": "text", "text": "following response"}],
            },
            event_id="evt_after_duplicate",
        )
        await db.commit()

        duplicate_id = duplicate.id
        duplicate_seq = duplicate.seq
        duplicate_payload = dict(duplicate.payload)
        following_seq = following.seq

    assert duplicate_id == first_id == "evt_stable_duplicate"
    assert duplicate_seq == first_seq
    assert duplicate_payload == first_payload
    assert following_seq > first_seq

    async with session_scope() as db:
        stored = await events_q.list_events(
            db,
            session_id=session_data["id"],
            organization_id="org_test",
            limit=100,
        )
    assert [event.id for event in stored].count("evt_stable_duplicate") == 1


async def test_append_event_duplicate_id_keeps_first_replay_result(client):
    session_data = await _create_session(client, name="event-replay-collision")
    original_payload = {
        "type": "agent.message",
        "content": [{"type": "text", "text": "original"}],
    }
    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_data["id"], for_update=True)
        assert session is not None
        original = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload=original_payload,
            event_id="evt_replay_collision",
        )
        await db.commit()
        original_seq = original.seq
        original_payload = dict(original.payload)

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_data["id"], for_update=True)
        assert session is not None
        duplicate = await events_q.append_event(
            db,
            session,
            event_type="agent.tool_result",
            payload={
                "type": "agent.tool_result",
                "content": [{"type": "text", "text": "different replay result"}],
            },
            event_id="evt_replay_collision",
        )
        following = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload={
                "type": "agent.message",
                "content": [{"type": "text", "text": "transaction survived"}],
            },
            event_id="evt_after_payload_conflict",
        )
        await db.commit()
        duplicate_type = duplicate.type
        duplicate_payload = dict(duplicate.payload)
        following_seq = following.seq

    assert duplicate_type == "agent.message"
    assert duplicate_payload == original_payload
    assert following_seq > original_seq


async def test_append_event_does_not_return_event_from_another_session(client):
    first_session = await _create_session(client, name="event-identity-first")
    second_session = await _create_session(client, name="event-identity-second")
    payload = {
        "type": "agent.message",
        "content": [{"type": "text", "text": "same payload"}],
    }
    async with session_scope() as db:
        session = await sessions_q.get_session(db, first_session["id"], for_update=True)
        assert session is not None
        await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload=payload,
            event_id="evt_identity_conflict",
        )
        await db.commit()

    async with session_scope() as db:
        session = await sessions_q.get_session(db, second_session["id"], for_update=True)
        assert session is not None
        with pytest.raises(IntegrityError):
            await events_q.append_event(
                db,
                session,
                event_type="agent.message",
                payload=payload,
                event_id="evt_identity_conflict",
            )
        following = await events_q.append_event(
            db,
            session,
            event_type="agent.message",
            payload={"type": "agent.message", "content": []},
            event_id="evt_after_cross_session_collision",
        )
        await db.commit()
        following_session_id = following.session_id

    assert following_session_id == second_session["id"]
