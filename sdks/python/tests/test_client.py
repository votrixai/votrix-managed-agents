from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from votrix import (
    AsyncVotrix,
    ConflictError,
    InternalServerError,
    RateLimitError,
    UnprocessableEntityError,
    VotrixModel,
)
import votrix._client as client_module


def make_client(handler, *, max_retries: int = 0, auth_scheme: str = "x-api-key"):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncVotrix(
        api_key="vma_test_secret",
        base_url="https://vma.test",
        auth_scheme=auth_scheme,
        max_retries=max_retries,
        http_client=http_client,
    )
    return sdk, http_client


def test_client_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("VOTRIX_API_KEY", raising=False)
    monkeypatch.delenv("VOTRIX_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        AsyncVotrix(base_url="https://vma.test")
    monkeypatch.setenv("VOTRIX_API_KEY", "vma_test")
    with pytest.raises(ValueError, match="base_url"):
        AsyncVotrix()


@pytest.mark.asyncio
async def test_native_headers_and_external_client_ownership():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "openrouter",
                "type": "model_provider",
                "display_name": "OpenRouter",
                "adapter": "openai",
                "credential_type": "api_key",
                "capabilities": {},
            },
        )

    sdk, http_client = make_client(handler)
    provider = await sdk.model_providers.retrieve("openrouter")
    assert provider.id == "openrouter"
    assert seen["x-api-key"] == "vma_test_secret"
    assert seen["votrix-managed-agents-beta"] == "votrix-managed-agents-2026-04-01"
    assert seen["x-votrix-sdk-version"]
    await sdk.close()
    assert not http_client.is_closed
    await http_client.aclose()


@pytest.mark.asyncio
async def test_bearer_auth():
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer vma_test_secret"
        assert "x-api-key" not in request.headers
        return httpx.Response(
            200,
            json={
                "id": "none",
                "type": "model_provider",
                "display_name": "None",
                "adapter": "none",
                "credential_type": "none",
                "capabilities": {},
            },
        )

    sdk, http_client = make_client(handler, auth_scheme="bearer")
    await sdk.model_providers.retrieve("none")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_idempotent_event_retry_replays_same_body_and_key(monkeypatch):
    monkeypatch.setattr(client_module, "_retry_delay", lambda *_: 0)
    requests: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request):
        requests.append((request.content, request.headers["idempotency-key"]))
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"type": "overloaded_error", "message": "retry"}})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "event_1",
                        "type": "user.message",
                        "session_id": "session_1",
                        "seq": 1,
                    }
                ]
            },
        )

    sdk, http_client = make_client(handler, max_retries=1)
    result = await sdk.sessions.events.send(
        "session_1", events=[{"type": "user.message", "content": "hello"}]
    )
    assert result.data[0].type == "user.message"
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert json.loads(requests[0][0])["events"][0]["content"] == "hello"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_conflict_is_not_retried():
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(409, json={"detail": "semantic conflict"})

    sdk, http_client = make_client(handler, max_retries=3)
    with pytest.raises(ConflictError, match="semantic conflict"):
        await sdk.model_providers.retrieve("openrouter")
    assert calls == 1
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unsafe_request_cannot_be_forced_to_retry_without_idempotency_key(monkeypatch):
    monkeypatch.setattr(client_module, "_retry_delay", lambda *_: 0)
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "not safe to replay"})

    sdk, http_client = make_client(handler, max_retries=3)
    with pytest.raises(InternalServerError):
        await sdk.request(
            "POST",
            "/v1/model_providers",
            model=VotrixModel,
            json={"id": "provider"},
            retry=True,
        )
    assert calls == 1
    await http_client.aclose()


@pytest.mark.asyncio
async def test_status_error_exposes_request_and_rate_limit_headers():
    def handler(_request: httpx.Request):
        return httpx.Response(
            429,
            json={
                "error": {
                    "type": "rate_limit_error",
                    "code": "request_quota_exceeded",
                    "message": "slow down",
                }
            },
            headers={
                "x-request-id": "request_rate_limited",
                "retry-after": "3",
                "x-ratelimit-remaining": "0",
            },
        )

    sdk, http_client = make_client(handler)
    with pytest.raises(RateLimitError) as caught:
        await sdk.model_providers.retrieve("openrouter")
    assert caught.value.request_id == "request_rate_limited"
    assert caught.value.error_code == "request_quota_exceeded"
    assert caught.value.headers["retry-after"] == "3"
    assert caught.value.retry_after == "3"
    assert caught.value.rate_limit_headers == {
        "retry-after": "3",
        "x-ratelimit-remaining": "0",
    }
    await http_client.aclose()


@pytest.mark.asyncio
async def test_session_create_is_idempotent_and_retries_with_one_key(monkeypatch):
    monkeypatch.setattr(client_module, "_retry_delay", lambda *_: 0)
    requests: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request):
        requests.append((request.content, request.headers["idempotency-key"]))
        if len(requests) == 1:
            return httpx.Response(503, json={"detail": "retry session create"})
        return httpx.Response(201, json={"id": "session_1", "type": "session"})

    sdk, http_client = make_client(handler, max_retries=1)
    session = await sdk.sessions.create(
        agent="agent_1",
        environment_id="environment_1",
    )
    assert session.id == "session_1"
    assert requests[0] == requests[1]
    assert str(uuid.UUID(requests[0][1])) == requests[0][1]
    assert "idempotency_key" not in json.loads(requests[0][0])
    await http_client.aclose()


def test_retry_after_supports_http_date():
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    request = httpx.Request("GET", "https://vma.test")
    response = httpx.Response(
        503,
        request=request,
        headers={"retry-after": format_datetime(retry_at, usegmt=True)},
    )
    delay = client_module._retry_delay(0, response)
    assert 28 <= delay <= 31


@pytest.mark.asyncio
async def test_error_redacts_client_and_model_api_keys():
    byok = "sk-end-user-secret"

    def handler(_request: httpx.Request):
        return httpx.Response(
            422,
            json={
                "detail": f"invalid {byok} and vma_test_secret",
                "api_key": byok,
            },
        )

    sdk, http_client = make_client(handler)
    with pytest.raises(UnprocessableEntityError) as caught:
        await sdk.vaults.model_credentials.create(
            "vault_1", provider="openrouter", api_key=byok
        )
    rendered = f"{caught.value!s} {caught.value!r} {caught.value.body!r}"
    assert byok not in rendered
    assert "vma_test_secret" not in rendered
    assert "[redacted]" in rendered
    await http_client.aclose()
