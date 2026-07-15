from httpx import ASGITransport, AsyncClient

from app.factory import create_app
from tests.conftest import TEST_HEADERS


async def test_request_id_is_echoed_on_success(client):
    response = await client.get("/health", headers={"request-id": "req_client_1234"})

    assert response.status_code == 200
    assert response.headers["request-id"] == "req_client_1234"
    assert response.headers["x-request-id"] == "req_client_1234"


async def test_error_body_and_headers_share_generated_request_id(client):
    response = await client.get(
        "/v1/sessions/sess_missing",
        headers={**TEST_HEADERS, "x-request-id": "invalid whitespace"},
    )

    assert response.status_code == 404
    request_id = response.headers["request-id"]
    assert request_id.startswith("req_")
    assert response.headers["x-request-id"] == request_id
    assert response.json()["request_id"] == request_id
    assert response.json()["error"]["request_id"] == request_id
    assert response.json()["error"]["code"] == "resource_not_found"


async def test_unhandled_error_uses_stable_envelope_and_request_id():
    app = create_app()

    @app.get("/_test/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/_test/boom",
            headers={"request-id": "req_unhandled_1234"},
        )

    assert response.status_code == 500
    assert response.headers["request-id"] == "req_unhandled_1234"
    assert response.json()["request_id"] == "req_unhandled_1234"
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text
