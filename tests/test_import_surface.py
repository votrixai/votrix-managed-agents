from pathlib import Path

import pytest
from fastapi import FastAPI


def test_votrix_managed_agents_exports_app_factory():
    from votrix_managed_agents import CurrentWorkspace, create_app

    app = create_app(auth_provider=_HostedAuthProvider())

    assert isinstance(app, FastAPI)
    assert CurrentWorkspace(id="ws_test").id == "ws_test"


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
    monkeypatch.setenv("VMA_ALLOW_UNSAFE_LOCAL_SANDBOX", "true")
    monkeypatch.setenv("VMA_SANDBOX_ROOT", "/tmp/vma-sandboxes")

    settings = Settings(_env_file=None)

    assert settings.vma_model_providers["gateway"]["adapter"] == "openai"
    assert settings.vma_checkpoint_database_url == "memory://"
    assert settings.vma_allow_unsafe_local_sandbox is True
    assert settings.vma_sandbox_root == "/tmp/vma-sandboxes"


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
        from votrix_managed_agents import CurrentWorkspace

        return CurrentWorkspace(id="ws_test", slug="test", source="test")


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
        "VMA_API_KEY_WORKSPACES",
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
