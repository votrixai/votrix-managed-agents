from datetime import datetime, timedelta, timezone

from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.worker import _next_runnable_work, run_worker
from tests.conftest import TEST_HEADERS


async def test_worker_once_consumes_queued_work(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Worker Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "self-hosted-worker", "config": {"type": "self_hosted"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={"agent": {"id": agent["id"], "version": 1}, "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    session = response.json()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "work"}]},
    )
    assert response.status_code == 200, response.text

    await run_worker(environment_id=environment["id"], poll_interval_seconds=0.01, once=True)

    response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["completed"] == 1


async def test_worker_next_runnable_work_leases_before_execution(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Worker Lease Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "self-hosted-worker-lease", "config": {"type": "self_hosted"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={"agent": {"id": agent["id"], "version": 1}, "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    session = response.json()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "lease once"}]},
    )
    assert response.status_code == 200, response.text

    leased = await _next_runnable_work(environment_id=environment["id"], worker_id="worker-1", lease_seconds=30)

    assert leased is not None
    assert leased["status"] == "leased"
    assert leased["lease"]["worker_id"] == "worker-1"
    assert await _next_runnable_work(environment_id=environment["id"], worker_id="worker-2", lease_seconds=30) is None


async def test_worker_skips_rescheduled_work_before_retry_at(client):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "self-hosted-reschedule-worker", "config": {"type": "self_hosted"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    async with session_scope() as db:
        await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id=environment["id"],
            name="session:future",
            status="rescheduling",
            data={
                "session_id": "sess_future",
                "attempt": 1,
                "retry_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            },
        )
        await db.commit()

    assert await _next_runnable_work(environment_id=environment["id"]) is None


async def test_vault_credential_response_redacts_secret_fields(client):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"name": "Main"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "name": "github",
            "api_key": "sk-test",
            "nested": {"access_token": "secret-token", "safe": "value"},
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    assert credential["api_key"] == "redacted"
    assert credential["nested"]["access_token"] == "redacted"
    assert credential["nested"]["safe"] == "value"


async def test_vault_credential_archive_purges_persisted_secret_values(client):
    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"name": "Main"})
    assert response.status_code == 201, response.text
    vault = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "Linear",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "secret-access-token",
                "refresh": {
                    "client_id": "client-1",
                    "refresh_token": "secret-refresh-token",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {"type": "none"},
                    "safe": "keep",
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials/{credential['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    archived = response.json()
    assert archived["archived_at"] is not None
    assert archived["auth"]["mcp_server_url"] == "https://mcp.example.invalid"

    async with session_scope() as db:
        stored = await res_q.get_resource(
            db,
            resource_id=credential["id"],
            resource_type="credential",
            parent_id=vault["id"],
            include_deleted=True,
        )

    assert stored is not None
    auth = stored.data["auth"]
    assert auth["access_token"] is None
    assert auth["refresh"]["refresh_token"] is None
    assert auth["refresh"]["safe"] == "keep"
    assert stored.data["secrets_purged_at"]
