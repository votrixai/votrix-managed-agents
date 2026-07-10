"""Model provider resolution for the Deep Agents runtime.

Provider routing is deliberately server-controlled. Agent resources select a provider
and model, while credentials and endpoint URLs come from service configuration or a
session vault. This keeps tenant-supplied JSON from silently redirecting requests to an
arbitrary endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import SecretStr

from app.config import Settings, get_settings


class ProviderConfigurationError(RuntimeError):
    """Raised when a model provider cannot be resolved safely."""


@dataclass(frozen=True)
class RuntimeProviderCapabilities:
    streaming: bool = True
    tool_calls: bool = True
    multimodal_input: bool = False
    reasoning: bool = False
    native_structured_output: bool = False


@dataclass(frozen=True)
class RuntimeProviderConfig:
    provider: str
    model_id: str
    adapter: str
    api_key: str | None
    base_url: str | None
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    capabilities: RuntimeProviderCapabilities = RuntimeProviderCapabilities()


def runtime_provider_configured(
    model: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> bool:
    """Return whether a provider has enough configuration to construct a model."""
    try:
        config = resolve_runtime_provider(model, runtime=runtime, secrets=secrets)
    except ProviderConfigurationError as exc:
        if "requires an API key" in str(exc):
            return False
        raise
    return bool(config.api_key) or config.adapter in {"ollama", "fake"}


def resolve_runtime_provider(
    model: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> RuntimeProviderConfig:
    """Resolve a public model declaration into a server-owned provider config."""
    settings = get_settings()
    runtime = dict(runtime or {})
    provider, explicit_model_id = _provider_and_model(model, runtime, settings)
    registry = _provider_registry(settings)
    provider_config = registry.get(provider)

    # Installed LangChain integrations remain usable without pre-registration, but
    # custom URLs never do: only registry entries may supply a base URL.
    if provider_config is None:
        provider_config = {
            "adapter": provider,
            "api_key_env": f"{provider.upper()}_API_KEY",
            "default_model": "",
            "capabilities": {},
        }

    adapter = _clean_optional_str(provider_config.get("adapter")) or provider
    model_id = explicit_model_id or _clean_optional_str(provider_config.get("default_model"))
    if not model_id:
        raise ProviderConfigurationError(f"Provider {provider} requires a model id")

    api_key_env = _clean_optional_str(provider_config.get("api_key_env"))
    api_key = _resolve_secret(api_key_env, provider_config, secrets or {})
    if not api_key and adapter not in {"ollama", "fake"}:
        key_hint = api_key_env or f"{provider.upper()}_API_KEY"
        raise ProviderConfigurationError(f"Provider {provider} requires an API key in {key_hint}")

    model_kwargs = dict(provider_config.get("model_kwargs") or {})
    runtime_model = runtime.get("model")
    if isinstance(runtime_model, dict):
        # Only inference parameters are tenant configurable. Connection and auth
        # parameters remain server-owned.
        for key in (
            "temperature",
            "max_tokens",
            "timeout",
            "max_retries",
            "top_p",
            "reasoning_effort",
            "use_responses_api",
        ):
            if key in runtime_model:
                model_kwargs[key] = runtime_model[key]

    capabilities = _provider_capabilities(adapter, provider_config)
    if adapter == "deepseek" and model_id == "deepseek-reasoner":
        capabilities = replace(capabilities, tool_calls=False)

    return RuntimeProviderConfig(
        provider=provider,
        model_id=model_id,
        adapter=adapter,
        api_key=api_key,
        base_url=_clean_optional_str(provider_config.get("base_url")),
        model_kwargs=model_kwargs,
        capabilities=capabilities,
    )


def build_chat_model(config: RuntimeProviderConfig):
    """Construct a LangChain chat model without mutating process environment."""
    kwargs = dict(config.model_kwargs)
    if config.api_key:
        kwargs["api_key"] = SecretStr(config.api_key)
    if config.base_url:
        kwargs["base_url"] = config.base_url

    if config.adapter == "openai":
        from langchain_openai import ChatOpenAI

        kwargs.setdefault("use_responses_api", False)
        return ChatOpenAI(model=config.model_id, **kwargs)
    if config.adapter == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(model=config.model_id, **kwargs)
    if config.adapter == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=config.model_id, **kwargs)

    from langchain.chat_models import init_chat_model

    return init_chat_model(
        model=config.model_id,
        model_provider=config.adapter,
        **kwargs,
    )


def _provider_registry(settings: Settings) -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {
        "openai": {
            "adapter": "openai",
            "api_key": getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY", ""),
            "api_key_env": "OPENAI_API_KEY",
            "base_url": getattr(settings, "openai_base_url", "") or os.getenv("OPENAI_BASE_URL", ""),
            "default_model": _setting(settings, "vma_default_openai_model", default="gpt-5.5"),
            "model_kwargs": {
                "use_responses_api": bool(getattr(settings, "openai_use_responses", False)),
            },
            "capabilities": {
                "multimodal_input": True,
                "reasoning": True,
                "native_structured_output": True,
            },
        },
        "anthropic": {
            "adapter": "anthropic",
            "api_key": getattr(settings, "anthropic_api_key", "") or os.getenv("ANTHROPIC_API_KEY", ""),
            "api_key_env": "ANTHROPIC_API_KEY",
            "default_model": _setting(settings, "vma_default_anthropic_model", default="claude-sonnet-4-6"),
            "capabilities": {
                "multimodal_input": True,
                "reasoning": True,
                "native_structured_output": True,
            },
        },
        "deepseek": {
            "adapter": "deepseek",
            "api_key": getattr(settings, "deepseek_api_key", "") or os.getenv("DEEPSEEK_API_KEY", ""),
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": getattr(settings, "deepseek_api_base", "") or os.getenv("DEEPSEEK_API_BASE", ""),
            "default_model": _setting(settings, "vma_default_deepseek_model", default="deepseek-chat"),
            "capabilities": {
                "reasoning": True,
                "native_structured_output": True,
            },
        },
    }

    configured = _setting(settings, "vma_model_providers", default={})
    for raw_name, raw_config in (configured.items() if isinstance(configured, dict) else ()):
        name = _normalize_provider_name(str(raw_name))
        if not name or not isinstance(raw_config, dict):
            continue
        config = dict(raw_config)
        config.setdefault("adapter", "openai")
        config.setdefault("api_key_env", f"{name.upper()}_API_KEY")
        registry[name] = config
    return registry


def _provider_and_model(
    model: dict[str, Any],
    runtime: dict[str, Any],
    settings: Settings,
) -> tuple[str, str | None]:
    raw_model_id = _clean_optional_str(model.get("id") or model.get("model"))
    runtime_model = runtime.get("model")
    runtime_provider = runtime_model.get("provider") if isinstance(runtime_model, dict) else None
    raw_provider = _clean_optional_str(
        runtime_provider
        or model.get("provider")
        or model.get("provider_id")
        or model.get("vendor")
        or model.get("source")
    )
    if raw_provider:
        return _normalize_provider_name(raw_provider), raw_model_id

    if raw_model_id and ":" in raw_model_id:
        candidate_provider, candidate_model = raw_model_id.split(":", 1)
        if candidate_provider and candidate_model:
            return _normalize_provider_name(candidate_provider), candidate_model

    default_provider = _setting(
        settings,
        "vma_default_model_provider",
        default="anthropic",
    )
    return _normalize_provider_name(str(default_provider)), raw_model_id


def _resolve_secret(
    env_name: str | None,
    config: dict[str, Any],
    secrets: dict[str, str],
) -> str | None:
    if env_name and secrets.get(env_name):
        return secrets[env_name]
    direct = _clean_optional_str(config.get("api_key"))
    if direct:
        return direct
    return _clean_optional_str(os.getenv(env_name, "")) if env_name else None


def _provider_capabilities(adapter: str, config: dict[str, Any]) -> RuntimeProviderCapabilities:
    raw = dict(config.get("capabilities") or {})
    return RuntimeProviderCapabilities(
        streaming=bool(raw.get("streaming", True)),
        tool_calls=bool(raw.get("tool_calls", True)),
        multimodal_input=bool(raw.get("multimodal_input", adapter in {"openai", "anthropic"})),
        reasoning=bool(raw.get("reasoning", adapter in {"openai", "anthropic", "deepseek"})),
        native_structured_output=bool(raw.get("native_structured_output", False)),
    )


def _setting(settings: Settings, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(settings, name):
            value = getattr(settings, name)
            if value not in (None, ""):
                return value
    return default


def _normalize_provider_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
