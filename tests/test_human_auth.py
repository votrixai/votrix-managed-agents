"""Supabase verification at the first-party Console trust boundary."""

from collections.abc import Callable

import httpx
import pytest
from fastapi import HTTPException, status

from app import human_auth
from app.config import get_settings


@pytest.fixture(autouse=True)
def supabase_settings(monkeypatch):
    monkeypatch.setenv("VMA_SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setenv("VMA_SUPABASE_PUBLISHABLE_KEY", "publishable-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_transport(
    monkeypatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(human_auth.httpx, "AsyncClient", client)


async def test_authenticate_user_uses_supabase_user_endpoint(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "user-a", "app_metadata": {"super_admin": True}},
        )

    _use_transport(monkeypatch, handler)
    user = await human_auth.authenticate_user("access-token")

    assert user.id == "user-a"
    assert user.is_super_admin is True
    assert str(requests[0].url) == "https://project.supabase.co/auth/v1/user"
    assert requests[0].headers["apikey"] == "publishable-test"
    assert requests[0].headers["authorization"] == "Bearer access-token"


@pytest.mark.parametrize("provider_status", [400, 401, 403, 404])
async def test_authenticate_user_rejects_invalid_tokens(monkeypatch, provider_status):
    _use_transport(
        monkeypatch,
        lambda _request: httpx.Response(provider_status, json={"message": "denied"}),
    )

    with pytest.raises(HTTPException) as raised:
        await human_auth.authenticate_user("invalid")

    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED


async def test_authenticate_user_fails_closed_when_provider_is_unavailable(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _request: httpx.Response(503, json={"message": "unavailable"}),
    )

    with pytest.raises(HTTPException) as raised:
        await human_auth.authenticate_user("access-token")

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


@pytest.mark.parametrize("payload", [{"app_metadata": {}}, {"id": 123}])
async def test_authenticate_user_rejects_an_invalid_success_payload(
    monkeypatch,
    payload,
):
    _use_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(HTTPException) as raised:
        await human_auth.authenticate_user("access-token")

    assert raised.value.status_code == status.HTTP_401_UNAUTHORIZED


async def test_authenticate_user_rejects_a_non_object_document(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, json=[{"id": "user-a"}]),
    )

    with pytest.raises(HTTPException) as raised:
        await human_auth.authenticate_user("access-token")

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
