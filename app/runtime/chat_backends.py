"""Build the chat client selected by an Account credential.

The public model id is stable across funding modes. Platform Accounts route
every catalog model through OpenRouter; a direct BYOK Account can only route a
model owned by the same backend. Each adapter translates VMA's small thinking
vocabulary into that backend's native controls, while LangChain normalizes all
final token metadata back into one ``UsageMetadata`` shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.models.llm import (
    ANTHROPIC,
    DEEPSEEK,
    DEFAULT_THINKING,
    GOOGLE,
    MODEL_CATALOG,
    OPENAI,
    OPENROUTER_SLUGS,
    THINKING_LEVELS,
    ModelResponse,
)
from app.services.account_credentials import ResolvedAccountCredential

OPENROUTER = "openrouter"

# Stated explicitly because an upstream id is an API contract, not a naming
# convention. A catalog addition is not usable for direct BYOK until its
# native id is deliberately added here.
DIRECT_MODEL_IDS: dict[str, dict[str, str]] = {
    ANTHROPIC: {
        "claude-opus-5": "claude-opus-5",
        "claude-sonnet-5": "claude-sonnet-5",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "claude-haiku-4-5": "claude-haiku-4-5",
    },
    GOOGLE: {
        "gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
        "gemini-3.6-flash": "gemini-3.6-flash",
        "gemini-3.5-flash": "gemini-3.5-flash",
        "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
        "gemini-2.5-pro": "gemini-2.5-pro",
    },
    OPENAI: {
        "gpt-5.6-sol": "gpt-5.6-sol",
        "gpt-5.6-terra": "gpt-5.6-terra",
        "gpt-5.6-luna": "gpt-5.6-luna",
    },
    DEEPSEEK: {
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek-v4-flash",
    },
}


class UnknownModelError(ValueError):
    """A model id that is absent from the VMA catalog."""


class UnsupportedModelBackendError(ValueError):
    """The Account's direct backend cannot serve the requested model."""


class UnsupportedThinkingError(ValueError):
    """A thinking level cannot be represented by the selected model/backend."""


class MissingProviderKeyError(RuntimeError):
    """A credential resolved without usable secret material."""


def model_id(spec: dict[str, Any] | str) -> str:
    return spec if isinstance(spec, str) else str(spec.get("id") or "")


def resolve_catalog_model(spec: dict[str, Any] | str) -> ModelResponse:
    """Resolve the public model id without making a routing decision."""

    requested_id = model_id(spec)
    entry = next((model for model in MODEL_CATALOG if model.id == requested_id), None)
    if entry is None:
        known = ", ".join(model.id for model in MODEL_CATALOG)
        raise UnknownModelError(
            f"Unknown model {requested_id!r}. Known models: {known}"
        )
    return entry


def validate_model_for_backend(
    spec: dict[str, Any] | str,
    *,
    backend: str,
) -> ModelResponse:
    """Resolve a catalog entry and prove the backend can serve its options."""

    entry = resolve_catalog_model(spec)

    if backend == OPENROUTER:
        if entry.id not in OPENROUTER_SLUGS:
            raise UnknownModelError(
                f"Model {entry.id!r} has no OpenRouter slug configured"
            )
    else:
        native = DIRECT_MODEL_IDS.get(backend, {})
        if entry.provider != backend or entry.id not in native:
            raise UnsupportedModelBackendError(
                f"Model {entry.id!r} cannot use the {backend} BYOK backend; "
                f"choose a model served by {backend} or an OpenRouter Account"
            )

    thinking = resolve_thinking(spec, entry)
    if (
        backend == GOOGLE
        and entry.id == "gemini-3.1-pro-preview"
        and thinking == "none"
    ):
        raise UnsupportedThinkingError(
            "gemini-3.1-pro-preview cannot disable thinking on the direct "
            "Google backend"
        )
    return entry


def build_chat_model(
    spec: dict[str, Any] | str,
    *,
    credential: ResolvedAccountCredential,
    session_id: str,
    callbacks: list[Any] | None = None,
) -> Any:
    """Build exactly one native client from an Account credential."""

    entry = validate_model_for_backend(spec, backend=credential.backend)
    key = _require_key(credential.api_key)
    thinking = resolve_thinking(spec, entry)

    if credential.backend == OPENROUTER:
        return _openrouter_model(
            entry,
            key=key,
            thinking=thinking,
            session_id=session_id,
            callbacks=callbacks,
        )
    if credential.backend == ANTHROPIC:
        return _anthropic_model(
            entry, key=key, thinking=thinking, callbacks=callbacks
        )
    if credential.backend == OPENAI:
        return _openai_model(entry, key=key, thinking=thinking, callbacks=callbacks)
    if credential.backend == GOOGLE:
        return _google_model(entry, key=key, thinking=thinking, callbacks=callbacks)
    if credential.backend == DEEPSEEK:
        return _deepseek_model(
            entry, key=key, thinking=thinking, callbacks=callbacks
        )
    raise UnsupportedModelBackendError(
        f"Unsupported Account credential backend {credential.backend!r}"
    )


