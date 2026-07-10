import json
from urllib.parse import parse_qs, urlparse

from app.db.engine import session_scope
from app.db.queries import resources as res_q
from tests.conftest import TEST_HEADERS
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

    response = await client.delete(f"/v1/files/{file['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200
    assert response.json()["deleted"] is True


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
    assert second["storage"]["key"] == first["storage"]["key"]

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


async def test_file_complete_requires_current_workspace_staged_key(client):
    response = await client.post(
        "/v1/files/presign",
        headers=TEST_HEADERS,
        json={"filename": "staged.txt", "mime_type": "text/plain"},
    )
    assert response.status_code == 200, response.text
    staged = response.json()
    assert staged["key"].startswith("workspaces/wrkspc_default/")
    assert "/staged-uploads/" in staged["key"]

    response = await client.post(
        "/v1/files/complete",
        headers=TEST_HEADERS,
        json={
            "key": "workspaces/other/vma/staged-uploads/2026-01-01/obj_staged.txt",
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
        json={"name": "Customer memory"},
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": ["customers", "acme"], "content": "ACME prefers email."},
    )
    assert response.status_code == 201, response.text
    memory = response.json()
    assert memory["type"] == "memory"
    assert memory["path"] == "/customers/acme"
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


async def test_user_profile_enrollment_url_persists_hashed_token(client):
    response = await client.post(
        "/v1/user_profiles",
        headers=TEST_HEADERS,
        json={"relationship": "external", "external_id": "user-enroll"},
    )
    assert response.status_code == 201, response.text
    profile = response.json()

    response = await client.post(f"/v1/user_profiles/{profile['id']}/enrollment_url", headers=TEST_HEADERS)

    assert response.status_code == 200, response.text
    enrollment = response.json()
    assert enrollment["type"] == "enrollment_url"
    parsed = urlparse(enrollment["url"])
    token = parse_qs(parsed.query)["token"][0]
    assert parsed.path == f"/managed-agents/user-profiles/{profile['id']}/enroll"
    assert token
    assert enrollment["expires_at"]

    async with session_scope() as db:
        resources = await res_q.list_resources(
            db,
            resource_type="user_profile_enrollment",
            parent_id=profile["id"],
            limit=10,
        )

    assert len(resources) == 1
    stored = resources[0].data
    assert stored["user_profile_id"] == profile["id"]
    assert stored["token_hash"]
    assert token not in str(stored)


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
