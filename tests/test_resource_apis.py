import json
from datetime import datetime, timezone

import pytest

from app.db.engine import session_scope
from app.db.models import ManagedResource
from app.db.queries import resources as res_q
from tests.conftest import TEST_HEADERS, TEST_ORGANIZATION_ID
from app.config import get_settings


async def test_post_update_alias_matches_official_sdk_shape(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Alias Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "description": "updated via POST"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["description"] == "updated via POST"


async def test_files_upload_download_delete(client):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()
    assert file["type"] == "file"
    assert file["filename"] == "hello.txt"
    assert file["size_bytes"] == 11

    response = await client.get(f"/v1/files/{file['id']}/content", headers=TEST_HEADERS)
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    response = await client.delete(f"/v1/files/{file['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200
    assert response.json()["deleted"] is True


async def test_mounted_session_file_copy_cannot_be_deleted_directly(client):
    agent = (
        await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={"name": "Mounted File Guard", "model": {"id": "gpt-5.5"}},
        )
    ).json()
    environment = (
        await client.post(
            "/v1/environments",
            headers=TEST_HEADERS,
            json={"name": "Mounted File Guard", "config": {"type": "cloud"}},
        )
    ).json()
    uploaded = (
        await client.post(
            "/v1/files",
            headers=TEST_HEADERS,
            files={"file": ("sealed.txt", b"sealed bytes", "text/plain")},
        )
    ).json()
    session = (
        await client.post(
            "/v1/sessions",
            headers=TEST_HEADERS,
            json={"agent": agent["id"], "environment_id": environment["id"]},
        )
    ).json()
    mounted = await client.post(
        f"/v1/sessions/{session['id']}/resources",
        headers=TEST_HEADERS,
        json={
            "type": "file",
            "file_id": uploaded["id"],
            "mount_path": "/mnt/session/uploads/sealed.txt",
        },
    )
    assert mounted.status_code == 201, mounted.text
    scoped_file_id = mounted.json()["file_id"]

    deletion = await client.delete(
        f"/v1/files/{scoped_file_id}",
        headers=TEST_HEADERS,
    )
    assert deletion.status_code == 409, deletion.text
    assert "mounted by an active Session resource" in deletion.text
    download = await client.get(
        f"/v1/files/{scoped_file_id}/content",
        headers=TEST_HEADERS,
    )
    assert download.content == b"sealed bytes"

    session_deletion = await client.delete(
        f"/v1/sessions/{session['id']}",
        headers=TEST_HEADERS,
    )
    assert session_deletion.status_code == 200, session_deletion.text
    deletion = await client.delete(
        f"/v1/files/{scoped_file_id}",
        headers=TEST_HEADERS,
    )
    assert deletion.status_code == 200, deletion.text


async def test_duplicate_file_uploads_share_object_until_last_reference_is_deleted(client):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("first.txt", b"same bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    first = response.json()

    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("second.txt", b"same bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    second = response.json()

    assert second["deduplicated_from_file_id"] == first["id"]
    assert "storage" not in first
    assert "storage" not in second

    response = await client.delete(f"/v1/files/{first['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/files/{second['id']}/content", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.content == b"same bytes"

    response = await client.delete(f"/v1/files/{second['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/files/{second['id']}/content", headers=TEST_HEADERS)
    assert response.status_code == 404


async def test_file_upload_size_limit(client, monkeypatch):
    monkeypatch.setenv("VMA_MAX_FILE_UPLOAD_BYTES", "4")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("too-big.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 413
    assert "maximum size" in response.json()["error"]["message"]


async def test_file_upload_content_scan_rejects_eicar_signature(client):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={
            "file": (
                "eicar.txt",
                b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
                "text/plain",
            )
        },
    )

    assert response.status_code == 422
    assert "content scan" in response.json()["error"]["message"]


async def test_file_complete_requires_current_organization_staged_key(client):
    response = await client.post(
        "/v1/files/presign",
        headers=TEST_HEADERS,
        json={"filename": "staged.txt", "mime_type": "text/plain"},
    )
    assert response.status_code == 200, response.text
    staged = response.json()
    assert staged["key"].startswith(f"organizations/{TEST_ORGANIZATION_ID}/")
    assert "/staged-uploads/" in staged["key"]

    response = await client.post(
        "/v1/files/complete",
        headers=TEST_HEADERS,
        json={
            "key": "organizations/other/vma/staged-uploads/2026-01-01/obj_staged.txt",
            "filename": "bad.txt",
        },
    )
    assert response.status_code == 422

    response = await client.post(
        "/v1/files/complete",
        headers=TEST_HEADERS,
        json={"key": staged["key"], "filename": "staged.txt", "mime_type": "text/plain"},
    )
    assert response.status_code == 201, response.text
    completed = response.json()
    assert completed["type"] == "file"
    assert completed["filename"] == "staged.txt"


async def test_file_complete_enforces_actual_staged_object_size(client, monkeypatch):
    import app.routers.files as files_router

    response = await client.post(
        "/v1/files/presign",
        headers=TEST_HEADERS,
        json={"filename": "claimed-small.txt", "mime_type": "text/plain"},
    )
    assert response.status_code == 200, response.text
    key = response.json()["key"]

    async def false_head(_key):
        return {"ContentLength": 1, "ContentType": "text/plain"}

    async def actual_download(_key):
        return b"five!", "text/plain"

    monkeypatch.setattr(files_router, "get_file_info", false_head)
    monkeypatch.setattr(files_router, "download_file_with_type", actual_download)
    monkeypatch.setenv("VMA_MAX_FILE_UPLOAD_BYTES", "4")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/files/complete",
        headers=TEST_HEADERS,
        json={
            "key": key,
            "filename": "claimed-small.txt",
            "size_bytes": 1,
        },
    )
    assert response.status_code == 413, response.text
    assert "maximum size" in response.text


async def test_skill_create_version_and_download(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        data={"display_title": "Research Skill"},
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: research\ndescription: Use sources.\n---\nUse sources.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["type"] == "skill"
    first_version = skill["latest_version"]
    assert first_version.isdigit()
    assert len(first_version) >= 16

    response = await client.post(
        f"/v1/skills/{skill['id']}/versions",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: research\ndescription: Use sources.\n---\nUpdated.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    second_version = response.json()["version"]
    assert second_version.isdigit()
    assert int(second_version) > int(first_version)

    response = await client.get(f"/v1/skills/{skill['id']}/versions/{second_version}/content", headers=TEST_HEADERS)
    assert response.status_code == 200
    assert b"Updated" in response.content


async def test_skill_upload_content_scan_rejects_eicar_signature(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        data={"display_title": "Unsafe Skill"},
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: unsafe\ndescription: Unsafe skill.\n---\nEICAR-STANDARD-ANTIVIRUS-TEST-FILE",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 422
    assert "content scan" in response.json()["error"]["message"]


async def test_generic_resource_metadata_limits_are_enforced(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Too Much Metadata", "metadata": {f"k{index}": "v" for index in range(17)}},
    )
    assert response.status_code == 422

    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={
            "name": "Limited Metadata Store",
            "metadata": {f"k{index}": "v" for index in range(16)},
        },
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=TEST_HEADERS,
        json={"metadata": {"extra": "v"}},
    )
    assert response.status_code == 422


async def test_vault_and_credential_display_name_validation(client):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={})
    assert response.status_code == 422
    assert "display_name" in response.json()["error"]["message"]

    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "x" * 256})
    assert response.status_code == 422
    assert "255" in response.json()["error"]["message"]

    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "Name Validation"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "x" * 256,
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": "secret-token",
            },
        },
    )
    assert response.status_code == 422
    assert "255" in response.json()["error"]["message"]


