from contextvars import Context
from pathlib import Path

import pytest
from fastapi import FastAPI


def test_votrix_managed_agents_exports_app_factory():
    from votrix_managed_agents import CurrentOrganization, create_app

    app = create_app(auth_provider=_HostedAuthProvider())

    assert isinstance(app, FastAPI)
    assert CurrentOrganization(id="org_test").id == "org_test"


def test_organization_context_has_no_implicit_fallback():
    from app.organization import MissingOrganizationContextError, current_organization

    with pytest.raises(MissingOrganizationContextError, match="Organization context"):
        Context().run(current_organization)


@pytest.mark.parametrize(
    "organization_id",
    ["tenant", "org_", "org_bad/path", "org_" + "default"],
)
def test_current_organization_rejects_invalid_or_reserved_ids(organization_id: str):
    from app.organization import CurrentOrganization

    with pytest.raises(ValueError, match="organization_id"):
        CurrentOrganization(id=organization_id)


def test_app_factory_loads_local_dotenv(monkeypatch):
    from app import factory

    calls = []
    monkeypatch.setattr(factory, "load_dotenv", lambda: calls.append(True))

    factory.create_app(auth_provider=_HostedAuthProvider())

    assert calls == [True]


def test_asgi_entrypoint_exposes_app():
    from app.main import app

    assert isinstance(app, FastAPI)


def test_vma_settings_parse_provider_configuration(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"gateway":{"adapter":"openai","base_url":"https://models.example/v1"}}',
    )
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "memory://")

    settings = Settings(_env_file=None)

    assert settings.vma_model_providers["gateway"]["adapter"] == "openai"
    assert settings.vma_checkpoint_database_url == "memory://"


def test_core_does_not_import_anthropic_sdk():
    repo_root = Path(__file__).resolve().parents[1]
    offenders = []
    for package in ("app", "votrix_managed_agents"):
        for path in (repo_root / package).rglob("*.py"):
            text = path.read_text()
            if "import anthropic" in text or "from anthropic" in text:
                offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []


class _HostedAuthProvider:
    async def authenticate(self, request, credentials):
        from votrix_managed_agents import CurrentOrganization

        return CurrentOrganization(id="org_test", slug="test", source="test")


@pytest.mark.parametrize("app_env", ["local", "test", "production"])
async def test_default_auth_uses_database_keys_and_ignores_legacy_environment_auth(
    client,
    monkeypatch,
    app_env,
):
    from app.config import get_settings
    from tests.conftest import TEST_HEADERS, UNAUTHENTICATED_TEST_HEADERS

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("VMA_API_KEY", "legacy-single-key")
    monkeypatch.setenv("VMA_API_KEYS", "legacy-list-key")
    monkeypatch.setenv(
        "VMA_API_KEY_ORGANIZATIONS",
        '{"legacy-single-key":"legacy_ws","legacy-list-key":"legacy_ws"}',
    )
    monkeypatch.setenv("VMA_ALLOW_ANONYMOUS_LOCAL", "true")
    get_settings.cache_clear()

    missing = await client.get("/v1/agents", headers=UNAUTHENTICATED_TEST_HEADERS)
    assert missing.status_code == 401

    for token in ("legacy-single-key", "legacy-list-key"):
        legacy = await client.get(
            "/v1/agents",
            headers={**UNAUTHENTICATED_TEST_HEADERS, "x-api-key": token},
        )
        assert legacy.status_code == 401

    authenticated = await client.get("/v1/agents", headers=TEST_HEADERS)
    assert authenticated.status_code == 200, authenticated.text
