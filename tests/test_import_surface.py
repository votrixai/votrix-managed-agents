from pathlib import Path

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


def test_vma_settings_parse_tenant_and_provider_configuration(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("VMA_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("VMA_API_KEY_WORKSPACES", '{"key-a":"ws_a","key-b":"ws_b"}')
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"gateway":{"adapter":"openai","base_url":"https://models.example/v1"}}',
    )
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", "memory://")
    monkeypatch.setenv("VMA_ALLOW_UNSAFE_LOCAL_SANDBOX", "true")
    monkeypatch.setenv("VMA_SANDBOX_ROOT", "/tmp/vma-sandboxes")

    settings = Settings(_env_file=None)

    assert settings.vma_api_keys == ["key-a", "key-b"]
    assert settings.vma_api_key_workspaces == {"key-a": "ws_a", "key-b": "ws_b"}
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


async def test_default_auth_fails_closed_outside_local_mode(client, monkeypatch):
    from app.config import get_settings
    from tests.conftest import TEST_HEADERS

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("VMA_API_KEY", "")
    monkeypatch.setenv("VMA_API_KEYS", "")
    get_settings.cache_clear()

    response = await client.get("/v1/agents", headers=TEST_HEADERS)

    assert response.status_code == 401
    assert "not configured" in response.json()["error"]["message"]
