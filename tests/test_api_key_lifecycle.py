from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import DatabaseApiKeyAuthProvider, default_auth_provider
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Workspace
from app.db.queries import api_keys as api_keys_q
from app.factory import create_app
from tests.conftest import TEST_HEADERS


async def _seed_key(*, workspace_id: str, name: str, scopes: list[str], expires_at=None):
    async with session_scope() as db:
        api_key, token = await api_keys_q.create_api_key(
            db,
            workspace_id=workspace_id,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )
        await db.commit()
        return api_key, token


def _headers(token: str) -> dict[str, str]:
    return {**TEST_HEADERS, "x-api-key": token}


async def test_api_key_lifecycle_returns_plaintext_once_and_is_workspace_scoped():
    admin_a, admin_token_a = await _seed_key(
        workspace_id="wrkspc_keys_a",
        name="Workspace A admin",
        scopes=[api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE],
    )
    _, admin_token_b = await _seed_key(
        workspace_id="wrkspc_keys_b",
        name="Workspace B admin",
        scopes=[api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE],
    )

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created = await client.post(
            "/v1/api_keys",
            headers=_headers(admin_token_a),
            json={
                "name": "Public SDK",
                "scopes": ["api"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                "metadata": {"environment": "test"},
            },
        )
        assert created.status_code == 201, created.text
        created_body = created.json()
        child_id = created_body["id"]
        child_token = created_body["secret"]
        assert child_token.startswith("vma_")
        assert created_body["prefix"] == child_token[:12]
        assert created_body["created_by"] == admin_a.id

        listed = await client.get("/v1/api_keys", headers=_headers(admin_token_a))
        assert listed.status_code == 200, listed.text
        listed_child = next(item for item in listed.json()["data"] if item["id"] == child_id)
        assert "secret" not in listed_child
        assert "key_hash" not in listed_child

        retrieved = await client.get(f"/v1/api_keys/{child_id}", headers=_headers(admin_token_a))
        assert retrieved.status_code == 200, retrieved.text
        assert "secret" not in retrieved.json()

        denied = await client.get("/v1/api_keys", headers=_headers(child_token))
        assert denied.status_code == 403
        assert "api_keys:manage" in denied.json()["error"]["message"]

        other_tenant = await client.get(f"/v1/api_keys/{child_id}", headers=_headers(admin_token_b))
        assert other_tenant.status_code == 404

        created_agent = await client.post(
            "/v1/agents",
            headers=_headers(child_token),
            json={"name": "Tenant A Agent", "model": {"id": "gpt-5.5"}},
        )
        assert created_agent.status_code == 201, created_agent.text
        agent_id = created_agent.json()["id"]
        cross_tenant_agent = await client.get(f"/v1/agents/{agent_id}", headers=_headers(admin_token_b))
        assert cross_tenant_agent.status_code == 404

        rotated = await client.post(
            f"/v1/api_keys/{child_id}/rotate",
            headers=_headers(admin_token_a),
            json={"reason": "scheduled rotation"},
        )
        assert rotated.status_code == 201, rotated.text
        replacement = rotated.json()
        replacement_token = replacement["secret"]
        assert replacement["replaces_key_id"] == child_id
        assert replacement_token != child_token

        old_denied = await client.get("/v1/agents", headers=_headers(child_token))
        assert old_denied.status_code == 401
        new_allowed = await client.get("/v1/agents", headers=_headers(replacement_token))
        assert new_allowed.status_code == 200, new_allowed.text

        old_record = await client.get(f"/v1/api_keys/{child_id}", headers=_headers(admin_token_a))
        assert old_record.status_code == 200, old_record.text
        assert old_record.json()["replaced_by_key_id"] == replacement["id"]
        assert old_record.json()["revocation_reason"] == "scheduled rotation"

        revoked = await client.post(
            f"/v1/api_keys/{replacement['id']}/revoke",
            headers=_headers(admin_token_a),
            json={"reason": "customer request"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revocation_reason"] == "customer request"
        assert revoked.json()["revoked_by"] == admin_a.id
        revoked_denied = await client.get("/v1/agents", headers=_headers(replacement_token))
        assert revoked_denied.status_code == 401

    async with session_scope() as db:
        stored = await api_keys_q.get_api_key(db, child_id, workspace_id="wrkspc_keys_a")
        assert stored is not None
        assert stored.key_hash == api_keys_q.hash_api_key(child_token)
        assert stored.key_hash != child_token


async def test_expired_api_key_is_rejected_and_past_expiry_cannot_be_issued():
    _, expired_token = await _seed_key(
        workspace_id="wrkspc_expired",
        name="Expired",
        scopes=[api_keys_q.API_SCOPE],
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    _, admin_token = await _seed_key(
        workspace_id="wrkspc_expired",
        name="Admin",
        scopes=[api_keys_q.API_KEYS_MANAGE_SCOPE],
    )

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        expired = await client.get("/v1/agents", headers=_headers(expired_token))
        assert expired.status_code == 401

        invalid_create = await client.post(
            "/v1/api_keys",
            headers=_headers(admin_token),
            json={
                "name": "Already expired",
                "scopes": ["api"],
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            },
        )
        assert invalid_create.status_code == 422


async def test_archived_workspace_api_key_is_rejected():
    _, token = await _seed_key(
        workspace_id="wrkspc_archived_tenant",
        name="Archived tenant key",
        scopes=[api_keys_q.API_SCOPE],
    )
    async with session_scope() as db:
        workspace = Workspace(
            id="wrkspc_archived_tenant",
            slug="archived-tenant",
            name="Archived tenant",
            metadata_={},
            archived_at=datetime.now(timezone.utc),
        )
        db.add(workspace)
        await db.commit()

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/v1/agents", headers=_headers(token))
    assert response.status_code == 401


async def test_worker_scope_is_separate_and_tenant_bound():
    _, api_token_a = await _seed_key(
        workspace_id="wrkspc_worker_a",
        name="API A",
        scopes=[api_keys_q.API_SCOPE],
    )
    _, worker_token_a = await _seed_key(
        workspace_id="wrkspc_worker_a",
        name="Worker A",
        scopes=[api_keys_q.WORKER_SCOPE],
    )
    _, worker_token_b = await _seed_key(
        workspace_id="wrkspc_worker_b",
        name="Worker B",
        scopes=[api_keys_q.WORKER_SCOPE],
    )

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        environment = await client.post(
            "/v1/environments",
            headers=_headers(api_token_a),
            json={"name": "Tenant worker", "config": {"type": "self_hosted"}},
        )
        assert environment.status_code == 201, environment.text
        environment_id = environment.json()["id"]

        api_key_denied = await client.get(
            f"/v1/environments/{environment_id}/work/stats",
            headers=_headers(api_token_a),
        )
        assert api_key_denied.status_code == 403

        worker_allowed = await client.get(
            f"/v1/environments/{environment_id}/work/stats",
            headers=_headers(worker_token_a),
        )
        assert worker_allowed.status_code == 200, worker_allowed.text

        worker_cannot_use_api = await client.get("/v1/agents", headers=_headers(worker_token_a))
        assert worker_cannot_use_api.status_code == 403

        other_worker_cannot_see_environment = await client.get(
            f"/v1/environments/{environment_id}/work/stats",
            headers=_headers(worker_token_b),
        )
        assert other_worker_cannot_see_environment.status_code == 404


@pytest.mark.parametrize("app_env", ["local", "test", "production"])
def test_default_auth_provider_is_database_backed_in_every_environment(monkeypatch, app_env):
    monkeypatch.setenv("APP_ENV", app_env)
    get_settings.cache_clear()
    assert isinstance(default_auth_provider(), DatabaseApiKeyAuthProvider)
    assert isinstance(create_app().state.auth_provider, DatabaseApiKeyAuthProvider)
