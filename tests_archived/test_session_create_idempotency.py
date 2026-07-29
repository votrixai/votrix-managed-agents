from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import ManagedSession, TenantIdempotencyRecord
from tests.conftest import TEST_HEADERS


async def _agent_and_environment(client):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Idempotent session agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "idempotent-session-env", "config": {"type": "cloud"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    return agent_response.json(), environment_response.json()


async def test_session_create_idempotency_replays_without_second_session(client):
    agent, environment = await _agent_and_environment(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "session-create-once"}
    payload = {
        "agent": {"type": "agent", "id": agent["id"], "version": 1},
        "environment_id": environment["id"],
        "title": "one session",
    }

    first = await client.post("/v1/sessions", headers=headers, json=payload)
    replay = await client.post("/v1/sessions", headers=headers, json=payload)

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    async with session_scope() as db:
        session_count = await db.scalar(select(func.count()).select_from(ManagedSession))
        record = await db.scalar(select(TenantIdempotencyRecord))
    assert session_count == 1
    assert record is not None
    assert record.operation == "sessions.create"
    assert record.state == "completed"
    assert record.key_hash != "session-create-once"


async def test_session_create_key_reuse_with_different_body_is_rejected(client):
    agent, environment = await _agent_and_environment(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "session-create-conflict"}
    payload = {
        "agent": {"type": "agent", "id": agent["id"], "version": 1},
        "environment_id": environment["id"],
        "title": "first title",
    }
    first = await client.post("/v1/sessions", headers=headers, json=payload)
    conflict = await client.post(
        "/v1/sessions",
        headers=headers,
        json={**payload, "title": "different title"},
    )

    assert first.status_code == 201, first.text
    assert conflict.status_code == 422, conflict.text
    assert "different request" in conflict.json()["error"]["message"]
    async with session_scope() as db:
        count = await db.scalar(select(func.count()).select_from(ManagedSession))
    assert count == 1


async def test_failed_session_create_rolls_back_idempotency_claim(client):
    agent, environment = await _agent_and_environment(client)
    headers = {**TEST_HEADERS, "Idempotency-Key": "session-create-retry-after-failure"}
    invalid_payload = {
        "agent": {"type": "agent", "id": agent["id"], "version": 1},
        "environment_id": "env_missing",
    }
    failed = await client.post("/v1/sessions", headers=headers, json=invalid_payload)
    assert failed.status_code == 404, failed.text

    valid = await client.post(
        "/v1/sessions",
        headers=headers,
        json={**invalid_payload, "environment_id": environment["id"]},
    )
    assert valid.status_code == 201, valid.text
