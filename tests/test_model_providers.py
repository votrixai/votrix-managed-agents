import base64
import json

import pytest

from app.config import Settings, get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.secret_cipher import ENCRYPTED_PREFIX, decrypt_secret
from tests.conftest import TEST_HEADERS, UNAUTHENTICATED_TEST_HEADERS


async def test_model_provider_catalog_is_stable_and_secret_free(client, monkeypatch):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "open-router": {
                    "display_name": "OpenRouter Fast",
                    "adapter": "openrouter",
                    "api_key_env": "INTERNAL_OPENROUTER_SECRET_NAME",
                    "base_url": "https://private-gateway.example/v1",
                    "default_model": "deepseek/deepseek-v4-pro",
                    "model_kwargs": {"private_routing_token": "hidden"},
                    "capabilities": {
                        "multimodal_input": True,
                        "reasoning": True,
                    },
                },
                "local-fake": {
                    "adapter": "fake",
                    "default_model": "test-model",
                },
            }
        ),
    )
    get_settings.cache_clear()

    response = await client.get("/v1/model_providers", headers=TEST_HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["id"] for item in payload["data"]] == [
        "anthropic",
        "deepseek",
        "local_fake",
        "open_router",
        "openai",
    ]
    assert payload["has_more"] is False
    assert payload["first_id"] == "anthropic"
    assert payload["last_id"] == "openai"

    openrouter = next(item for item in payload["data"] if item["id"] == "open_router")
    assert openrouter == {
        "id": "open_router",
        "type": "model_provider",
        "display_name": "OpenRouter Fast",
        "adapter": "openrouter",
        "credential_type": "api_key",
        "default_model": "deepseek/deepseek-v4-pro",
        "capabilities": {
            "streaming": True,
            "tool_calls": True,
            "multimodal_input": True,
            "reasoning": True,
            "native_structured_output": False,
        },
    }
    local_fake = next(item for item in payload["data"] if item["id"] == "local_fake")
    assert local_fake["credential_type"] == "none"

    serialized = response.text
    for forbidden in (
        "INTERNAL_OPENROUTER_SECRET_NAME",
        "private-gateway.example",
        "private_routing_token",
        "api_key_env",
        "base_url",
        "model_kwargs",
    ):
        assert forbidden not in serialized


def test_model_provider_registry_rejects_embedded_api_keys():
    with pytest.raises(ValueError, match="must not embed model API keys"):
        Settings(
            _env_file=None,
            vma_model_providers={
                "openrouter": {
                    "adapter": "openrouter",
                    "api_key": "must-never-be-stored-in-provider-config",
                }
            },
        )


