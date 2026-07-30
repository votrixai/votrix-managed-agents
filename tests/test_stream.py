"""The SSE stream: following a session's events live.

Most of this goes at the generator rather than at the endpoint, because httpx's
`ASGITransport` cannot carry a streaming response — it waits for the response to
complete before handing one back, and a stream that stays open never completes.
So the cursor, the resume, the heartbeat and the wire format are exercised
directly, and the endpoint is checked with a stream that does end.

That a reader sees events appear as a turn produces them is proven against a
real database rather than here: one connection ran a turn while another polled,
and the ten events arrived one at a time, four to eighteen seconds apart.
Reproducing that on SQLite would test the driver's locking, not this code.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.queries import sessions as sessions_q
from app.main import app
from app.routers.deps import get_db
from app.routers.sessions import _seq_from, _sse
from app.services import sessions as service


@pytest_asyncio.fixture
def headers(org):
    return {"x-organization-id": org, "x-api-key": "anything"}


@pytest.fixture
def scoped(db, monkeypatch):
    """Point the stream's own session handling at the test database.

    It opens its own rather than taking the request's, because a stream can
    outlive a request by half an hour and holding a pooled connection for that
    long spends the pool on readers instead of on work.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield db

    monkeypatch.setattr("app.services.sessions.session_scope", _scope)
    monkeypatch.setattr("app.routers.sessions.session_scope", _scope)


@pytest_asyncio.fixture
async def client(db, scoped):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def write(db, session, type_="agent.message", payload=None):
    event = await sessions_q.append_event(
        db, session, type=type_, source="agent",
        payload=payload or {"content": [{"type": "text", "text": "hi"}]},
    )
    await db.commit()
    return event


async def take(session_id, org, *, count, after_seq=None, timeout=5.0):
    """Pull `count` events off the stream, then stop.

    The stream goes on waiting for more, which is the point of it — a test has
    to be the one that says it has seen enough.
    """
    got: list = []

    async def pump():
        async for event in service.stream_events(
            session_id=session_id, organization_id=org, after_seq=after_seq
        ):
            if event is None:
                continue
            got.append(event)
            if len(got) >= count:
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return got


# --- what a reader sees ------------------------------------------------------


async def test_a_reader_with_no_cursor_gets_the_transcript_from_the_start(
    db, org, session, scoped
):
    """A refreshed page should not have to call `/events` first."""
    await write(db, session, "user.message", {"content": [{"type": "text", "text": "one"}]})
    await write(db, session, "agent.message", {"content": [{"type": "text", "text": "two"}]})

    got = await take(session.id, org, count=2)

    assert [e.type for e in got] == ["user.message", "agent.message"]


async def test_a_cursor_skips_what_was_already_seen(db, org, session, scoped):
    first = await write(db, session, "user.message", {"content": [{"type": "text", "text": "one"}]})
    await write(db, session, "agent.message", {"content": [{"type": "text", "text": "two"}]})

    got = await take(session.id, org, count=1, after_seq=first.seq)

    assert [e.payload["content"][0]["text"] for e in got] == ["two"]


async def test_nothing_new_is_reported_as_a_tick(db, org, session, scoped):
    """A turn can think for minutes without emitting. The stream says so rather
    than going silent, so the endpoint can keep the connection warm."""

    async def first():
        async for event in service.stream_events(session_id=session.id, organization_id=org):
            return event

    assert await asyncio.wait_for(first(), timeout=5.0) is None


async def test_a_terminated_session_ends_the_stream(db, org, session, scoped):
    """Nothing more will ever be written, so the connection is not held open
    waiting for it. This finishing at all is the assertion."""
    await write(db, session)
    await sessions_q.release_session(db, session, status="terminated")
    await db.commit()

    got = [
        event
        async for event in service.stream_events(session_id=session.id, organization_id=org)
        if event is not None
    ]

    assert [e.type for e in got] == ["agent.message"]


async def test_another_tenant_is_refused_up_front(db, session, scoped):
    """Before the stream opens, so it is a failed request rather than a
    connection that opens and says nothing."""
    from app.models.errors import NotFound

    with pytest.raises(NotFound):
        await take(session.id, "org_someone_else", count=1, timeout=2.0)


# --- the wire format ---------------------------------------------------------


class FakeRequest:
    """Stands in for the client connection, which never goes away here."""

    async def is_disconnected(self):
        return False


async def test_an_event_is_framed_with_its_seq_as_the_id(db, org, session, scoped):
    """That id is what a reconnecting client sends back, so it has to be the
    thing the resume cursor understands."""
    event = await write(db, session, "agent.message", {"content": [{"type": "text", "text": "hello"}]})

    async for frame in _sse(
        FakeRequest(), session_id=session.id, organization_id=org, after_seq=None
    ):
        assert f"id: {event.seq}" in frame
        assert "event: agent.message" in frame
        assert "hello" in frame
        assert frame.endswith("\n\n")
        break


async def test_a_quiet_stream_sends_a_keep_alive(db, org, session, scoped, monkeypatch):
    monkeypatch.setattr(service, "STREAM_HEARTBEAT_SECONDS", 0)

    async for frame in _sse(
        FakeRequest(), session_id=session.id, organization_id=org, after_seq=None
    ):
        assert frame == ": keep-alive\n\n"
        break


# --- resuming ----------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [("12", 12), (None, None), ("not-a-number", None)])
def test_the_last_event_id_header_becomes_the_cursor(header, expected):
    """What a browser's `EventSource` sends by itself after a dropped
    connection. Something unreadable replays from the start — wasteful, and
    never the silent skipping of events the client has not seen.
    """
    assert _seq_from(header) == expected


# --- the route ---------------------------------------------------------------


async def test_stream_is_not_swallowed_by_the_single_event_route(client, headers, db, session):
    """`/events/{event_id}` is declared beside this one and would match
    `stream` as an id. The order is load-bearing and nothing else would notice —
    the stream would simply 404 as an unknown event.

    Terminated, so the response actually ends: httpx's ASGI transport waits for
    that before handing anything back.
    """
    await write(db, session)
    await sessions_q.release_session(db, session, status="terminated")
    await db.commit()

    response = await client.get(f"/v1/sessions/{session.id}/events/stream", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: agent.message" in response.text


async def test_a_single_event_can_still_be_fetched_by_id(client, headers, db, session):
    """The route above must not shadow this one either."""
    event = await write(db, session)

    response = await client.get(
        f"/v1/sessions/{session.id}/events/{event.id}", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == event.id


async def test_someone_elses_session_is_a_404(client, db, session):
    response = await client.get(
        f"/v1/sessions/{session.id}/events/stream",
        headers={"x-organization-id": "org_someone_else", "x-api-key": "anything"},
    )

    assert response.status_code == 404