def resolve_thinking(
    spec: dict[str, Any] | str,
    entry: ModelResponse,
) -> str | None:
    requested = None if isinstance(spec, str) else spec.get("thinking")
    if requested is None:
        return DEFAULT_THINKING if entry.thinking else None

    level = str(requested).strip().lower()
    if level not in THINKING_LEVELS:
        allowed = ", ".join(THINKING_LEVELS)
        raise UnsupportedThinkingError(
            f"Unknown thinking level {requested!r}. Allowed: {allowed}"
        )
    if not entry.thinking:
        raise UnsupportedThinkingError(
            f"Model {entry.id!r} takes no thinking level — it reasons as it sees fit"
        )
    return level


def _openrouter_model(
    entry: ModelResponse,
    *,
    key: str,
    thinking: str | None,
    session_id: str,
    callbacks: list[Any] | None,
) -> Any:
    from langchain_openrouter import ChatOpenRouter

    options: dict[str, Any] = {}
    if entry.provider == DEEPSEEK:
        options["openrouter_provider"] = {
            "only": [DEEPSEEK],
            "allow_fallbacks": False,
        }
    if thinking is not None:
        options["reasoning"] = {"effort": thinking}
    return ChatOpenRouter(
        model=OPENROUTER_SLUGS[entry.id],
        api_key=key,
        session_id=session_id,
        stream_usage=True,
        callbacks=callbacks,
        **options,
    )


def _anthropic_model(
    entry: ModelResponse,
    *,
    key: str,
    thinking: str | None,
    callbacks: list[Any] | None,
) -> Any:
    from langchain_anthropic import ChatAnthropic

    options: dict[str, Any] = {}
    if thinking == "none":
        options["thinking"] = {"type": "disabled"}
    elif thinking is not None:
        options["thinking"] = {"type": "adaptive"}
        options["effort"] = thinking
    return ChatAnthropic(
        model=DIRECT_MODEL_IDS[ANTHROPIC][entry.id],
        api_key=SecretStr(key),
        max_tokens=16_000,
        stream_usage=True,
        callbacks=callbacks,
        **options,
    )


def _openai_model(
    entry: ModelResponse,
    *,
    key: str,
    thinking: str | None,
    callbacks: list[Any] | None,
) -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=DIRECT_MODEL_IDS[OPENAI][entry.id],
        api_key=SecretStr(key),
        reasoning_effort=thinking,
        use_responses_api=True,
        stream_usage=True,
        callbacks=callbacks,
    )


def _google_model(
    entry: ModelResponse,
    *,
    key: str,
    thinking: str | None,
    callbacks: list[Any] | None,
) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    options: dict[str, Any] = {}
    if thinking is not None:
        options["thinking_level"] = "minimal" if thinking == "none" else thinking
    return ChatGoogleGenerativeAI(
        model=DIRECT_MODEL_IDS[GOOGLE][entry.id],
        api_key=SecretStr(key),
        # Gemini 3.x guidance requires its default 1.0 sampling behavior.
        temperature=1.0,
        callbacks=callbacks,
        **options,
    )


def _deepseek_model(
    entry: ModelResponse,
    *,
    key: str,
    thinking: str | None,
    callbacks: list[Any] | None,
) -> Any:
    from langchain_deepseek import ChatDeepSeek

    options: dict[str, Any] = {}
    if thinking is not None:
        options["extra_body"] = {
            "thinking": {"type": "disabled" if thinking == "none" else "enabled"}
        }
    if thinking not in (None, "none"):
        options["reasoning_effort"] = thinking
    return ChatDeepSeek(
        model=DIRECT_MODEL_IDS[DEEPSEEK][entry.id],
        api_key=SecretStr(key),
        stream_usage=True,
        callbacks=callbacks,
        **options,
    )


def _require_key(key: SecretStr | str) -> str:
    plaintext = key.get_secret_value() if isinstance(key, SecretStr) else key
    if not plaintext:
        raise MissingProviderKeyError(
            "no Account credential was resolved for this turn"
        )
    return plaintext


__all__ = [
    "DIRECT_MODEL_IDS",
    "MissingProviderKeyError",
    "OPENROUTER",
    "UnknownModelError",
    "UnsupportedModelBackendError",
    "UnsupportedThinkingError",
    "build_chat_model",
    "model_id",
    "resolve_catalog_model",
    "resolve_thinking",
    "validate_model_for_backend",
]
