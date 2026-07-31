"""The HTTP surface: what a client actually sees.

Runs the real app over ASGI — same routing, same dependencies, same error
handlers — with only the database and E2B swapped out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.deps import get_db

MESSAGE = {"type": "user.message", "content": [{"type": "text", "text": "hi"}]}


@pytest_asyncio.fixture
def headers(org):
    return {"x-organization-id": org, "x-api-key": "anything"}


@pytest_asyncio.fixture
async def client(db, sandboxes):
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def created(client, headers, agent, environment):
    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_id": agent.id, "environment_id": environment.id, "title": "t"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_process_health_does_not_require_tenant_auth(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_database_health_runs_a_round_trip(client):
    response = await client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_the_organization_header_is_required(client):
    response = await client.get("/v1/sessions", headers={"x-api-key": "anything"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["header", "x-organization-id"]


async def test_the_api_key_is_accepted_but_not_checked(client, org):
    """Not yet verified — the shape is in place so adding the lookup changes no client."""
    response = await client.get("/v1/sessions", headers={"x-organization-id": org})
    assert response.status_code == 200


async def test_creating_a_session_returns_it(created):
    assert created["type"] == "session"
    assert created["status"] == "idle"
    assert created["id"].startswith("sess_")


async def test_a_session_never_exposes_its_internals(created):
    for hidden in ("organization_id", "lock_version", "lease_expires_at"):
        assert hidden not in created


async def test_a_missing_session_is_a_404(client, headers):
    response = await client.get("/v1/sessions/sess_nope", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_sending_a_message_returns_the_events(client, headers, created):
    response = await client.post(
        f"/v1/sessions/{created['id']}/events",
        headers=headers,
        json={"events": [MESSAGE]},
    )
    assert response.status_code == 200
    body = response.json()
    assert [e["seq"] for e in body["data"]] == [1]
    assert body["data"][0]["type"] == "user.message"


async def test_a_busy_session_answers_409_without_a_retry_hint(client, headers, created):
    """No `Retry-After`.

    The lease is renewed for as long as the worker lives, so its remainder is
    not how long the turn has left — it only measures how fast a dead worker
    is noticed. A header that says otherwise sends obedient clients back at
    exactly the wrong moment.
    """
    await client.post(
        f"/v1/sessions/{created['id']}/events", headers=headers, json={"events": [MESSAGE]}
    )

    response = await client.post(
        f"/v1/sessions/{created['id']}/events", headers=headers, json={"events": [MESSAGE]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_busy"
    assert "retry-after" not in response.headers
    assert "retry_after_seconds" not in response.json()["error"]


async def test_interrupt_gets_through_a_busy_session(client, headers, created):
    await client.post(
        f"/v1/sessions/{created['id']}/events", headers=headers, json={"events": [MESSAGE]}
    )

    response = await client.post(
        f"/v1/sessions/{created['id']}/events",
        headers=headers,
        json={"events": [{"type": "user.interrupt"}]},
    )

    assert response.status_code == 200
    session = (await client.get(f"/v1/sessions/{created['id']}", headers=headers)).json()
    assert session["status"] == "idle"
    assert session["stop_reason"]["type"] == "interrupted"


async def test_events_can_be_read_back_incrementally(client, headers, created):
    await client.post(
        f"/v1/sessions/{created['id']}/events", headers=headers, json={"events": [MESSAGE]}
    )

    response = await client.get(f"/v1/sessions/{created['id']}/events", headers=headers)
    assert [e["seq"] for e in response.json()["data"]] == [1]

    response = await client.get(
        f"/v1/sessions/{created['id']}/events?after_seq=1", headers=headers
    )
    assert response.json()["data"] == []


async def test_another_tenant_gets_a_404_not_someone_elses_session(client, headers, created):
    response = await client.get(
        f"/v1/sessions/{created['id']}",
        headers={"x-organization-id": "org_intruder", "x-api-key": "anything"},
    )
    assert response.status_code == 404


async def test_unknown_fields_are_rejected(client, headers, created):
    response = await client.post(
        f"/v1/sessions/{created['id']}",
        headers=headers,
        json={"title": "new", "status": "running"},
    )
    assert response.status_code == 422


# --- the internal endpoint ---------------------------------------------------
#
# Cloud Tasks calls this to run a turn. It skips the busy check and starts work
# directly, so who may call it is the whole of its security.


async def test_the_internal_endpoint_does_not_exist_under_inline_dispatch(client, headers):
    """Nothing legitimate calls it without a queue, so it is simply not there —
    an unauthenticated caller learns nothing about what runs behind it."""
    response = await client.post("/internal/sessions/ses_1/process", json={"message": {}})

    assert response.status_code == 404


async def test_an_unsigned_call_is_refused(client, cloud_dispatch):
    response = await client.post("/internal/sessions/ses_1/process", json={"message": {}})

    assert response.status_code == 401


async def test_a_token_from_the_wrong_account_is_refused(client, cloud_dispatch, monkeypatch):
    """A valid Google token is not enough. Any Google account can mint one for
    this audience; it has to be the service account the queue was told to use.
    """
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience: {"email": "someone-else@example.com"},
    )

    response = await client.post(
        "/internal/sessions/ses_1/process",
        json={"message": {}},
        headers={"Authorization": "Bearer whatever"},
    )

    assert response.status_code == 403


async def test_a_signed_call_from_the_queue_runs_the_turn(client, cloud_dispatch, monkeypatch):
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience: {"email": cloud_dispatch},
    )
    ran: list[str] = []

    async def _run(db, *, session_id, events):
        ran.append(session_id)

    monkeypatch.setattr("app.services.sessions.process_session", _run)

    response = await client.post(
        "/internal/sessions/ses_1/process",
        json={"events": [{"type": "user.message"}]},
        headers={"Authorization": "Bearer whatever"},
    )

    assert response.status_code == 204
    assert ran == ["ses_1"]
