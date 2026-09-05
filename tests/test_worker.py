"""A lost worker still has to end the turn on the public event stream."""

from contextlib import asynccontextmanager

from app import worker
from app.db.queries import sessions as sessions_q
from app.models import events as event_models
from app.models.sessions import IDLE, STOP_ERROR


async def test_sweeper_ends_a_lost_turn_with_a_valid_idle_event(
    db, session, monkeypatch
):
    assert await sessions_q.claim_session(
        db, session_id=session.id, lease_seconds=-1
    )
    await db.commit()
    await db.refresh(session)

    @asynccontextmanager
    async def scope():
        yield db

    monkeypatch.setattr(worker, "session_scope", scope)

    assert await worker.sweep_once() == 1

    # Reload after a rollback to prove sweep_once committed this state.
    await db.rollback()
    await db.refresh(session)
    events = (
        await sessions_q.list_events(
            db,
            session_id=session.id,
            organization_id=session.organization_id,
        )
    ).items

    assert session.status == IDLE
    assert session.stop_reason == {"type": STOP_ERROR}
    assert session.lease_expires_at is None
    assert [(event.type, event.payload) for event in events] == [
        ("session.error", {"error": {"type": "worker_lost"}}),
        ("session.status_idle", {"stop_reason": {"type": STOP_ERROR}}),
    ]
    assert event_models.from_row(events[-1]).stop_reason.type == STOP_ERROR
