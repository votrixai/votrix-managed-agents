import json

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from tests.conftest import TEST_HEADERS


async def _create_vault(client, name: str) -> dict:
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_generic_credential_api_reserves_native_model_type_fields(client):
    vault = await _create_vault(client, "Credential type boundary")
    endpoint = f"/v1/vaults/{vault['id']}/credentials"
    auth = {
        "type": "environment_variable",
        "secret_name": "SERVICE_API_KEY",
        "secret_value": "generic-secret",
        "networking": {"type": "unrestricted"},
    }

    for reserved_field, value in (
        ("credential_kind", "model_provider_api_key"),
        ("model_provider", "openrouter"),
    ):
        response = await client.post(
            endpoint,
            headers=TEST_HEADERS,
            json={
                "display_name": "Must use typed API",
                reserved_field: value,
                "auth": auth,
            },
        )
        assert response.status_code == 422, response.text
        assert "model_credentials API" in response.json()["error"]["message"]

    created = await client.post(
        endpoint,
        headers=TEST_HEADERS,
        json={"display_name": "Generic service key", "auth": auth},
    )
    assert created.status_code == 201, created.text

    update = await client.post(
        f"{endpoint}/{created.json()['id']}",
        headers=TEST_HEADERS,
        json={"model_provider": "openrouter"},
    )
    assert update.status_code == 422, update.text
    assert "model_credentials API" in update.json()["error"]["message"]


async def test_native_model_credentials_are_provider_scoped_but_legacy_rows_work(
    client,
    monkeypatch,
):
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        json.dumps(
            {
                "provider_a": {
                    "adapter": "openrouter",
                    "api_key_env": "SHARED_PROVIDER_API_KEY",
                    "default_model": "provider-a/model",
                },
                "provider_b": {
                    "adapter": "openrouter",
                    "api_key_env": "SHARED_PROVIDER_API_KEY",
                    "default_model": "provider-b/model",
                },
            }
        ),
    )
    get_settings.cache_clear()

    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Provider-scoped credential",
            "model": {"id": "provider-b/model", "provider": "provider_b"},
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "credential-types", "config": {"type": "cloud"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    environment = environment_response.json()

    typed_vault = await _create_vault(client, "Typed provider A")
    typed = await client.post(
        f"/v1/vaults/{typed_vault['id']}/model_credentials",
        headers=TEST_HEADERS,
        json={"provider": "provider_a", "api_key": "provider-a-key"},
    )
    assert typed.status_code == 201, typed.text
    async with session_scope() as db:
        stored = await res_q.get_resource(
            db,
            resource_id=typed.json()["id"],
            resource_type="credential",
        )
        assert stored is not None
        assert stored.data["credential_kind"] == "model_provider_api_key"
        assert stored.data["model_provider"] == "provider_a"

    mismatched = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent["id"],
            "environment_id": environment["id"],
            "vault_ids": [typed_vault["id"]],
        },
    )
    assert mismatched.status_code == 422, mismatched.text
    assert mismatched.json()["error"]["code"] == "model_credential_required"

    legacy_vault = await _create_vault(client, "Legacy generic key")
    legacy = await client.post(
        f"/v1/vaults/{legacy_vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Pre-typed compatibility row",
            "auth": {
                "type": "environment_variable",
                "secret_name": "SHARED_PROVIDER_API_KEY",
                "secret_value": "legacy-key",
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert legacy.status_code == 201, legacy.text

    compatible = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent["id"],
            "environment_id": environment["id"],
            "vault_ids": [legacy_vault["id"]],
        },
    )
    assert compatible.status_code == 201, compatible.text
    binding = compatible.json()["status_details"]["model_credential_binding"]
    assert binding["source"] == "vault"
    assert binding["credential_id"] == legacy.json()["id"]
