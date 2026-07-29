import base64
import json

from tests.conftest import TEST_HEADERS


def expired_page_cursor(offset: int = 0) -> str:
    payload = json.dumps({"offset": offset, "expires_at": 0}, separators=(",", ":")).encode("utf-8")
    return "page_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


async def test_page_cursor_rejects_invalid_cursor(client):
    response = await client.get("/v1/agents", headers=TEST_HEADERS, params={"page": "not-a-page"})

    assert response.status_code == 400
    assert "Invalid page cursor" in response.json()["error"]["message"]


async def test_page_cursor_rejects_expired_cursor(client):
    response = await client.get("/v1/agents", headers=TEST_HEADERS, params={"page": expired_page_cursor()})

    assert response.status_code == 400
    assert "Expired page cursor" in response.json()["error"]["message"]


async def test_file_id_cursor_rejects_unknown_cursor(client):
    response = await client.get("/v1/files", headers=TEST_HEADERS, params={"after_id": "file_missing"})

    assert response.status_code == 400
    assert "Invalid pagination cursor" in response.json()["error"]["message"]


async def test_file_id_cursor_rejects_after_and_before_together(client):
    response = await client.get(
        "/v1/files",
        headers=TEST_HEADERS,
        params={"after_id": "file_a", "before_id": "file_b"},
    )

    assert response.status_code == 400
    assert "Only one of after_id or before_id" in response.json()["error"]["message"]


async def test_list_order_rejects_unknown_direction(client):
    response = await client.get("/v1/sessions", headers=TEST_HEADERS, params={"order": "sideways"})

    assert response.status_code == 422
    assert "order must be asc or desc" in response.json()["error"]["message"]

    response = await client.get("/v1/user_profiles", headers=TEST_HEADERS, params={"order": "sideways"})

    assert response.status_code == 422
    assert "order must be asc or desc" in response.json()["error"]["message"]


async def test_memory_list_rejects_unknown_order_params(client):
    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "Order Validation Store"},
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        params={"order": "sideways"},
    )
    assert response.status_code == 422
    assert "order must be asc or desc" in response.json()["error"]["message"]

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        params={"order_by": "unsupported"},
    )
    assert response.status_code == 422
    assert "order_by must be path or created_at" in response.json()["error"]["message"]


async def test_list_filters_reject_unknown_enum_values(client):
    response = await client.get("/v1/sessions", headers=TEST_HEADERS, params={"statuses": "sleeping"})
    assert response.status_code == 422
    assert "statuses must be idle" in response.json()["error"]["message"]

    response = await client.get("/v1/deployment_runs", headers=TEST_HEADERS, params={"trigger_type": "webhook"})
    assert response.status_code == 422
    assert "trigger_type must be manual or schedule" in response.json()["error"]["message"]


async def test_memory_list_rejects_unknown_view(client):
    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "View Validation Store"},
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        params={"view": "summary"},
    )
    assert response.status_code == 422
    assert "view must be basic or full" in response.json()["error"]["message"]


async def test_created_at_filters_accept_sdk_aliases(client):
    for index in range(2):
        response = await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={"name": f"Filtered Agent {index}", "model": {"id": "gpt-5.5"}},
        )
        assert response.status_code == 201, response.text

    response = await client.get("/v1/agents", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    first_created_at = response.json()["data"][0]["created_at"]

    response = await client.get(
        "/v1/agents",
        headers=TEST_HEADERS,
        params={"created_at[gte]": first_created_at, "created_at[lte]": first_created_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]
    assert all(item["created_at"] == first_created_at for item in response.json()["data"])


async def test_limit_is_clamped_to_safe_maximum(client):
    for index in range(3):
        response = await client.post(
            "/v1/user_profiles",
            headers=TEST_HEADERS,
            json={"relationship": "external", "external_id": f"user-{index}"},
        )
        assert response.status_code == 201, response.text

    response = await client.get("/v1/user_profiles", headers=TEST_HEADERS, params={"limit": 5000})

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 3
    assert response.json()["has_more"] is False


async def test_agent_limit_is_clamped_to_sdk_maximum(client):
    for index in range(105):
        response = await client.post(
            "/v1/agents",
            headers=TEST_HEADERS,
            json={"name": f"Limit Agent {index}", "model": {"id": "gpt-5.5"}},
        )
        assert response.status_code == 201, response.text

    response = await client.get("/v1/agents", headers=TEST_HEADERS, params={"limit": 5000})

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 100
    assert response.json()["has_more"] is True


async def test_agent_versions_limit_is_clamped_to_sdk_maximum(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Version Limit Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    for index in range(104):
        response = await client.patch(
            f"/v1/agents/{agent['id']}",
            headers=TEST_HEADERS,
            json={"version": index + 1, "system": f"version {index + 2}"},
        )
        assert response.status_code == 200, response.text

    response = await client.get(f"/v1/agents/{agent['id']}/versions", headers=TEST_HEADERS, params={"limit": 5000})

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 100
    assert response.json()["has_more"] is True
