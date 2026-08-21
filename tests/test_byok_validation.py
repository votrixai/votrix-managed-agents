"""BYOK validation uses read-only endpoints and never exposes a secret."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.models.errors import CredentialValidationUnavailable, InvalidRequest
from app.services.account_credentials import HttpByokKeyValidator


@pytest.mark.parametrize(
    ("backend", "url", "header", "value"),
    [
        (
            "anthropic",
            "https://api.anthropic.com/v1/models?limit=1",
            "x-api-key",
            "secret-key",
        ),
        (
            "openai",
            "https://api.openai.com/v1/models",
            "Authorization",
            "Bearer secret-key",
        ),
        (
            "google",
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            "x-goog-api-key",
            "secret-key",
        ),
        (
            "deepseek",
            "https://api.deepseek.com/models",
            "Authorization",
            "Bearer secret-key",
        ),
    ],
)
async def test_each_backend_uses_its_read_only_key_check(
    backend, url, header, value
):
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    validator = HttpByokKeyValidator(transport=httpx.MockTransport(respond))
    await validator.validate(backend=backend, api_key=SecretStr("secret-key"))

    assert str(seen[0].url) == url
    assert seen[0].headers[header] == value
    if backend == "google":
        assert "secret-key" not in str(seen[0].url)


async def test_rejected_key_error_never_contains_the_key_or_upstream_body():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "secret-key was invalid"},
        )

    validator = HttpByokKeyValidator(transport=httpx.MockTransport(respond))
    with pytest.raises(InvalidRequest) as raised:
        await validator.validate(
            backend="openai", api_key=SecretStr("secret-key")
        )

    assert "secret-key" not in str(raised.value)
    assert "was invalid" not in str(raised.value)


async def test_openrouter_is_reserved_for_platform_funding():
    validator = HttpByokKeyValidator(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    with pytest.raises(InvalidRequest, match="Unsupported BYOK backend"):
        await validator.validate(
            backend="openrouter", api_key=SecretStr("secret-key")
        )


async def test_provider_outage_is_not_misreported_as_a_bad_key():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    validator = HttpByokKeyValidator(transport=httpx.MockTransport(respond))
    with pytest.raises(CredentialValidationUnavailable):
        await validator.validate(
            backend="anthropic", api_key=SecretStr("secret-key")
        )


@pytest.mark.parametrize("status", [302, 408, 429, 503])
async def test_transient_or_redirect_responses_are_validation_unavailable(status):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    validator = HttpByokKeyValidator(transport=httpx.MockTransport(respond))
    with pytest.raises(CredentialValidationUnavailable):
        await validator.validate(
            backend="openai", api_key=SecretStr("secret-key")
        )
