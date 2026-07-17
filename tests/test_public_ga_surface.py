from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.factory import create_app
from app.public_surface import is_public_ga_path
from tests.conftest import TEST_HEADERS


@pytest.fixture
def public_ga_app(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("VMA_PUBLIC_GA_ONLY", "true")
    get_settings.cache_clear()
    app = create_app()
    yield app
    get_settings.cache_clear()


async def test_public_ga_openapi_contains_only_allowed_paths(public_ga_app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_ga_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200, response.text
    schema = response.json()
    paths = set(schema["paths"])
    assert "/v1/agents" in paths
    assert "/v1/environments" in paths
    assert "/v1/sessions" in paths
    assert "/v1/files" in paths
    assert "/v1/skills" in paths
    assert "/v1/api_keys" in paths
    assert "/v1/usage" in paths
    assert paths
    assert all(is_public_ga_path(path) for path in paths)

    deferred_prefixes = (
        "/v1/deployments",
        "/v1/deployment_runs",
        "/v1/memory_stores",
        "/v1/user_profiles",
    )
    assert not any(path.startswith(deferred_prefixes) for path in paths)
    assert not any("/threads" in path or "/work" in path for path in paths)


async def test_public_capability_manifest_includes_platform_guarantees(public_ga_app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_ga_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/v1/capabilities")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["release_channel"] == "public_beta"
    assert body["platform_guarantees"]["tenant_scoped_api_keys"] == "ga"
    assert body["platform_guarantees"]["audit_and_usage_ledgers"] == "ga"


@pytest.mark.parametrize(
    "path",
    (
        "/v1/deployments",
        "/v1/memory_stores",
        "/v1/user_profiles",
        "/v1/sessions/sess_example/threads",
        "/v1/environments/env_example/work/next",
    ),
)
async def test_deferred_public_endpoints_return_capability_404(public_ga_app, path: str) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_ga_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "capability_not_available"
    assert response.json()["request_id"] == response.headers["request-id"]


async def test_public_ga_rejects_anthropic_system_skills(public_ga_app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_ga_app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={
                "name": "No private system packages",
                "model": {"id": "gpt-5.5"},
                "skills": [
                    {"type": "anthropic", "skill_id": "xlsx", "version": "latest"}
                ],
            },
        )

    assert response.status_code == 422, response.text
    assert "system skills are not available" in response.json()["error"]["message"]


async def test_public_ga_rejects_github_session_resources(public_ga_app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=public_ga_app),
        base_url="http://testserver",
    ) as client:
        agent_response = await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={"name": "Public agent", "model": {"id": "gpt-5.5"}},
        )
        assert agent_response.status_code == 201, agent_response.text
        agent = agent_response.json()

        environment_response = await client.post(
            "/v1/environments",
            headers=TEST_HEADERS,
            json={
                "name": "Public environment",
                "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
            },
        )
        assert environment_response.status_code == 201, environment_response.text
        environment = environment_response.json()

        response = await client.post(
            "/v1/sessions",
            headers=TEST_HEADERS,
            json={
                "agent": {
                    "type": "agent",
                    "id": agent["id"],
                    "version": agent["version"],
                },
                "environment_id": environment["id"],
                "resources": [
                    {
                        "type": "github_repository",
                        "url": "https://github.com/votrixai/private-repo",
                        "authorization_token": "must-not-be-stored",
                    }
                ],
            },
        )

    assert response.status_code == 422, response.text
    assert "github_repository resources are not available" in response.json()["error"]["message"]
