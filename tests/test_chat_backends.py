"""An Account credential selects one explicit LangChain chat adapter."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.models.llm import MODEL_CATALOG
from app.runtime.chat_backends import (
    DIRECT_MODEL_IDS,
    UnsupportedModelBackendError,
    UnsupportedThinkingError,
    build_chat_model,
    validate_model_for_backend,
)
from app.services.account_credentials import ResolvedAccountCredential


def credential(backend: str) -> ResolvedAccountCredential:
    return ResolvedAccountCredential(
        account_id="acct_test",
        funding_mode="platform" if backend == "openrouter" else "byok",
        backend=backend,
        api_key=SecretStr("provider-key"),
    )


@pytest.mark.parametrize(
    ("backend", "model", "class_name"),
    [
        ("openrouter", "claude-opus-5", "ChatOpenRouter"),
        ("anthropic", "claude-opus-5", "ChatAnthropic"),
        ("openai", "gpt-5.6-sol", "ChatOpenAI"),
        ("google", "gemini-3.6-flash", "ChatGoogleGenerativeAI"),
        ("deepseek", "deepseek-v4-pro", "ChatDeepSeek"),
    ],
)
def test_each_backend_builds_only_its_native_adapter(backend, model, class_name):
    built = build_chat_model(
        {"id": model},
        credential=credential(backend),
        session_id="sess_test",
    )

    assert type(built).__name__ == class_name


def test_every_direct_catalog_model_has_an_explicit_native_id():
    missing = [
        model.id
        for model in MODEL_CATALOG
        if model.id not in DIRECT_MODEL_IDS.get(model.provider, {})
    ]
    stale = [
        model_id
        for mapping in DIRECT_MODEL_IDS.values()
        for model_id in mapping
        if model_id not in {model.id for model in MODEL_CATALOG}
    ]

    assert missing == []
    assert stale == []


def test_direct_key_cannot_silently_route_a_different_vendors_model():
    with pytest.raises(UnsupportedModelBackendError) as raised:
        build_chat_model(
            {"id": "gpt-5.6-sol"},
            credential=credential("anthropic"),
            session_id="sess_test",
        )

    assert "choose a model served by anthropic or an OpenRouter Account" in str(
        raised.value
    )


def test_native_thinking_controls_are_translated_per_backend():
    anthropic = build_chat_model(
        {"id": "claude-opus-5", "thinking": "high"},
        credential=credential("anthropic"),
        session_id="sess_test",
    )
    openai = build_chat_model(
        {"id": "gpt-5.6-sol", "thinking": "high"},
        credential=credential("openai"),
        session_id="sess_test",
    )
    google = build_chat_model(
        {"id": "gemini-3.6-flash", "thinking": "none"},
        credential=credential("google"),
        session_id="sess_test",
    )
    deepseek = build_chat_model(
        {"id": "deepseek-v4-pro", "thinking": "none"},
        credential=credential("deepseek"),
        session_id="sess_test",
    )

    assert anthropic.thinking == {"type": "adaptive"}
    assert anthropic.effort == "high"
    assert openai.reasoning_effort == "high"
    assert google.thinking_level == "minimal"
    assert deepseek.extra_body == {"thinking": {"type": "disabled"}}


def test_google_pro_refuses_a_thinking_setting_it_cannot_honor():
    with pytest.raises(UnsupportedThinkingError):
        validate_model_for_backend(
            {"id": "gemini-3.1-pro-preview", "thinking": "none"},
            backend="google",
        )


def test_openrouter_remains_the_all_catalog_platform_backend():
    for model in MODEL_CATALOG:
        validate_model_for_backend(model.id, backend="openrouter")
