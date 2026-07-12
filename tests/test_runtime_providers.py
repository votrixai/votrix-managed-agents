from langchain_openrouter import ChatOpenRouter
from pydantic import SecretStr

from app.config import get_settings
from app.runtime.providers import build_chat_model, resolve_runtime_provider


def test_deepseek_reasoner_is_rejected_for_tool_harness(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    get_settings.cache_clear()

    provider = resolve_runtime_provider({"id": "deepseek-reasoner", "provider": "deepseek"})

    assert provider.adapter == "deepseek"
    assert provider.capabilities.tool_calls is False


def test_server_approved_openai_compatible_provider_uses_chat_completions(monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"gateway":{"adapter":"openai","api_key_env":"GATEWAY_TOKEN",'
        '"base_url":"https://models.example/v1","default_model":"vendor-model"}}',
    )
    get_settings.cache_clear()

    provider = resolve_runtime_provider({"provider": "gateway", "id": "vendor-model"})
    model = build_chat_model(provider)

    assert provider.base_url == "https://models.example/v1"
    assert provider.api_key == "gateway-secret"
    assert model.use_responses_api is False
    assert isinstance(model.openai_api_key, SecretStr)
    assert model.openai_api_key.get_secret_value() == "gateway-secret"


def test_openrouter_provider_uses_native_routing_controls(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"openrouter":{"adapter":"openrouter",'
        '"api_key_env":"OPENROUTER_API_KEY",'
        '"base_url":"https://openrouter.ai/api/v1",'
        '"default_model":"deepseek/deepseek-v4-pro",'
        '"model_kwargs":{"openrouter_provider":{'
        '"order":["fireworks","together"],'
        '"only":["fireworks","together"],'
        '"allow_fallbacks":true,"require_parameters":true,'
        '"data_collection":"deny"}}}}',
    )
    monkeypatch.setenv("VMA_DEFAULT_MODEL_PROVIDER", "openrouter")
    get_settings.cache_clear()

    provider = resolve_runtime_provider({})
    model = build_chat_model(provider)

    assert provider.provider == "openrouter"
    assert provider.adapter == "openrouter"
    assert provider.model_id == "deepseek/deepseek-v4-pro"
    assert isinstance(model, ChatOpenRouter)
    assert model.model_name == "deepseek/deepseek-v4-pro"
    assert model.route is None
    assert model.openrouter_provider == {
        "order": ["fireworks", "together"],
        "only": ["fireworks", "together"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert model._default_params["provider"] == model.openrouter_provider
    assert isinstance(model.openrouter_api_key, SecretStr)
    assert model.openrouter_api_key.get_secret_value() == "openrouter-secret"


def test_runtime_cannot_override_openrouter_profile_or_connection(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "server-secret")
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"openrouter":{"adapter":"openrouter",'
        '"api_key_env":"OPENROUTER_API_KEY",'
        '"base_url":"https://openrouter.ai/api/v1",'
        '"default_model":"deepseek/deepseek-v4-pro",'
        '"model_kwargs":{"openrouter_provider":{'
        '"order":["fireworks","together"],'
        '"only":["fireworks","together"],'
        '"allow_fallbacks":true,"require_parameters":true,'
        '"data_collection":"deny"}}}}',
    )
    monkeypatch.setenv("VMA_DEFAULT_MODEL_PROVIDER", "openrouter")
    get_settings.cache_clear()

    provider = resolve_runtime_provider(
        {},
        runtime={
            "model": {
                "api_key": "tenant-secret",
                "base_url": "https://attacker.invalid/v1",
                "openrouter_provider": {"only": ["deepseek"]},
            }
        },
    )

    assert provider.api_key == "server-secret"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model_kwargs["openrouter_provider"] == {
        "order": ["fireworks", "together"],
        "only": ["fireworks", "together"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
    }