async def test_vault_credentials_memory_and_deployment_metadata(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"name": "Main Vault"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "linear",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "secret-token",
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()
    assert credential["type"] == "vault_credential"
    assert credential["vault_id"] == vault["id"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Vault Session Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "vault-session-env", "config": {"type": "self_hosted"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "vault_ids": [vault["id"], vault["id"]],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["vault_ids"] == [vault["id"]]

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "vault_ids": ["vault_missing"],
        },
    )
    assert response.status_code == 404


    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "Organization memory"},
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": ["accounts", "acme"], "content": "ACME prefers email."},
    )
    assert response.status_code == 201, response.text
    memory = response.json()
    assert memory["type"] == "memory"
    assert memory["path"] == "/accounts/acme"
    assert memory["content_sha256"]

    response = await client.post(
        "/v1/deployments",
        headers=TEST_HEADERS,
        json={
            "name": "Daily report",
            "agent": {"id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "initial_events": [{"type": "user.message", "content": "Run report."}],
        },
    )
    assert response.status_code == 201, response.text
    deployment = response.json()

    response = await client.post(f"/v1/deployments/{deployment['id']}/run", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "deployment_run"


async def test_vault_credential_auth_validation_and_redaction(client):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "Credential Vault"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={"display_name": "bad", "auth": {"type": "api_key", "token": "secret"}},
    )
    assert response.status_code == 422
    assert "auth type" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "oauth",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "access-secret",
                "refresh": {
                    "client_id": "client-1",
                    "refresh_token": "refresh-secret",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {
                        "type": "client_secret_basic",
                        "client_secret": "client-secret",
                    },
                    "scope": "read write",
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()
    assert "access-secret" not in str(credential)
    assert "refresh-secret" not in str(credential)
    assert "client-secret" not in str(credential)
    assert credential["auth"]["refresh"] == {
        "client_id": "client-1",
        "token_endpoint": "https://auth.example.invalid/token",
        "token_endpoint_auth": {"type": "client_secret_basic"},
        "scope": "read write",
    }

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "env",
            "auth": {
                "type": "environment_variable",
                "secret_name": "SDK_TOKEN",
                "secret_value": "env-secret",
                "networking": {"type": "limited", "allowed_hosts": ["api.example.invalid"]},
            },
        },
    )
    assert response.status_code == 201, response.text
    env_credential = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{env_credential['id']}",
        headers=TEST_HEADERS,
        json={"auth": {"type": "environment_variable", "networking": {"type": "unrestricted"}}},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["auth"]["secret_name"] == "SDK_TOKEN"
    assert updated["auth"]["networking"] == {"type": "unrestricted"}
    assert "env-secret" not in str(updated)


