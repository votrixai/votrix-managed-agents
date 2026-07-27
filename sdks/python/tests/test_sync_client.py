from __future__ import annotations

import json

import httpx
import pytest

from votrix.managed_agents import UnprocessableEntityError, Votrix


def test_sync_client_configuration_is_fail_closed_and_matches_async(monkeypatch):
    monkeypatch.setenv("VMA_API_KEY", "vma_test_environment")
    monkeypatch.setenv("VOTRIX_VMA_API_KEY", "vma_test_environment")
    monkeypatch.setenv("VMA_BASE_URL", "https://environment.vma.test")
    monkeypatch.setenv("VOTRIX_VMA_BASE_URL", "https://environment.vma.test")

    with Votrix() as client:
        assert client._api_key == "vma_test_environment"
        assert str(client.base_url) == "https://environment.vma.test/"

    with pytest.raises(ValueError, match="api_key"):
        Votrix(api_key="", base_url="https://explicit.vma.test")
    with pytest.raises(ValueError, match="base_url"):
        Votrix(api_key="vma_test_explicit", base_url=" ")

    monkeypatch.setenv("VOTRIX_VMA_API_KEY", "vma_test_conflict")
    with pytest.raises(ValueError, match="different API key values"):
        Votrix(base_url="https://explicit.vma.test")

    with Votrix(
        api_key="vma_test_explicit",
        base_url="https://explicit.vma.test",
    ) as client:
        assert client._api_key == "vma_test_explicit"


def model_credential_payload(*, archived: bool = False) -> dict:
    return {
        "id": "credential_1",
        "type": "model_credential",
        "vault_id": "vault_1",
        "model_provider": "openrouter",
        "display_name": "End-user OpenRouter",
        "metadata": {},
        "archived_at": "2026-07-15T00:00:00Z" if archived else None,
    }


