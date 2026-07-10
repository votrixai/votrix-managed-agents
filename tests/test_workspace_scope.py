from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.workspace import CurrentWorkspace
from votrix_managed_agents import AuthProvider, create_app
from tests.conftest import TEST_HEADERS


async def test_api_keys_scope_resources_to_workspaces(client, monkeypatch):
    monkeypatch.setenv("VMA_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("VMA_API_KEY_WORKSPACES", '{"key-a":"ws_a","key-b":"ws_b"}')
    get_settings.cache_clear()

    headers_a = {**TEST_HEADERS, "x-api-key": "key-a"}
    headers_b = {**TEST_HEADERS, "x-api-key": "key-b"}

    response = await client.post(
        "/v1/agents",
        headers=headers_a,
        json={"name": "Scoped Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.get("/v1/agents", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.get(f"/v1/agents/{agent['id']}", headers=headers_b)
    assert response.status_code == 404

    response = await client.post(
        "/v1/environments",
        headers=headers_a,
        json={"name": "shared-name", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text

    response = await client.post(
        "/v1/environments",
        headers=headers_b,
        json={"name": "shared-name", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text


async def test_single_api_key_authorizes_bearer_token(client, monkeypatch):
    monkeypatch.setenv("VMA_API_KEY", "single-key")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/agents",
        headers={**TEST_HEADERS, "authorization": "Bearer single-key"},
        json={"name": "Single Key Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text

    response = await client.get(
        "/v1/agents",
        headers={**TEST_HEADERS, "authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


async def test_api_keys_scope_generic_resource_families_to_workspaces(client, monkeypatch):
    monkeypatch.setenv("VMA_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("VMA_API_KEY_WORKSPACES", '{"key-a":"ws_a","key-b":"ws_b"}')
    get_settings.cache_clear()

    headers_a = {**TEST_HEADERS, "x-api-key": "key-a"}
    headers_b = {**TEST_HEADERS, "x-api-key": "key-b"}

    response = await client.post("/v1/vaults", headers=headers_a, json={"display_name": "Scoped Vault"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=headers_a,
        json={
            "display_name": "Scoped Credential",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": "secret",
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    response = await client.post("/v1/memory_stores", headers=headers_a, json={"name": "Scoped Memory"})
    assert response.status_code == 201, response.text
    memory_store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{memory_store['id']}/memories",
        headers=headers_a,
        json={"path": "/private/note.md", "content": "workspace scoped"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.post(
        "/v1/user_profiles",
        headers=headers_a,
        json={"relationship": "external", "external_id": "scoped-user"},
    )
    assert response.status_code == 201, response.text
    user_profile = response.json()

    response = await client.post(
        "/v1/skills",
        headers=headers_a,
        data={"display_title": "Scoped Skill"},
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: scoped\ndescription: Scoped skill.\n---\nUse it.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    skill = response.json()

    response = await client.post(
        "/v1/files",
        headers=headers_a,
        files={"file": ("scoped.txt", b"scoped", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()

    response = await client.get("/v1/vaults", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    for path in [
        f"/v1/vaults/{vault['id']}",
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}",
        f"/v1/memory_stores/{memory_store['id']}",
        f"/v1/memory_stores/{memory_store['id']}/memories/{memory['id']}",
        f"/v1/user_profiles/{user_profile['id']}",
        f"/v1/skills/{skill['id']}",
        f"/v1/files/{file['id']}",
    ]:
        response = await client.get(path, headers=headers_b)
        assert response.status_code == 404, f"{path}: {response.text}"

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=headers_b,
        json={
            "display_name": "Cross Workspace Credential",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.invalid",
                "token": "secret",
            },
        },
    )
    assert response.status_code == 404

    response = await client.get("/v1/skills", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.get("/v1/files", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.post(
        "/v1/agents",
        headers=headers_b,
        json={"name": "Workspace B Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent_b = response.json()

    response = await client.post(
        "/v1/environments",
        headers=headers_b,
        json={"name": "workspace-b-env", "config": {"type": "self_hosted"}},
    )
    assert response.status_code == 201, response.text
    environment_b = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=headers_b,
        json={
            "agent": {"type": "agent", "id": agent_b["id"], "version": 1},
            "environment_id": environment_b["id"],
            "vault_ids": [vault["id"]],
        },
    )
    assert response.status_code == 404


async def test_api_keys_scope_deployments_and_runs_to_workspaces(client, monkeypatch):
    monkeypatch.setenv("VMA_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("VMA_API_KEY_WORKSPACES", '{"key-a":"ws_a","key-b":"ws_b"}')
    get_settings.cache_clear()

    headers_a = {**TEST_HEADERS, "x-api-key": "key-a"}
    headers_b = {**TEST_HEADERS, "x-api-key": "key-b"}

    response = await client.post(
        "/v1/agents",
        headers=headers_a,
        json={"name": "Deployment Scope Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=headers_a,
        json={"name": "deployment-scope-env", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/deployments",
        headers=headers_a,
        json={
            "name": "Scoped Deployment",
            "agent": {"id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "initial_events": [{"type": "user.message", "content": "scope check"}],
        },
    )
    assert response.status_code == 201, response.text
    deployment = response.json()

    response = await client.post(f"/v1/deployments/{deployment['id']}/run", headers=headers_a)
    assert response.status_code == 200, response.text
    deployment_run = response.json()

    response = await client.get("/v1/deployments", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.get(f"/v1/deployments/{deployment['id']}", headers=headers_b)
    assert response.status_code == 404

    response = await client.post(f"/v1/deployments/{deployment['id']}/run", headers=headers_b)
    assert response.status_code == 404

    response = await client.get("/v1/deployment_runs", headers=headers_b)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.get(f"/v1/deployment_runs/{deployment_run['id']}", headers=headers_b)
    assert response.status_code == 404


async def test_create_app_accepts_hosted_auth_provider(monkeypatch):
    class HostedAuthProvider:
        async def authenticate(self, request, credentials):
            return CurrentWorkspace(id="ws_hosted", slug="hosted", source="hosted_test")

    assert isinstance(HostedAuthProvider(), AuthProvider)

    monkeypatch.delenv("VMA_API_KEY", raising=False)
    monkeypatch.delenv("VMA_API_KEYS", raising=False)
    get_settings.cache_clear()

    app = create_app(auth_provider=HostedAuthProvider())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={"name": "Hosted Agent", "model": {"id": "gpt-5.5"}},
        )
        assert response.status_code == 201, response.text

        response = await client.get("/v1/agents", headers=TEST_HEADERS)
        assert response.status_code == 200, response.text
        assert [item["name"] for item in response.json()["data"]] == ["Hosted Agent"]