async def test_vault_credential_keys_are_unique_required_and_immutable(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Credential Constraints"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Missing value",
            "auth": {
                "type": "environment_variable",
                "secret_name": "OPENROUTER_API_KEY",
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 422, response.text
    assert "secret_value" in response.json()["error"]["message"]

    payload = {
        "display_name": "OpenRouter",
        "auth": {
            "type": "environment_variable",
            "secret_name": "OPENROUTER_API_KEY",
            "secret_value": "first-key",
            "networking": {"type": "unrestricted"},
        },
    }
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json=payload,
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={**payload, "display_name": "Duplicate", "auth": {**payload["auth"], "secret_value": "second-key"}},
    )
    assert response.status_code == 409, response.text

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        headers=TEST_HEADERS,
        json={
            "auth": {
                "type": "environment_variable",
                "secret_name": "DEEPSEEK_API_KEY",
                "secret_value": "rotated-key",
            }
        },
    )
    assert response.status_code == 422, response.text
    assert "immutable" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={**payload, "display_name": "Replacement"},
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "auth,expected_field",
    [
        (
            {
                "type": "environment_variable",
                "secret_name": "OPENROUTER_API_KEY",
                "secret_value": "   ",
                "networking": {"type": "unrestricted"},
            },
            "secret_value",
        ),
        (
            {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": "\t",
            },
            "token",
        ),
        (
            {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "\n",
            },
            "access_token",
        ),
    ],
)
async def test_vault_credential_create_rejects_blank_required_secrets(client, auth, expected_field):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": f"Blank {expected_field}"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={"display_name": "Blank secret", "auth": auth},
    )
    assert response.status_code == 422, response.text
    assert expected_field in response.json()["error"]["message"]