def test_sync_model_credential_lifecycle_and_secret_redaction():
    calls: list[tuple[str, str, dict[str, str], dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if body.get("api_key") == "sk-rejected":
            return httpx.Response(
                422,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_provider_credential",
                        "message": "invalid sk-rejected",
                    }
                },
            )
        if request.method == "GET" and request.url.path.endswith("/model_credentials"):
            return httpx.Response(
                200,
                json={"data": [model_credential_payload()], "has_more": False},
            )
        if request.method == "GET":
            return httpx.Response(200, json=model_credential_payload())
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={
                    "id": "credential_1",
                    "type": "model_credential_deleted",
                    "deleted": True,
                },
            )
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, json=model_credential_payload(archived=True))
        return httpx.Response(
            201 if request.url.path.endswith("/model_credentials") else 200,
            json=model_credential_payload(),
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with Votrix(
        api_key="vma_test_sync",
        base_url="https://vma.test",
        max_retries=0,
        http_client=http_client,
    ) as client:
        created = client.vaults.model_credentials.create(
            "vault_1",
            provider="openrouter",
            api_key="sk-created",
            display_name="End-user OpenRouter",
        )
        rotated = client.vaults.model_credentials.rotate(
            "vault_1",
            created.id,
            api_key="sk-rotated",
        )
        page = client.vaults.model_credentials.list(
            "vault_1",
            include_archived=True,
        )
        retrieved = client.vaults.model_credentials.retrieve(
            created.id,
            vault_id="vault_1",
        )
        archived = client.vaults.model_credentials.archive(
            created.id,
            vault_id="vault_1",
        )
        deleted = client.vaults.model_credentials.delete(
            created.id,
            vault_id="vault_1",
        )
        with pytest.raises(UnprocessableEntityError) as caught:
            client.vaults.model_credentials.create(
                "vault_1",
                provider="openrouter",
                api_key="sk-rejected",
            )

    assert not http_client.is_closed
    http_client.close()
    assert created.id == rotated.id == retrieved.id == archived.id
    assert page.data[0].id == created.id
    assert [item.id for item in page] == [created.id]
    assert archived.archived_at is not None
    assert deleted.type == "model_credential_deleted"
    public_values = [created, rotated, *page.data, retrieved, archived, deleted]
    serialized = " ".join(item.model_dump_json() for item in public_values)
    assert "secret_name" not in serialized
    assert "sk-created" not in serialized
    assert "sk-rotated" not in serialized
    assert "sk-rejected" not in str(caught.value)
    assert "sk-rejected" not in repr(caught.value)
    assert "sk-rejected" not in repr(caught.value.body)
    assert caught.value.error_code == "invalid_provider_credential"

    assert calls[:2] == [
        (
            "POST",
            "/v1/vaults/vault_1/model_credentials",
            {},
            {
                "provider": "openrouter",
                "api_key": "sk-created",
                "display_name": "End-user OpenRouter",
            },
        ),
        (
            "POST",
            "/v1/vaults/vault_1/model_credentials/credential_1",
            {},
            {"api_key": "sk-rotated"},
        ),
    ]


def test_sync_provider_and_vault_wrappers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "vma_test_sync"
        if request.url.path == "/v1/model_providers":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "openrouter",
                            "type": "model_provider",
                            "display_name": "OpenRouter",
                            "adapter": "openrouter",
                            "credential_type": "api_key",
                            "capabilities": {},
                        }
                    ],
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/vaults"
        return httpx.Response(
            201,
            json={
                "id": "vault_1",
                "type": "vault",
                "display_name": "Organization keys",
                "metadata": {},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with Votrix(
            api_key="vma_test_sync",
            base_url="https://vma.test",
            max_retries=0,
            http_client=http_client,
        ) as client:
            provider = client.model_providers.list().data[0]
            vault = client.vaults.create(display_name="Organization keys")
    assert provider.id == "openrouter"
    assert vault.id == "vault_1"


def test_sync_api_key_lifecycle_exposes_secrets_only_on_create_and_rotate():
    def payload(key_id: str, *, secret: str | None = None, revoked: bool = False) -> dict:
        value = {
            "id": key_id,
            "type": "api_key",
            "organization_id": "org_test",
            "name": "CI",
            "prefix": "vma_test_ci",
            "scopes": ["api", "api_keys:manage"],
            "expires_at": None,
            "created_by": "key_admin",
            "metadata": {},
            "last_used_at": None,
            "revoked_at": "2026-07-15T00:00:00Z" if revoked else None,
            "revoked_by": "key_admin" if revoked else None,
            "revocation_reason": "retired" if revoked else None,
            "replaced_by_key_id": None,
            "replaces_key_id": "key_1" if key_id == "key_2" else None,
            "created_at": "2026-07-15T00:00:00Z",
            "updated_at": "2026-07-15T00:00:00Z",
        }
        if secret is not None:
            value["secret"] = secret
        return value

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/api_keys":
            return httpx.Response(201, json=payload("key_1", secret="vma_test_sync_create"))
        if request.url.path.endswith("/rotate"):
            return httpx.Response(201, json=payload("key_2", secret="vma_test_sync_rotate"))
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200, json=payload("key_2", secret="ignored", revoked=True))
        if request.method == "GET" and request.url.path == "/v1/api_keys":
            return httpx.Response(
                200,
                json={"data": [payload("key_1", secret="ignored")], "has_more": False},
            )
        return httpx.Response(200, json=payload("key_1", secret="ignored"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with Votrix(
            api_key="vma_test_sync",
            base_url="https://vma.test",
            max_retries=0,
            http_client=http_client,
        ) as client:
            created = client.api_keys.create(name="CI", scopes=["api", "api_keys:manage"])
            page = client.api_keys.list(include_revoked=False)
            retrieved = client.api_keys.retrieve(created.id)
            rotated = client.api_keys.rotate(created.id, reason="rollover")
            revoked = client.api_keys.revoke(rotated.id, reason="retired")

    assert created.secret.get_secret_value() == "vma_test_sync_create"
    assert rotated.secret.get_secret_value() == "vma_test_sync_rotate"
    for safe in [*page.data, retrieved, revoked]:
        assert "secret" not in safe.model_dump()
