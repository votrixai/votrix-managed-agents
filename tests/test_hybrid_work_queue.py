from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.runtime.runner import _admit_graph_execution
from app.runtime.work_queue import (
    WorkLeaseError,
    execute_work_item,
    lease_next_work_for_worker,
)
from tests.conftest import TEST_HEADERS


async def _queued_session(client):
    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Push Race Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "Push Race Environment", "config": {"type": "self_hosted"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {
                "type": "agent",
                "id": agent_response.json()["id"],
                "version": 1,
            },
            "environment_id": environment_response.json()["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()
    events_response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "execute once"}]},
    )
    assert events_response.status_code == 200, events_response.text
    return session, environment_response.json()


async def _lease_for_poller(environment_id: str):
    async with session_scope() as db:
        work = await lease_next_work_for_worker(
            db,
            environment_id=environment_id,
            worker_id="poller-owner",
            lease_seconds=30,
        )
        assert work is not None
        lease = dict((work.data or {}).get("lease") or {})
        await db.commit()
        return work.id, lease


async def test_direct_push_observes_live_foreign_lease_without_stealing_it(
    client,
    monkeypatch,
):
    _session, environment = await _queued_session(client)
    work_id, lease = await _lease_for_poller(environment["id"])

    async def fail_if_executed(*_args, **_kwargs):
        raise AssertionError("a live foreign lease must not execute twice")

    monkeypatch.setattr("app.runtime.runner.run_session_turn", fail_if_executed)
    assert await execute_work_item(work_id, worker_id="push-contender") == "already_running"

    with pytest.raises(WorkLeaseError):
        await execute_work_item(
            work_id,
            worker_id="push-contender",
            lease_id="wrong-lease",
            lease_generation=int(lease["generation"]),
        )

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
    assert work is not None
    assert work.status == "leased"
    assert work.data["attempt"] == 0
    assert work.data["lease"]["worker_id"] == "poller-owner"


async def test_direct_push_reclaims_expired_foreign_lease(
    client,
    monkeypatch,
):
    _session, environment = await _queued_session(client)
    work_id, lease = await _lease_for_poller(environment["id"])
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        assert work is not None
        data = dict(work.data or {})
        data["lease"] = {
            **dict(data["lease"]),
            "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
        }
        await res_q.update_resource(db, work, data=data, status="leased")
        await db.commit()

    observed_leases = []

    async def defer_turn(_session_id, *, work_lease, **_kwargs):
        observed_leases.append(work_lease)
        return False

    monkeypatch.setattr("app.runtime.runner.run_session_turn", defer_turn)
    result = await execute_work_item(work_id, worker_id="push-recovery", lease_seconds=30)

    assert result == "deferred"
    assert len(observed_leases) == 1
    assert observed_leases[0].worker_id == "push-recovery"
    assert observed_leases[0].generation == int(lease["generation"]) + 1
    assert observed_leases[0].attempt == 0


async def test_rescheduling_dispatches_after_commit_with_current_attempt(
    client,
    monkeypatch,
):
    session, environment = await _queued_session(client)
    work_id, lease = await _lease_for_poller(environment["id"])
    monkeypatch.setenv("VMA_WORK_DISPATCH_MODE", "hybrid")
    get_settings.cache_clear()
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    dispatches = []

    async def reschedule_turn(session_id, *, organization_id, work_lease, **_kwargs):
        admitted = await _admit_graph_execution(
            session_id=session_id,
            organization_id=organization_id,
            work_lease=work_lease,
        )
        assert admitted.attempt == 1
        async with session_scope() as db:
            stored_session = await sessions_q.get_session(
                db,
                session_id,
                organization_id=organization_id,
                for_update=True,
            )
            assert stored_session is not None
            await sessions_q.update_session(
                db,
                stored_session,
                status="rescheduling",
                stop_reason={
                    "type": "rescheduling",
                    "retry_at": retry_at.isoformat(),
                    "retry_after_seconds": 120,
                },
            )
            await db.commit()
        return True

    async def fake_dispatch(work_id_arg, *, attempt, schedule_at=None):
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id_arg)
            assert work is not None
            assert work.status == "rescheduling"
        dispatches.append((work_id_arg, attempt, schedule_at))

    monkeypatch.setattr("app.runtime.runner.run_session_turn", reschedule_turn)
    monkeypatch.setattr("app.runtime.dispatch.dispatch_work", fake_dispatch)
    outcome = await execute_work_item(
        work_id,
        worker_id="poller-owner",
        lease_id=str(lease["lease_id"]),
        lease_generation=int(lease["generation"]),
        lease_seconds=30,
    )

    assert outcome == "rescheduling"
    assert dispatches == [(work_id, 1, retry_at)]
    async with session_scope() as db:
        stored_session = await sessions_q.get_session(
            db,
            session["id"],
            organization_id="org_test",
        )
    assert stored_session is not None
    assert stored_session.status == "rescheduling"