async def test_vault_credential_create_requires_explicit_nonempty_secret(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Strict secret Vault"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={"display_name": "Legacy missing auth", "api_key": "ignored-legacy-secret"},
    )
    assert response.status_code == 422, response.text
    assert "access_token" in response.json()["error"]["message"]


@pytest.mark.parametrize("auth_type", ["client_secret_basic", "client_secret_post"])
async def test_vault_oauth_client_secret_modes_require_client_secret(client, auth_type):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": f"OAuth {auth_type}"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "OAuth missing client secret",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "access-secret",
                "refresh": {
                    "client_id": "client-1",
                    "refresh_token": "refresh-secret",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {"type": auth_type},
                },
            },
        },
    )
    assert response.status_code == 422, response.text
    assert "client_secret" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "OAuth blank client secret",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://blank-mcp.example.invalid",
                "access_token": "access-secret",
                "refresh": {
                    "client_id": "client-1",
                    "refresh_token": "refresh-secret",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {"type": auth_type, "client_secret": "  "},
                },
            },
        },
    )
    assert response.status_code == 422, response.text
    assert "client_secret" in response.json()["error"]["message"]


async def test_vault_supports_at_most_twenty_active_credentials(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Capacity Vault"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    for index in range(20):
        response = await client.post(
            f"/v1/vaults/{vault['id']}/credentials",
            headers=TEST_HEADERS,
            json={
                "display_name": f"Key {index}",
                "auth": {
                    "type": "environment_variable",
                    "secret_name": f"PROVIDER_KEY_{index}",
                    "secret_value": f"secret-{index}",
                    "networking": {"type": "unrestricted"},
                },
            },
        )
        assert response.status_code == 201, response.text

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Overflow",
            "auth": {
                "type": "environment_variable",
                "secret_name": "PROVIDER_KEY_OVERFLOW",
                "secret_value": "overflow-secret",
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 400, response.text
    assert "20" in response.json()["error"]["message"]


async def test_credential_mutations_lock_parent_before_credential(client, monkeypatch):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Lock order Vault"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Lock order Credential",
            "auth": {
                "type": "environment_variable",
                "secret_name": "LOCK_ORDER_KEY",
                "secret_value": "secret",
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    original_get_resource = res_q.get_resource
    lock_calls: list[tuple[str | None, str]] = []

    async def recording_get_resource(*args, **kwargs):
        if kwargs.get("for_update"):
            lock_calls.append((kwargs.get("resource_type"), kwargs["resource_id"]))
        return await original_get_resource(*args, **kwargs)

    monkeypatch.setattr(res_q, "get_resource", recording_get_resource)

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Delete lock order Credential",
            "auth": {
                "type": "environment_variable",
                "secret_name": "DELETE_LOCK_ORDER_KEY",
                "secret_value": "secret",
                "networking": {"type": "unrestricted"},
            },
        },
    )
    assert response.status_code == 201, response.text
    deletable_credential = response.json()
    assert lock_calls == [("vault", vault["id"])]

    lock_calls.clear()
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        headers=TEST_HEADERS,
        json={"display_name": "Updated"},
    )
    assert response.status_code == 200, response.text
    assert lock_calls == [("vault", vault["id"]), ("credential", credential["id"])]

    lock_calls.clear()
    response = await client.delete(
        f"/v1/vaults/{vault['id']}/credentials/{deletable_credential['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert lock_calls == [("vault", vault["id"]), ("credential", deletable_credential["id"])]

    lock_calls.clear()
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}/mcp_oauth_validate",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert lock_calls == [("vault", vault["id"]), ("credential", credential["id"])]

    lock_calls.clear()
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert lock_calls == [("vault", vault["id"]), ("credential", credential["id"])]


