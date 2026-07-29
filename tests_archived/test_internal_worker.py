from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.factory import create_app
from tests.conftest import TEST_HEADERS


def _configure_worker(monkeypatch, *, turn_limit: int = 5) -> None:
    monkeypatch.setenv("VMA_SERVICE_ROLE", "worker")
    monkeypatch.setenv("VMA_EMBEDDED_WORKER_ENABLED", "false")
    monkeypatch.setenv("VMA_WORKER_TURN_LIMIT", str(turn_limit))
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "error",
        "stopped",
        "exhausted",
        "rescheduling",
        "already_running",
        "superseded",
        "missing",
        "not_runnable",
    ],
)
async def test_worker_push_terminal_and_owned_outcomes_return_200(
    monkeypatch,
    outcome,
):
    _configure_worker(monkeypatch)
    calls: list[tuple[str, dict]] = []

    async def fake_execute_work_item(work_id, **kwargs):
        calls.append((work_id, kwargs))
        return outcome

    monkeypatch.setattr(
        "app.routers.internal_work.execute_work_item",
        fake_execute_work_item,
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/internal/work/work-1/execute")

    assert response.status_code == 200
    assert response.json() == {"outcome": outcome}
    assert calls[0][0] == "work-1"
    assert calls[0][1]["lease_seconds"] == get_settings().vma_worker_lease_seconds
    assert calls[0][1]["worker_id"].startswith("push-")


async def test_worker_push_deferred_returns_retryable_503(monkeypatch):
    _configure_worker(monkeypatch)

    async def fake_execute_work_item(*_args, **_kwargs):
        return "deferred"

    monkeypatch.setattr(
        "app.routers.internal_work.execute_work_item",
        fake_execute_work_item,
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/internal/work/work-1/execute")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "15"
    assert response.json() == {"outcome": "deferred"}


async def test_repeated_deferred_pushes_do_not_consume_attempts(client, monkeypatch):
    agent = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Deferred Push Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent.status_code == 201, agent.text
    environment = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "Deferred Push Environment", "config": {"type": "self_hosted"}},
    )
    assert environment.status_code == 201, environment.text
    session = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent.json()["id"], "version": 1},
            "environment_id": environment.json()["id"],
        },
    )
    assert session.status_code == 201, session.text
    submitted = await client.post(
        f"/v1/sessions/{session.json()['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "stay deferred"}]},
    )
    assert submitted.status_code == 200, submitted.text
    async with session_scope() as db:
        resources = await res_q.list_resources(
            db,
            resource_type="environment_work",
            parent_id=environment.json()["id"],
            limit=10,
        )
    assert len(resources) == 1
    work_id = resources[0].id

    async def defer_without_admission(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.runtime.runner.run_session_turn", defer_without_admission)
    _configure_worker(monkeypatch)
    worker_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=worker_app),
        base_url="http://testserver",
    ) as worker_client:
        for _ in range(3):
            response = await worker_client.post(f"/internal/work/{work_id}/execute")
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "15"

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
    assert work is not None
    assert work.status == "queued"
    assert work.data["attempt"] == 0


@pytest.mark.parametrize("failure", [RuntimeError("db unavailable"), "unknown"])
async def test_worker_push_unexpected_results_return_500(monkeypatch, failure):
    _configure_worker(monkeypatch)

    async def fake_execute_work_item(*_args, **_kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(
        "app.routers.internal_work.execute_work_item",
        fake_execute_work_item,
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/internal/work/work-1/execute")

    assert response.status_code == 500
    assert response.json() == {"outcome": "internal_error"}


async def test_worker_push_waits_for_shared_turn_capacity(monkeypatch):
    _configure_worker(monkeypatch, turn_limit=1)
    execution_started = asyncio.Event()

    async def fake_execute_work_item(*_args, **_kwargs):
        execution_started.set()
        return "completed"

    monkeypatch.setattr(
        "app.routers.internal_work.execute_work_item",
        fake_execute_work_item,
    )
    app = create_app()
    limiter = app.state.turn_limiter
    assert limiter.acquire_nowait()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        request = asyncio.create_task(
            client.post("/internal/work/work-waits/execute")
        )
        await asyncio.sleep(0)
        assert not execution_started.is_set()
        assert not request.done()

        limiter.release()
        response = await asyncio.wait_for(request, timeout=1)

    assert response.status_code == 200
    assert execution_started.is_set()
    assert limiter.acquire_nowait()
    limiter.release()


@pytest.mark.parametrize("role", ["api", "combined"])
async def test_internal_work_route_is_absent_from_non_worker_roles(
    monkeypatch,
    role,
):
    monkeypatch.setenv("VMA_SERVICE_ROLE", role)
    monkeypatch.setenv("VMA_EMBEDDED_WORKER_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/internal/work/work-1/execute")

    assert response.status_code == 404
