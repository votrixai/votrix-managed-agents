from app.config import get_settings
from tests.conftest import TEST_HEADERS


async def _create_agent_environment_and_session(client, *, name: str):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": f"{name} Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{name}-environment", "config": {"type": "self_hosted"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"id": agent_response.json()["id"], "version": 1},
            "environment_id": environment_response.json()["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    return session_response.json()


async def test_active_work_quota_denial_and_cancel_release(client, monkeypatch):
    monkeypatch.setenv("VMA_MAX_ACTIVE_WORK", "1")
    get_settings.cache_clear()

    first = await _create_agent_environment_and_session(client, name="first-quota")
    second = await _create_agent_environment_and_session(client, name="second-quota")

    first_run = await client.post(
        f"/v1/sessions/{first['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "hold the slot"}]},
    )
    assert first_run.status_code == 200, first_run.text

    denied = await client.post(
        f"/v1/sessions/{second['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "wait for a slot"}]},
    )
    assert denied.status_code == 429, denied.text
    assert denied.json()["error"]["code"] == "active_work_quota_exceeded"
    assert denied.headers["x-quota-metric"] == "active_work"
    assert denied.headers["x-quota-limit"] == "1"
    assert denied.headers["x-quota-remaining"] == "0"

    cancelled = await client.post(
        f"/v1/sessions/{first['id']}/cancel",
        headers=TEST_HEADERS,
    )
    assert cancelled.status_code == 200, cancelled.text

    admitted = await client.post(
        f"/v1/sessions/{second['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "slot is available"}]},
    )
    assert admitted.status_code == 200, admitted.text
