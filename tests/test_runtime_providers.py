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