async def test_vault_delete_cascades_more_than_one_thousand_credentials(client):
    response = await client.post(
        "/v1/vaults",
        headers=TEST_HEADERS,
        json={"display_name": "Unbounded cascade Vault"},
    )
    assert response.status_code == 201, response.text
    vault = response.json()

    archived_at = datetime.now(timezone.utc)
    credential_count = 1005
    async with session_scope() as db:
        db.add_all(
            [
                ManagedResource(
                    id=f"cred_unbounded_{index:04d}",
                    organization_id=TEST_ORGANIZATION_ID,
                    resource_type="credential",
                    parent_id=vault["id"],
                    name=f"Archived {index}",
                    status="archived",
                    archived_at=archived_at,
                    data={
                        "auth": {
                            "type": "environment_variable",
                            "secret_name": f"ARCHIVED_KEY_{index}",
                            "secret_value": f"secret-{index}",
                            "networking": {"type": "unrestricted"},
                        }
                    },
                )
                for index in range(credential_count)
            ]
        )
        await db.commit()

    response = await client.delete(f"/v1/vaults/{vault['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text

    async with session_scope() as db:
        credentials = await res_q.list_resources(
            db,
            resource_type="credential",
            parent_id=vault["id"],
            limit=credential_count + 1,
            include_archived=True,
            include_deleted=True,
        )
    assert len(credentials) == credential_count
    assert all(credential.deleted_at is not None for credential in credentials)
    assert all(credential.data["auth"]["secret_value"] is None for credential in credentials)


async def test_vault_credential_validation_is_persisted_in_metadata(client):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "MCP Vault"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Linear MCP",
            "metadata": {"team": "platform"},
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "secret-access-token",
                "refresh": {
                    "client_id": "client-1",
                    "refresh_token": "secret-refresh-token",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {"type": "none"},
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}/mcp_oauth_validate",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    validation = response.json()
    assert validation["type"] == "vault_credential_validation"
    assert validation["status"] == "unknown"
    assert validation["has_refresh_token"] is True

    response = await client.get(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["metadata"]["team"] == "platform"
    last_validation = json.loads(updated["metadata"]["last_validation"])
    assert last_validation["credential_id"] == credential["id"]
    assert last_validation["status"] == "unknown"
    assert last_validation["has_refresh_token"] is True
    assert "secret" not in str(last_validation)


async def test_user_profile_relationship_validation(client):
    response = await client.post(
        "/v1/user_profiles",
        headers=TEST_HEADERS,
        json={"relationship": "partner", "external_id": "user-invalid"},
    )
    assert response.status_code == 422
    assert "relationship" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/user_profiles",
        headers=TEST_HEADERS,
        json={"relationship": "resold", "external_id": "company-missing-name"},
    )
    assert response.status_code == 422
    assert "resold" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/user_profiles",
        headers=TEST_HEADERS,
        json={"relationship": "resold", "external_id": "company-1", "name": "Acme Inc"},
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["relationship"] == "resold"
    assert profile["name"] == "Acme Inc"

    response = await client.post(
        f"/v1/user_profiles/{profile['id']}",
        headers=TEST_HEADERS,
        json={"name": ""},
    )
    assert response.status_code == 422
    assert "resold" in response.json()["error"]["message"]


async def test_user_profile_field_length_validation(client):
    response = await client.post(
        "/v1/user_profiles",
        headers=TEST_HEADERS,
        json={"relationship": "external", "external_id": "x" * 256},
    )

    assert response.status_code == 422
    assert "external_id" in response.json()["error"]["message"]
