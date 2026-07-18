from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.worker import run_worker
from tests.conftest import TEST_HEADERS


async def _create_agent(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Hybrid Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "Hybrid Environment", "config": {"type": "cloud"}},
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


def _enable_hybrid(monkeypatch):
    monkeypatch.setenv("VMA_WORK_DISPATCH_MODE", "hybrid")
    get_settings.cache_clear()


async def test_send_events_dispatches_after_commit_without_inline_execution(
    client,
    monkeypatch,
):
    _enable_hybrid(monkeypatch)
    dispatched: list[tuple[str, int]] = []

    async def fake_dispatch(work_id, *, attempt, schedule_at=None):
        assert schedule_at is None
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            assert work is not None
            assert work.status == "queued"
        dispatched.append((work_id, attempt))

    async def fail_inline(*_args, **_kwargs):
        raise AssertionError("hybrid dispatch must be mutually exclusive with inline execution")

    monkeypatch.setattr("app.routers.sessions.dispatch_work", fake_dispatch)
    monkeypatch.setattr("app.routers.sessions.execute_work_item", fail_inline)
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "run with push"}]},
    )

    assert response.status_code == 200, response.text
    assert len(dispatched) == 1
    assert dispatched[0][1] == 0


async def test_resume_dispatches_after_commit_in_hybrid_mode(client, monkeypatch):
    _enable_hybrid(monkeypatch)
    dispatched: list[tuple[str, int]] = []

    async def fake_dispatch(work_id, *, attempt, schedule_at=None):
        assert schedule_at is None
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            assert work is not None
            assert work.status == "queued"
        dispatched.append((work_id, attempt))

    async def fail_inline(*_args, **_kwargs):
        raise AssertionError("hybrid resume must not also execute inline")

    monkeypatch.setattr("app.routers.sessions.dispatch_work", fake_dispatch)
    monkeypatch.setattr("app.routers.sessions.execute_work_item", fail_inline)
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/resume",
        headers=TEST_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert len(dispatched) == 1
    assert dispatched[0][1] == 0


async def test_failed_dispatch_leaves_durable_work_for_hybrid_reconciler(
    client,
    monkeypatch,
):
    _enable_hybrid(monkeypatch)
    dispatch_attempts = []

    async def failed_dispatch(work_id, *, attempt, schedule_at=None):
        dispatch_attempts.append((work_id, attempt, schedule_at))

    monkeypatch.setattr("app.routers.sessions.dispatch_work", failed_dispatch)
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "reconcile me"}]},
    )
    assert response.status_code == 200, response.text
    assert len(dispatch_attempts) == 1

    await run_worker(
        environment_id=environment["id"],
        poll_interval_seconds=0,
        once=True,
        dispatch_mode="hybrid",
    )

    stats = await client.get(
        f"/v1/environments/{environment['id']}/work/stats",
        headers=TEST_HEADERS,
    )
    assert stats.status_code == 200, stats.text
    assert stats.json()["completed"] == 1