async def test_retrieve_model_provider_and_not_found(client, monkeypatch):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "openrouter": {
                    "adapter": "openrouter",
                    "default_model": "deepseek/deepseek-v4-pro",
                }
            }
        ),
    )
    get_settings.cache_clear()

    response = await client.get("/v1/model_providers/OpenRouter", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "openrouter"
    assert response.json()["display_name"] == "OpenRouter"

    missing = await client.get("/v1/model_providers/unknown", headers=TEST_HEADERS)
    assert missing.status_code == 404
    assert "not found" in missing.json()["error"]["message"].lower()


async def test_model_provider_catalog_requires_auth_and_is_in_openapi(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()

    unauthenticated = await client.get(
        "/v1/model_providers",
        headers=UNAUTHENTICATED_TEST_HEADERS,
    )
    assert unauthenticated.status_code == 401

    authenticated = await client.get(
        "/v1/model_providers",
        headers=TEST_HEADERS,
    )
    assert authenticated.status_code == 200, authenticated.text

    schema = (await client.get("/openapi.json")).json()
    assert "/v1/model_providers" in schema["paths"]
    assert "/v1/model_providers/{provider_id}" in schema["paths"]
    response_schema = schema["components"]["schemas"]["ModelProviderResponse"]
    properties = response_schema["properties"]
    assert set(properties) == {
        "id",
        "type",
        "display_name",
        "adapter",
        "credential_type",
        "default_model",
        "capabilities",
    }
    assert not {"api_key", "api_key_env", "base_url", "model_kwargs"} & set(properties)


async def test_create_model_credential_maps_provider_and_encrypts_secret(
    client,
    monkeypatch,
):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "openrouter": {
                    "display_name": "OpenRouter",
                    "adapter": "openrouter",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "base_url": "https://openrouter.ai/api/v1",
                    "default_model": "deepseek/deepseek-v4-pro",
                }
            }
        ),
    )
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    vault_response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Model keys"},
    )
    assert vault_response.status_code == 201, vault_response.text
    vault = vault_response.json()

    api_key = "sk-test-model-provider-secret"
    response = await client.post(
        f"/v1/vaults/{vault['id']}/model_credentials",
        headers=TEST_HEADERS,
        json={
            "provider": "OpenRouter",
            "api_key": api_key,
            "display_name": "Organization OpenRouter",
            "metadata": {"owner": "sdk-test"},
        },
    )

    assert response.status_code == 201, response.text
    credential = response.json()
    assert credential == {
        "id": credential["id"],
        "type": "model_credential",
        "vault_id": vault["id"],
        "model_provider": "openrouter",
        "display_name": "Organization OpenRouter",
        "metadata": {"owner": "sdk-test"},
        "created_at": credential["created_at"],
        "updated_at": credential["updated_at"],
        "archived_at": None,
    }
    assert api_key not in response.text
    assert "OPENROUTER_API_KEY" not in response.text
    assert "secret_name" not in response.text
    assert "auth" not in credential

    async with session_scope() as db:
        stored = await res_q.get_resource(
            db,
            resource_id=credential["id"],
            resource_type="credential",
        )
        assert stored is not None
        assert stored.parent_id == vault["id"]
        assert stored.data["model_provider"] == "openrouter"
        assert stored.data["auth"]["secret_name"] == "OPENROUTER_API_KEY"
        ciphertext = stored.data["auth"]["secret_value"]
        assert ciphertext.startswith(ENCRYPTED_PREFIX)
        assert decrypt_secret(ciphertext) == api_key

    rotated_key = "sk-test-model-provider-rotated"
    rotated_response = await client.post(
        f"/v1/vaults/{vault['id']}/model_credentials/{credential['id']}",
        headers=TEST_HEADERS,
        json={"api_key": rotated_key},
    )
    assert rotated_response.status_code == 200, rotated_response.text
    rotated_payload = rotated_response.json()
    assert rotated_payload["id"] == credential["id"]
    assert rotated_payload["vault_id"] == credential["vault_id"]
    assert rotated_payload["model_provider"] == credential["model_provider"]
    assert rotated_payload["display_name"] == credential["display_name"]
    assert rotated_payload["metadata"] == credential["metadata"]
    assert rotated_key not in rotated_response.text
    assert "OPENROUTER_API_KEY" not in rotated_response.text
    assert "secret_name" not in rotated_response.text
    assert "auth" not in rotated_response.json()

    async with session_scope() as db:
        rotated = await res_q.get_resource(
            db,
            resource_id=credential["id"],
            resource_type="credential",
        )
        assert rotated is not None
        assert decrypt_secret(rotated.data["auth"]["secret_value"]) == rotated_key


async def test_create_model_credential_reuses_vault_constraints(client, monkeypatch):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "openrouter": {
                    "adapter": "openrouter",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "default_model": "deepseek/deepseek-v4-pro",
                },
                "local": {
                    "adapter": "fake",
                    "default_model": "fake-model",
                },
            }
        ),
    )
    get_settings.cache_clear()
    vault_response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Model keys"},
    )
    assert vault_response.status_code == 201, vault_response.text
    vault = vault_response.json()
    endpoint = f"/v1/vaults/{vault['id']}/model_credentials"

    first = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "openrouter", "api_key": "first"},
    )
    assert first.status_code == 201, first.text

    duplicate = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "openrouter", "api_key": "second"},
    )
    assert duplicate.status_code == 409
    assert "openrouter" in duplicate.json()["error"]["message"]
    assert "OPENROUTER_API_KEY" not in duplicate.text
    assert "second" not in duplicate.text

    unknown = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "unknown", "api_key": "secret"},
    )
    assert unknown.status_code == 404

    no_api_key = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "local", "api_key": "secret"},
    )
    assert no_api_key.status_code == 422
    assert "does not accept" in no_api_key.json()["error"]["message"]

    blank = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "openrouter", "api_key": "   "},
    )
    assert blank.status_code == 422

    archive = await client.post(
        f"/v1/vaults/{vault['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert archive.status_code == 200, archive.text
    archived = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "deepseek", "api_key": "secret"},
    )
    assert archived.status_code == 409
    assert "archived" in archived.json()["error"]["message"].lower()


