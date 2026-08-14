"""Which browser origins may call the API, and with what.

CORS is configuration, so these tests build the app under an explicit
`VMA_CORS_ORIGINS` rather than trusting whatever the environment happens to
hold.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import clear_settings_cache
from app.server import create_app


ALLOWED = "https://docs.vma.votrixai.com"
ALSO_ALLOWED = "https://vma.votrixai.com"
DENIED = "https://not-votrix.example"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("VMA_CORS_ORIGINS", f"{ALSO_ALLOWED},{ALLOWED}")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setenv("VMA_CORS_ORIGINS", "")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest_asyncio.fixture
async def http(request):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _preflight(http: AsyncClient, origin: str, *, method: str = "GET"):
    return await http.options(
        "/v1/models",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "x-api-key",
        },
    )


async def test_preflight_from_an_allowed_origin_succeeds(configured, http):
    response = await _preflight(http, ALLOWED)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "x-api-key" in allowed_headers


async def test_a_real_request_is_readable_by_an_allowed_origin(configured, http):
    response = await http.get("/v1/models", headers={"Origin": ALSO_ALLOWED})

    assert response.headers["access-control-allow-origin"] == ALSO_ALLOWED
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    assert "request-id" in exposed


async def test_an_unlisted_origin_gets_no_grant(configured, http):
    """The browser is what enforces this, so the absent header is the point."""
    preflight = await _preflight(http, DENIED)
    assert "access-control-allow-origin" not in preflight.headers

    actual = await http.get("/v1/models", headers={"Origin": DENIED})
    assert "access-control-allow-origin" not in actual.headers


@pytest.mark.parametrize(
    "requested",
    [
        # What the browser attaches by itself for `fetch(…, {cache: "no-cache"})`.
        "cache-control,pragma",
        # What the API contract needs.
        "x-api-key",
        "idempotency-key",
        # Anything else a trusted origin decides to send.
        "authorization",
        "x-some-header-nobody-has-thought-of-yet",
    ],
)
async def test_an_allowed_origin_may_send_any_request_header(
    configured,
    http,
    requested,
):
    """A preflight naming one unlisted header takes down every endpoint.

    The headers are not all the caller's choice — `cache: "no-cache"` makes
    the browser add two on its own — so enumerating them cannot be kept
    correct. The boundary that matters is the origin list, which stays exact.
    """
    response = await http.options(
        "/v1/models",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": requested,
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    granted = response.headers["access-control-allow-headers"].lower()
    assert all(name in granted for name in requested.split(","))


async def test_every_documented_write_method_is_preflightable(configured, http):
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        response = await _preflight(http, ALLOWED, method=method)
        assert response.status_code == 200, method
        assert method in response.headers["access-control-allow-methods"], method


async def test_no_configured_origins_means_no_cors_headers(unconfigured, http):
    """An API whose credential belongs on a server denies browsers by default."""
    response = await http.get("/v1/models", headers={"Origin": ALLOWED})

    assert "access-control-allow-origin" not in response.headers


async def test_credentials_are_never_granted(configured, http):
    """VMA authenticates by header, so no response needs cookie access."""
    preflight = await _preflight(http, ALLOWED)

    assert "access-control-allow-credentials" not in preflight.headers


async def test_origins_parse_without_blanks_or_duplicates(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("VMA_CORS_ORIGINS", f" {ALLOWED} , ,{ALLOWED},{ALSO_ALLOWED} ")
    clear_settings_cache()
    try:
        assert get_settings().cors_origins == (ALLOWED, ALSO_ALLOWED)
    finally:
        clear_settings_cache()
