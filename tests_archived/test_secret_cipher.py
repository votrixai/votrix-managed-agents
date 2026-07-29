import base64

import pytest

from app.config import get_settings
from app.secret_cipher import (
    ENCRYPTED_PREFIX,
    decrypt_secret,
    decrypt_secret_values,
    encrypt_secret,
    encrypt_secret_values,
)
from tests.conftest import TEST_HEADERS

TEST_KEY = base64.b64encode(b"0" * 32).decode()


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", TEST_KEY)
    get_settings.cache_clear()
    yield


def test_encrypt_decrypt_round_trip(encryption_key):
    ciphertext = encrypt_secret("super-secret")
    assert ciphertext.startswith(ENCRYPTED_PREFIX)
    assert "super-secret" not in ciphertext
    assert decrypt_secret(ciphertext) == "super-secret"


def test_passthrough_without_key():
    assert encrypt_secret("super-secret") == "super-secret"
    assert decrypt_secret("super-secret") == "super-secret"


def test_secret_writes_fail_closed_in_production_without_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="VMA_ENCRYPTION_KEY"):
        encrypt_secret("super-secret")


def test_decrypt_encrypted_value_without_key_fails():
    with pytest.raises(ValueError, match="VMA_ENCRYPTION_KEY"):
        decrypt_secret(ENCRYPTED_PREFIX + "abcd")


def test_invalid_key_rejected(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", "not-base64!!")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="base64"):
        encrypt_secret("x")

    monkeypatch.setenv("VMA_ENCRYPTION_KEY", base64.b64encode(b"short").decode())
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="32 bytes"):
        encrypt_secret("x")


def test_walker_encrypts_only_nested_secret_fields(encryption_key):
    auth = {
        "type": "mcp_oauth",
        "mcp_server_url": "https://mcp.example.invalid",
        "access_token": "at-plain",
        "refresh": {
            "client_id": "client",
            "refresh_token": "rt-plain",
            "token_endpoint": "https://mcp.example.invalid/token",
            "token_endpoint_auth": {"type": "client_secret_basic", "client_secret": "cs-plain"},
        },
    }
    encrypted = encrypt_secret_values(auth)
    assert encrypted["type"] == "mcp_oauth"
    assert encrypted["mcp_server_url"] == "https://mcp.example.invalid"
    assert encrypted["refresh"]["client_id"] == "client"
    assert encrypted["access_token"].startswith(ENCRYPTED_PREFIX)
    assert encrypted["refresh"]["refresh_token"].startswith(ENCRYPTED_PREFIX)
    assert encrypted["refresh"]["token_endpoint_auth"]["client_secret"].startswith(ENCRYPTED_PREFIX)
    assert decrypt_secret_values(encrypted) == auth
    assert encrypt_secret_values(encrypted) == encrypted


async def test_credential_secrets_encrypted_at_rest(client, encryption_key):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "Enc Vault"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "enc-cred",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": "super-secret",
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()
    assert "token" not in credential["auth"]

    from app.db.engine import session_scope
    from app.db.queries import resources as res_q

    async with session_scope() as db:
        resource = await res_q.get_resource(db, resource_id=credential["id"], resource_type="credential")
        stored_token = resource.data["auth"]["token"]
        assert stored_token.startswith(ENCRYPTED_PREFIX)
        assert decrypt_secret(stored_token) == "super-secret"

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        headers=TEST_HEADERS,
        json={"display_name": "enc-cred-renamed"},
    )
    assert response.status_code == 200, response.text

    async with session_scope() as db:
        resource = await res_q.get_resource(db, resource_id=credential["id"], resource_type="credential")
        assert decrypt_secret(resource.data["auth"]["token"]) == "super-secret"