async def test_model_credential_lifecycle_is_typed_filtered_and_secret_free(
    client,
    monkeypatch,
):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "openrouter": {
                    "display_name": "OpenRouter",
                    "adapter": "openrouter",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "default_model": "deepseek/deepseek-v4-pro",
                }
            }
        ),
    )
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", base64.b64encode(b"1" * 32).decode())
    get_settings.cache_clear()
    vault_response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Lifecycle keys"},
    )
    vault = vault_response.json()
    endpoint = f"/v1/vaults/{vault['id']}/model_credentials"

    plaintext = "sk-model-lifecycle-secret"
    created_response = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "openrouter", "api_key": plaintext},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()

    generic_secret = "generic-mcp-secret"
    generic_response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Not a model Credential",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": generic_secret,
            },
        },
    )
    assert generic_response.status_code == 201, generic_response.text
    generic = generic_response.json()

    listed_response = await client.get(endpoint, headers=TEST_HEADERS)
    assert listed_response.status_code == 200, listed_response.text
    listed = listed_response.json()
    assert [item["id"] for item in listed["data"]] == [created["id"]]
    assert listed["first_id"] == listed["last_id"] == created["id"]
    assert listed["has_more"] is False

    retrieved_response = await client.get(
        f"{endpoint}/{created['id']}",
        headers=TEST_HEADERS,
    )
    assert retrieved_response.status_code == 200, retrieved_response.text
    retrieved = retrieved_response.json()
    assert {
        key: value
        for key, value in retrieved.items()
        if key not in {"created_at", "updated_at"}
    } == {
        key: value
        for key, value in created.items()
        if key not in {"created_at", "updated_at"}
    }

    generic_native_response = await client.get(
        f"{endpoint}/{generic['id']}",
        headers=TEST_HEADERS,
    )
    assert generic_native_response.status_code == 404

    generic_list_response = await client.get(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
    )
    assert generic_list_response.status_code == 200, generic_list_response.text
    assert [item["id"] for item in generic_list_response.json()["data"]] == [
        generic["id"]
    ]
    native_through_generic = await client.get(
        f"/v1/vaults/{vault['id']}/credentials/{created['id']}",
        headers=TEST_HEADERS,
    )
    assert native_through_generic.status_code == 404
    native_archive_through_generic = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{created['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert native_archive_through_generic.status_code == 404

    archive_response = await client.post(
        f"{endpoint}/{created['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert archive_response.status_code == 200, archive_response.text
    archived = archive_response.json()
    assert archived["id"] == created["id"]
    assert archived["type"] == "model_credential"
    assert archived["archived_at"] is not None

    active_list = await client.get(endpoint, headers=TEST_HEADERS)
    assert active_list.json()["data"] == []
    archived_list = await client.get(
        endpoint,
        headers=TEST_HEADERS,
        params={"include_archived": True},
    )
    assert [item["id"] for item in archived_list.json()["data"]] == [created["id"]]

    rotate_archived = await client.post(
        f"{endpoint}/{created['id']}",
        headers=TEST_HEADERS,
        json={"api_key": "must-not-be-stored"},
    )
    assert rotate_archived.status_code == 409

    replacement_secret = "sk-model-delete-secret"
    replacement_response = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"provider": "openrouter", "api_key": replacement_secret},
    )
    assert replacement_response.status_code == 201, replacement_response.text
    replacement = replacement_response.json()
    deleted_response = await client.delete(
        f"{endpoint}/{replacement['id']}",
        headers=TEST_HEADERS,
    )
    assert deleted_response.status_code == 200, deleted_response.text
    assert deleted_response.json() == {
        "id": replacement["id"],
        "type": "model_credential_deleted",
        "deleted": True,
    }

    native_serialized = " ".join(
        [
            listed_response.text,
            retrieved_response.text,
            archive_response.text,
            archived_list.text,
            deleted_response.text,
        ]
    )
    serialized = " ".join(
        [
            native_serialized,
            generic_list_response.text,
            native_through_generic.text,
            native_archive_through_generic.text,
        ]
    )
    for forbidden in (
        plaintext,
        replacement_secret,
        generic_secret,
        "OPENROUTER_API_KEY",
        "secret_name",
    ):
        assert forbidden not in serialized
    assert '"auth"' not in native_serialized

    async with session_scope() as db:
        archived_row = await res_q.get_resource(
            db,
            resource_id=created["id"],
            resource_type="credential",
        )
        deleted_row = await res_q.get_resource(
            db,
            resource_id=replacement["id"],
            resource_type="credential",
            include_deleted=True,
        )
        assert archived_row is not None
        assert archived_row.archived_at is not None
        assert archived_row.data["auth"]["secret_value"] is None
        assert deleted_row is not None
        assert deleted_row.deleted_at is not None
        assert deleted_row.data["auth"]["secret_value"] is None

    missing = await client.get(
        f"{endpoint}/{replacement['id']}",
        headers=TEST_HEADERS,
    )
    assert missing.status_code == 404
    generic_still_exists = await client.get(
        f"/v1/vaults/{vault['id']}/credentials/{generic['id']}",
        headers=TEST_HEADERS,
    )
    assert generic_still_exists.status_code == 200


async def test_model_credential_contract_is_in_openapi(client):
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"][
        "/v1/vaults/{vault_id}/model_credentials"
    ]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    request_schema = schema["components"]["schemas"][request_ref.rsplit("/", 1)[-1]]
    assert set(request_schema["properties"]) == {
        "provider",
        "api_key",
        "display_name",
        "metadata",
    }
    assert request_schema["properties"]["api_key"]["writeOnly"] is True
    response_ref = operation["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    response_schema = schema["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]
    assert set(response_schema["properties"]) == {
        "id",
        "type",
        "vault_id",
        "model_provider",
        "display_name",
        "metadata",
        "created_at",
        "updated_at",
        "archived_at",
    }
    assert not {"api_key", "secret_name", "auth"} & set(response_schema["properties"])

    list_operation = schema["paths"][
        "/v1/vaults/{vault_id}/model_credentials"
    ]["get"]
    list_response_ref = list_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    list_response_schema = schema["components"]["schemas"][
        list_response_ref.rsplit("/", 1)[-1]
    ]
    assert list_response_schema["properties"]["data"]["items"]["$ref"] == response_ref

    item_path = schema["paths"][
        "/v1/vaults/{vault_id}/model_credentials/{credential_id}"
    ]
    retrieve_response_ref = item_path["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert retrieve_response_ref == response_ref

    rotate_operation = schema["paths"][
        "/v1/vaults/{vault_id}/model_credentials/{credential_id}"
    ]["post"]
    rotate_request_ref = rotate_operation["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    rotate_request_schema = schema["components"]["schemas"][
        rotate_request_ref.rsplit("/", 1)[-1]
    ]
    assert set(rotate_request_schema["properties"]) == {"api_key"}
    assert rotate_request_schema["properties"]["api_key"]["writeOnly"] is True
    rotate_response_ref = rotate_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert rotate_response_ref == response_ref

    archive_response_ref = schema["paths"][
        "/v1/vaults/{vault_id}/model_credentials/{credential_id}/archive"
    ]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert archive_response_ref == response_ref

    delete_response_ref = item_path["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    delete_response_schema = schema["components"]["schemas"][
        delete_response_ref.rsplit("/", 1)[-1]
    ]
    assert set(delete_response_schema["properties"]) == {"id", "type", "deleted"}
