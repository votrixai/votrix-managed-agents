"""The HTTP surface: what a client actually sees.

Runs the real app over ASGI — same routing, same dependencies, same error
handlers — with only the database and E2B swapped out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio
from fastapi import HTTPException, status
from httpx import ASGITransport, AsyncClient

from app import human_auth
from app.main import app
from app.db.queries import organizations as organizations_q
from app.db.queries import vma_api_keys as api_keys_q
from app.routers import health as health_router
from app.routers.deps import get_console_principal, get_db, get_organization_id

MESSAGE = {"type": "user.message", "content": [{"type": "text", "text": "hi"}]}


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


async def test_process_health_does_not_require_tenant_auth(client, monkeypatch):
    commit = "952e7d141ad3c8548af497d99a0e43ffc8d06486"
    monkeypatch.setattr(
        health_router,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            vma_public_build_id="952e7d1",
            vma_git_commit_sha=commit,
        ),
    )
    monkeypatch.setattr(health_router, "_PROCESS_STARTED_AT", 100.0)
    monkeypatch.setattr(health_router, "monotonic", lambda: 112.3456)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "production",
        "build": "952e7d1",
        "git_commit": commit,
        "git_commit_url": (
            "https://github.com/votrixai/votrix-managed-agents/commit/"
            "952e7d141ad3c8548af497d99a0e43ffc8d06486"
        ),
        "uptime_seconds": 12.346,
    }


async def test_database_health_runs_a_round_trip(client):
    response = await client.get("/health/db")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["database_latency_ms"] >= 0
    assert body["uptime_seconds"] >= 0


async def test_a_request_without_a_key_is_refused(client):
    response = await client.get("/v1/sessions")
    assert response.status_code == 401


async def test_an_unknown_key_is_refused(client, org):
    response = await client.get("/v1/sessions", headers={"x-api-key": "sk-nope"})
    assert response.status_code == 401


async def test_naming_a_tenant_does_not_reach_it(client, org):
    """The first-party tenant header does not authenticate by itself.

    API-key callers derive their tenant from the key. First-party callers must
    pair the selected Organization with a verified bearer identity and live
    membership.
    """
    response = await client.get(
        "/v1/sessions", headers={"x-organization-id": org}
    )
    assert response.status_code == 401


async def test_member_user_token_reaches_only_its_organization(
    client,
    db,
    org,
    monkeypatch,
):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="user-a",
        email="a@example.com",
    )
    other = await organizations_q.create_organization(db, name="Other")
    await db.commit()

    async def authenticated_user(access_token: str):
        assert access_token == "user-a-token"
        return human_auth.AuthenticatedUser(id="user-a", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    auth = {"authorization": "Bearer user-a-token"}

    own = await client.get(
        "/v1/sessions",
        headers={**auth, "x-organization-id": org},
    )
    denied = await client.get(
        "/v1/sessions",
        headers={**auth, "x-organization-id": other.id},
    )

    assert own.status_code == 200
    assert denied.status_code == 403


async def test_super_admin_user_token_reaches_an_active_organization(
    client,
    org,
    monkeypatch,
):
    async def authenticated_user(_access_token: str):
        return human_auth.AuthenticatedUser(
            id="platform-admin",
            app_metadata={"super_admin": True},
        )

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    response = await client.get(
        "/v1/sessions",
        headers={
            "authorization": "Bearer admin-token",
            "x-organization-id": org,
        },
    )

    assert response.status_code == 200


async def test_user_token_requires_an_organization(client, monkeypatch):
    async def authenticated_user(_access_token: str):
        raise AssertionError("identity must not be resolved without an Organization")

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    response = await client.get(
        "/v1/sessions",
        headers={"authorization": "Bearer user-token"},
    )

    assert response.status_code == 400


async def test_invalid_user_token_is_refused(client, org, monkeypatch):
    async def authenticated_user(_access_token: str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user access token")

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    response = await client.get(
        "/v1/sessions",
        headers={
            "authorization": "Bearer invalid",
            "x-organization-id": org,
        },
    )

    assert response.status_code == 401


async def test_api_key_tenant_wins_over_user_selected_organization(
    client,
    headers,
    other_tenant,
    monkeypatch,
):
    other_id, _ = other_tenant

    async def authenticated_user(_access_token: str):
        raise AssertionError("bearer auth must not run when an API key is present")

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    response = await client.get(
        "/v1/sessions",
        headers={
            **headers,
            "authorization": "Bearer user-token",
            "x-organization-id": other_id,
        },
    )

    assert response.status_code == 200


async def test_api_key_auth_releases_its_read_transaction(db, headers, org):
    organization_id = await get_organization_id(
        db,
        x_api_key=headers["x-api-key"],
        authorization=None,
        x_organization_id=None,
    )

    assert organization_id == org
    assert not db.in_transaction()


async def test_console_auth_releases_its_read_transaction(
    db,
    org,
    monkeypatch,
):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="stream-user",
    )
    await db.commit()

    async def authenticated_user(_access_token: str):
        return human_auth.AuthenticatedUser(id="stream-user", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    principal = await get_console_principal(
        db,
        authorization="Bearer stream-token",
        x_organization_id=org,
    )

    assert principal.organization_id == org
    assert principal.user_id == "stream-user"
    assert not db.in_transaction()


async def test_member_creates_an_api_key_that_is_returned_only_once(
    client,
    db,
    org,
    monkeypatch,
):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="key-user",
        email="key-user@example.com",
    )
    await db.commit()

    async def authenticated_user(access_token: str):
        assert access_token == "key-user-token"
        return human_auth.AuthenticatedUser(id="key-user", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    auth = {
        "authorization": "Bearer key-user-token",
        "x-organization-id": org,
    }

    created = await client.post(
        "/v1/me/api-keys",
        headers=auth,
        json={"name": "  Local development  "},
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "private, no-store, max-age=0"
    payload = created.json()["data"]
    plaintext = payload["api_key"]
    api_keys_q.validate_vma_api_key(plaintext)
    assert payload["name"] == "Local development"
    assert payload["can_revoke"] is True

    stored = await api_keys_q.get_vma_api_key_by_token(db, plaintext)
    assert stored is not None
    assert stored.organization_id == org
    assert stored.created_by == "key-user"
    assert plaintext not in {str(value) for value in stored.__dict__.values()}

    listed = await client.get("/v1/me/api-keys", headers=auth)

    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "private, no-store, max-age=0"
    assert len(listed.json()["data"]) == 1
    listed_key = listed.json()["data"][0]
    assert listed_key["id"] == payload["id"]
    assert listed_key["name"] == payload["name"]
    assert listed_key["prefix"] == payload["prefix"]
    assert listed_key["can_revoke"] is True
    assert plaintext not in listed.text


async def test_api_key_auth_cannot_manage_api_keys(client, headers):
    response = await client.get("/v1/me/api-keys", headers=headers)

    assert response.status_code == 401


async def test_member_revokes_only_a_key_they_created(
    client,
    db,
    org,
    monkeypatch,
):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="member-key-user",
    )
    someone_elses_key, someone_elses_plaintext = await api_keys_q.create_vma_api_key(
        db,
        organization_id=org,
        name="Shared service",
        created_by="another-user",
    )
    await db.commit()

    async def authenticated_user(_access_token: str):
        return human_auth.AuthenticatedUser(id="member-key-user", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    auth = {
        "authorization": "Bearer member-token",
        "x-organization-id": org,
    }

    listed = await client.get("/v1/me/api-keys", headers=auth)
    denied = await client.delete(
        f"/v1/me/api-keys/{someone_elses_key.id}",
        headers=auth,
    )
    own = await client.post(
        "/v1/me/api-keys",
        headers=auth,
        json={"name": "My integration"},
    )
    own_plaintext = own.json()["data"]["api_key"]
    revoked = await client.delete(
        f"/v1/me/api-keys/{own.json()['data']['id']}",
        headers=auth,
    )

    assert listed.status_code == 200
    assert listed.json()["data"][0]["can_revoke"] is False
    assert denied.status_code == 403
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert await api_keys_q.get_vma_api_key_by_token(db, own_plaintext) is None
    assert (
        await api_keys_q.get_vma_api_key_by_token(db, someone_elses_plaintext)
        is someone_elses_key
    )


async def test_admin_can_revoke_any_api_only_key_but_management_keys_are_hidden(
    client,
    db,
    org,
    monkeypatch,
):
    from app.db.models import MEMBER_ROLE_ADMIN

    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="key-admin",
        role=MEMBER_ROLE_ADMIN,
    )
    ordinary, _ = await api_keys_q.create_vma_api_key(
        db,
        organization_id=org,
        name="Ordinary",
        created_by="someone-else",
    )
    management, _ = await api_keys_q.create_vma_api_key(
        db,
        organization_id=org,
        name="Operator",
        scopes=[
            api_keys_q.VMA_API_SCOPE,
            api_keys_q.VMA_API_KEYS_MANAGE_SCOPE,
        ],
        created_by="bootstrap",
    )
    await db.commit()

    async def authenticated_user(_access_token: str):
        return human_auth.AuthenticatedUser(id="key-admin", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    auth = {
        "authorization": "Bearer admin-token",
        "x-organization-id": org,
    }

    listed = await client.get("/v1/me/api-keys", headers=auth)
    revoked = await client.delete(f"/v1/me/api-keys/{ordinary.id}", headers=auth)
    protected = await client.delete(
        f"/v1/me/api-keys/{management.id}",
        headers=auth,
    )

    assert [item["id"] for item in listed.json()["data"]] == [ordinary.id]
    assert listed.json()["data"][0]["can_revoke"] is True
    assert revoked.status_code == 200
    assert protected.status_code == 404


async def test_api_key_name_cannot_be_blank(client, db, org, monkeypatch):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="blank-name-user",
    )
    await db.commit()

    async def authenticated_user(_access_token: str):
        return human_auth.AuthenticatedUser(id="blank-name-user", app_metadata={})

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated_user)
    response = await client.post(
        "/v1/me/api-keys",
        headers={
            "authorization": "Bearer blank-token",
            "x-organization-id": org,
        },
        json={"name": "   "},
    )

    assert response.status_code == 422


async def test_api_key_management_endpoint_is_hidden_from_openapi(client):
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/v1/me/api-keys" not in response.json()["paths"]


async def test_a_revoked_key_stops_working(client, db, org):
    from app.db.queries import vma_api_keys as keys_q

    api_key, token = await keys_q.create_vma_api_key(
        db, organization_id=org, name="doomed"
    )
    await db.commit()
    assert (
        await client.get("/v1/sessions", headers={"x-api-key": token})
    ).status_code == 200

    await keys_q.revoke_vma_api_key(db, api_key)
    await db.commit()

    assert (
        await client.get("/v1/sessions", headers={"x-api-key": token})
    ).status_code == 401


async def test_creating_a_session_returns_it(created):
    assert created["type"] == "session"
    assert created["status"] == "idle"
    assert created["id"].startswith("sess_")


async def test_session_pins_the_model_it_was_given(
    client, headers, agent, environment
):
    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "model": "claude-opus-5",
        },
    )

    assert response.status_code == 201, response.text
    # Normalised on the way in: a bare string is shorthand for {"id": ...}, the
    # same as on an Agent, so every reader sees one shape.
    assert response.json()["model"] == {"id": "claude-opus-5"}


async def test_a_session_without_a_model_follows_the_agent(created):
    """Null, not a copy of the Agent's model.

    Copying it at creation would freeze the choice: editing the Agent later
    would stop reaching a conversation that never asked to opt out of it.
    """
    assert created["model"] is None


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


async def test_another_tenant_gets_a_404_not_someone_elses_session(
    client, db, headers, created, other_tenant):
    other_id, other_headers = other_tenant
    """Not a 403: telling them it exists is telling them something."""

    response = await client.get(
        f"/v1/sessions/{created['id']}",
        headers=other_headers,
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
