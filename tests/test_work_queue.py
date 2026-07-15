import asyncio
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.runtime.work_queue import lease_next_work_for_worker
from tests.conftest import TEST_HEADERS


async def _create_agent(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Queue Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client, env_type: str):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{env_type}-queue", "config": {"type": env_type}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_session(client, agent, environment):
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_inline_environment_queues_and_completes_work(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "cloud")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "run inline"}]},
    )
    assert response.status_code == 200, response.text

    for _ in range(20):
        response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
        assert response.status_code == 200, response.text
        stats = response.json()
        if stats["completed"] == 1:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"work did not complete; stats={stats}")


async def test_self_hosted_environment_leases_work_without_inline_execution(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "lease me"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 1

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    work = response.json()
    assert work["status"] == "leased"
    assert work["session_id"] == session["id"]
    assert work["lease"]["worker_id"] == "worker-1"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/ack",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_id": work["lease"]["lease_id"]},
    )
    assert response.status_code == 409
    assert "does not own" in response.json()["error"]["message"]

    # Ack is the one compatibility exception: Anthropic's generated SDK method
    # has no lease_id parameter.  Worker ownership is still checked, and an
    # explicitly supplied stale lease remains fenced.
    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/ack",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
        json={"progress": 0.25},
    )
    assert response.status_code == 409
    assert "lease_id is required" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "worker-1",
            "lease_id": work["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.5},
    )
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["progress"] == 0.5
    assert response.json()["state"] == "active"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "worker-2",
            "lease_id": work["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.9},
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}/stop",
        headers=TEST_HEADERS,
        json={"reason": "test stop"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    assert response.json()["stop"]["reason"] == "test stop"


async def test_environment_work_update_only_accepts_metadata(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "metadata update"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    work = response.json()

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"status": "completed"},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": None},
    )
    assert response.status_code == 422
    assert "metadata" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": {"phase": "queued", "drop": "soon"}},
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["phase"] == "queued"
    assert metadata["drop"] == "soon"
    assert response.json()["status"] == "leased"

    response = await client.post(
        f"/v1/environments/{environment['id']}/work/{work['id']}",
        headers=TEST_HEADERS,
        json={"metadata": {"drop": None}},
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["phase"] == "queued"
    assert "drop" not in metadata


async def test_expired_work_lease_can_be_recovered_by_next_worker(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "recover lease"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    first_lease = response.json()
    assert first_lease["attempt"] == 1

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=first_lease["id"],
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_seconds": 30},
    )

    assert response.status_code == 200, response.text
    recovered = response.json()
    assert recovered["id"] == first_lease["id"]
    assert recovered["status"] == "leased"
    assert recovered["attempt"] == 2
    assert recovered["lease"]["worker_id"] == "worker-2"


async def test_stale_lease_id_is_fenced_when_worker_id_is_reused(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "fence stale lease"}]},
    )
    assert response.status_code == 200, response.text

    first_response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "reused-worker", "lease_seconds": 30},
    )
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=first["id"],
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    second_response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "reused-worker", "lease_seconds": 30},
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["lease"]["lease_id"] != first["lease"]["lease_id"]
    assert second["lease"]["generation"] == first["lease"]["generation"] + 1

    stale_ack = await client.post(
        f"/v1/environments/{environment['id']}/work/{first['id']}/ack",
        headers=TEST_HEADERS,
        params={
            "worker_id": "reused-worker",
            "lease_id": first["lease"]["lease_id"],
        },
    )
    assert stale_ack.status_code == 409
    assert "current work lease generation" in stale_ack.json()["error"]["message"]

    stale = await client.post(
        f"/v1/environments/{environment['id']}/work/{first['id']}/heartbeat",
        headers=TEST_HEADERS,
        params={
            "worker_id": "reused-worker",
            "lease_id": first["lease"]["lease_id"],
            "lease_seconds": 30,
        },
        json={"progress": 0.9},
    )
    assert stale.status_code == 409
    assert "current work lease generation" in stale.json()["error"]["message"]


async def test_rescheduled_work_is_not_leased_until_retry_at(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)
    future_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    async with session_scope() as db:
        work = await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id=environment["id"],
            name=f"session:{session['id']}",
            status="rescheduling",
            data={"session_id": session["id"], "attempt": 1, "retry_at": future_retry_at},
        )
        await db.commit()
        work_id = work.id

    response = await client.get(f"/v1/environments/{environment['id']}/work/stats", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["rescheduling"] == 1

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    assert response.json() is None

    async with session_scope() as db:
        work = await res_q.get_resource(
            db,
            resource_id=work_id,
            resource_type="environment_work",
            parent_id=environment["id"],
        )
        assert work is not None
        data = dict(work.data)
        data["retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await res_q.update_resource(db, work, data=data, status="rescheduling")
        await db.commit()

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-2", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    leased = response.json()
    assert leased["id"] == work_id
    assert leased["status"] == "leased"
    assert leased["lease"]["worker_id"] == "worker-2"


async def test_worker_token_is_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("VMA_WORKER_TOKEN", "worker-secret")
    get_settings.cache_clear()

    agent = await _create_agent(client)
    environment = await _create_environment(client, "self_hosted")
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "secure lease"}]},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers=TEST_HEADERS,
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 401

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers={**TEST_HEADERS, "x-worker-token": "wrong"},
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 401

    response = await client.get(
        f"/v1/environments/{environment['id']}/work/poll",
        headers={**TEST_HEADERS, "x-worker-token": "worker-secret"},
        params={"worker_id": "worker-1", "lease_seconds": 30},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "leased"


async def test_trusted_worker_can_atomically_lease_a_nondefault_workspace_item():
    async with session_scope() as db:
        work = await res_q.create_resource(
            db,
            resource_type="environment_work",
            parent_id="env_tenant_b",
            status="queued",
            data={"session_id": "sess_tenant_b", "workspace_id": "wrkspc_tenant_b"},
            workspace_id="wrkspc_tenant_b",
        )
        await db.commit()
        work_id = work.id

    async with session_scope() as db:
        leased = await lease_next_work_for_worker(
            db,
            environment_id=None,
            worker_id="global-worker",
        )
        await db.commit()

    assert leased is not None
    assert leased.id == work_id
    assert leased.workspace_id == "wrkspc_tenant_b"
    assert leased.status == "leased"
