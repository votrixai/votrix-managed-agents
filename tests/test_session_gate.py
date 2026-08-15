"""The rule the whole design rests on: one turn at a time, no queue.

A message is accepted only if it can claim the session. There is nothing to
hold a refused message, so "refused" has to mean nothing was written at all.
"""

from __future__ import annotations

import pytest

from app.db.queries import sessions as sessions_q
from app.models.sessions import (
    IDLE,
    RUNNING,
    STOP_INTERRUPTED,
    STOP_REQUIRES_ACTION,
    TERMINATED,
)
from app.services import sessions as service
from app.models.errors import Conflict, NotFound, SessionBusy, SessionCancelled

MESSAGE = {"type": "user.message", "content": [{"type": "text", "text": "hi"}]}
INTERRUPT = {"type": "user.interrupt"}
ACTION_RESULT = {
    "type": "user.custom_tool_result",
    "tool_use_id": "call_1",
    "content": [{"type": "text", "text": "30 rows"}],
}

# Captured before any fixture runs, because `never_dispatch` replaces it.
REAL_DISPATCH = service._dispatch_turn


async def send(db, session, message=MESSAGE, org=None):
    return await service.send_events(
        db,
        session_id=session.id,
        organization_id=org or session.organization_id,
        events=[message],
    )


async def test_first_message_claims_the_session(db, session):
    events = await send(db, session)

    await db.refresh(session)
    assert [e.seq for e in events] == [1]
    assert session.status == RUNNING
    assert session.lease_expires_at is not None


async def test_second_message_is_refused_while_busy(db, session):
    await send(db, session)

    with pytest.raises(SessionBusy):
        await send(db, session)


async def test_a_refused_message_writes_nothing(db, session):
    await send(db, session)
    before = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items

    with pytest.raises(SessionBusy):
        await send(db, session)

    after = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items
    assert len(after) == len(before)


async def test_a_freed_session_accepts_the_next_message(db, session):
    await send(db, session)
    await sessions_q.release_session(db, session, status=IDLE)
    await db.commit()

    events = await send(db, session)
    assert events[0].seq == 2


async def test_an_expired_lease_lets_the_next_message_through(db, session):
    """A worker that dies must not lock the session out forever."""
    await sessions_q.claim_session(db, session_id=session.id, lease_seconds=-1)
    await db.commit()

    events = await send(db, session)
    assert events[0].seq == 1


async def test_dispatch_happens_after_the_commit(db, session, never_dispatch):
    """The worker may start immediately, so the row has to be there first."""
    await send(db, session)

    assert len(never_dispatch.calls) == 1
    assert never_dispatch.calls[0]["session_id"] == session.id


async def test_another_organization_cannot_see_the_session(db, session):
    with pytest.raises(NotFound):
        await service.get_session(
            db, session_id=session.id, organization_id="org_someone_else"
        )


# --- interrupts --------------------------------------------------------------


async def test_interrupt_reaches_a_busy_session(db, session):
    """The one message that must not be subject to the gate."""
    await send(db, session)

    await send(db, session, INTERRUPT)

    await db.refresh(session)
    assert session.status == IDLE
    assert session.lease_expires_at is None


async def test_interrupt_records_why_the_turn_stopped(db, session):
    sent = await send(db, session)

    await send(db, session, INTERRUPT)

    await db.refresh(session)
    assert session.stop_reason == {"type": STOP_INTERRUPTED}


async def test_interrupt_records_that_the_turn_ended(db, session):
    """Without this the client waits forever — the engine never gets to say so."""
    await send(db, session)
    await send(db, session, INTERRUPT)

    events = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items
    assert [e.type for e in events] == [
        "user.message",
        "user.interrupt",
        "session.status_idle",
    ]


async def test_interrupting_an_idle_session_leaves_it_alone(db, session):
    await send(db, session, INTERRUPT)

    await db.refresh(session)
    assert session.status == IDLE
    assert session.stop_reason is None
    events = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items
    assert [e.type for e in events] == ["user.interrupt"]


async def test_interrupting_twice_is_harmless(db, session):
    await send(db, session)
    await send(db, session, INTERRUPT)
    await send(db, session, INTERRUPT)

    await db.refresh(session)
    assert session.status == IDLE


# --- a turn that stopped to ask ----------------------------------------------
#
# The gap the gate used to leave open. A turn that pauses on a tool the client
# has to answer *ends* — the session goes idle, the lease is dropped — and yet
# the graph is still holding that call open. Anything else claiming the session
# restarts the agent from the top and the call is cancelled on the way past, so
# for as long as the answer is owed, only the answer gets in.


async def park(db, session):
    """Leave the session where a turn that stopped to ask leaves it."""
    await sessions_q.release_session(
        db, session, status=IDLE, stop_reason={"type": STOP_REQUIRES_ACTION}
    )
    await db.commit()


async def test_a_parked_session_refuses_an_ordinary_message(db, session):
    await park(db, session)

    with pytest.raises(SessionBusy):
        await send(db, session)


async def test_a_parked_session_accepts_the_answer_it_is_waiting_for(db, session):
    await park(db, session)

    events = await send(db, session, ACTION_RESULT)

    await db.refresh(session)
    assert [e.type for e in events] == ["user.custom_tool_result"]
    assert session.status == RUNNING


async def test_claiming_clears_the_parking(db, session):
    """`stop_reason` describes the last turn that finished, so a turn that has
    just started must not be found still wearing the previous one's."""
    await park(db, session)

    await send(db, session, ACTION_RESULT)

    await db.refresh(session)
    assert session.stop_reason is None


async def test_a_message_refused_by_the_parking_writes_nothing(db, session):
    await park(db, session)

    with pytest.raises(SessionBusy):
        await send(db, session)

    events = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items
    assert events == []


async def test_interrupt_unparks_a_session_nobody_is_going_to_answer(db, session):
    """The only way out. Without it a client that dies owing an answer leaves
    the session refusing every message forever."""
    await park(db, session)

    await send(db, session, INTERRUPT)

    await db.refresh(session)
    assert session.stop_reason == {"type": STOP_INTERRUPTED}


async def test_an_unparked_session_takes_messages_again(db, session):
    await park(db, session)
    await send(db, session, INTERRUPT)

    events = await send(db, session)

    await db.refresh(session)
    assert [e.type for e in events] == ["user.message"]
    assert session.status == RUNNING


async def test_unparking_says_so_rather_than_leaving_the_client_waiting(db, session):
    """The client was told `requires_action` and is holding a spinner on it."""
    await park(db, session)

    await send(db, session, INTERRUPT)

    events = (await sessions_q.list_events(
        db, session_id=session.id, organization_id=session.organization_id
    )).items
    assert [e.type for e in events] == ["user.interrupt", "session.status_idle"]
    assert events[-1].payload["stop_reason"] == {"type": STOP_INTERRUPTED}


async def test_a_finished_turn_does_not_park_the_session(db, session):
    """Only `requires_action` closes the gate — an ordinary ending must not."""
    await sessions_q.release_session(
        db, session, status=IDLE, stop_reason={"type": "end_turn"}
    )
    await db.commit()

    events = await send(db, session)
    assert [e.type for e in events] == ["user.message"]


async def test_interrupt_must_be_sent_alone(db, session):
    with pytest.raises(Conflict):
        await service.send_events(
            db,
            session_id=session.id,
            organization_id=session.organization_id,
            events=[MESSAGE, INTERRUPT],
        )


async def test_a_terminated_session_refuses_interrupts(db, session):
    await sessions_q.release_session(db, session, status=TERMINATED)
    await db.commit()

    with pytest.raises(Conflict):
        await send(db, session, INTERRUPT)


# --- the stale worker --------------------------------------------------------


async def test_a_cancelled_worker_stops_writing(db, session):
    await send(db, session)
    await db.refresh(session)
    emit = service._emitter(db, session, session.lock_version)
    await emit("agent.message", {"text": "partial"})

    await send(db, session, INTERRUPT)

    with pytest.raises(SessionCancelled):
        await emit("agent.message", {"text": "too late"})


async def test_a_cancelled_worker_stops_even_once_a_new_turn_is_running(db, session):
    """The case a status check would miss.

    By the time the old worker writes again, the user has sent another message
    and the session is back to `running` — so `running` cannot be the test for
    whether this worker still owns it.
    """
    await send(db, session)
    await db.refresh(session)
    emit = service._emitter(db, session, session.lock_version)

    await send(db, session, INTERRUPT)
    await send(db, session)
    await db.refresh(session)
    assert session.status == RUNNING

    with pytest.raises(SessionCancelled):
        await emit("agent.message", {"text": "from the turn before"})


# --- handing the turn over ---------------------------------------------------
#
# Everything above stubs dispatch out entirely: accepting a message and running
# it are separate concerns. These are about the handover itself, on both routes.
# Neither one waits for the turn, and that is the property being pinned.


async def test_accepting_a_message_does_not_wait_for_the_turn(db, session, monkeypatch):
    """`inline` runs the turn on its own task and returns immediately.

    A request that only came back once the agent was done would hand the client
    the whole conversation at once: by the time it could open the event stream,
    every event would already be in the log, and no amount of streaming further
    down could undo that. So it is pinned here, at the handover, rather than
    left to the stream's own tests.
    """
    import asyncio
    from contextlib import asynccontextmanager

    started = asyncio.Event()
    finish = asyncio.Event()

    async def _turn_that_hangs(_db, *, session_id, events):
        started.set()
        await finish.wait()

    @asynccontextmanager
    async def _test_scope():
        # The turn opens a session of its own; in tests that has to be this
        # one, because the real one would reach the configured Postgres.
        yield db

    monkeypatch.setattr(service, "process_session", _turn_that_hangs)
    monkeypatch.setattr(service, "session_scope", _test_scope)
    # `never_dispatch` is autouse, so the real one has to go back for this test.
    monkeypatch.setattr(service, "_dispatch_turn", REAL_DISPATCH)

    # Times out rather than hanging if the request ever waits for the turn again.
    await asyncio.wait_for(send(db, session), timeout=2)
    await asyncio.wait_for(started.wait(), timeout=2)
    assert service._inline_turns, "a turn nothing holds can be collected mid-reply"

    finish.set()
    for task in list(service._inline_turns):
        await task


# --- handing the turn to Cloud Tasks -----------------------------------------


class FakeTasksClient:
    """Stands in for `CloudTasksAsyncClient`."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    async def create_task(self, request):
        self.created.append(request)


@pytest.fixture
def cloud(monkeypatch):
    """Switch dispatch to `cloud` and hand it a client that records."""
    from app.config import clear_settings_cache

    for name, value in {
        "TURN_DISPATCH": "cloud",
        "TASKS_PROJECT": "votrix-prod",
        "TASKS_LOCATION": "us-central1",
        "TASKS_QUEUE": "turns",
        "TASKS_SERVICE_ACCOUNT": "runner@votrix-prod.iam.gserviceaccount.com",
        "WORKER_URL": "https://api.votrix.example/",
    }.items():
        monkeypatch.setenv(name, value)
    clear_settings_cache()

    client = FakeTasksClient()
    monkeypatch.setattr(service, "_cloud_tasks_client", lambda: client)
    yield client
    clear_settings_cache()


async def test_a_queued_turn_calls_back_with_a_signed_request(cloud):
    await service._enqueue_task(session_id="ses_1", generation=3, events=[MESSAGE])

    request = cloud.created[0]["task"].http_request
    assert request.url == "https://api.votrix.example/internal/sessions/ses_1/process"
    # Signed for exactly the host it calls, so a token lifted off one deployment
    # is no use against another.
    assert request.oidc_token.audience == "https://api.votrix.example"
    assert (
        request.oidc_token.service_account_email
        == "runner@votrix-prod.iam.gserviceaccount.com"
    )


async def test_the_task_is_named_after_the_turn_it_runs(cloud):
    """Cloud Tasks delivers at least once. The name is what stops a second
    delivery from running the turn twice."""
    await service._enqueue_task(session_id="ses_1", generation=3, events=[MESSAGE])

    assert cloud.created[0]["task"].name.endswith("/tasks/turn-ses_1-3")


async def test_the_next_turn_on_the_same_session_gets_its_own_name(cloud):
    await service._enqueue_task(session_id="ses_1", generation=3, events=[MESSAGE])
    await service._enqueue_task(session_id="ses_1", generation=4, events=[MESSAGE])

    names = [request["task"].name for request in cloud.created]
    assert names[0] != names[1]


async def test_a_turn_already_queued_is_not_queued_again(cloud, monkeypatch):
    from google.api_core.exceptions import AlreadyExists

    async def _reject(request):
        raise AlreadyExists("that task name is taken")

    monkeypatch.setattr(cloud, "create_task", _reject)

    # The duplicate is the point: it must not surface as a failed request for a
    # message that really was accepted.
    await service._enqueue_task(session_id="ses_1", generation=3, events=[MESSAGE])


async def test_the_message_travels_with_the_task(cloud):
    """The worker is handed what to run, rather than looking it up — the event
    log is for clients, and the agent's own history lives in the checkpoint."""
    import json

    await service._enqueue_task(session_id="ses_1", generation=3, events=[MESSAGE])

    body = json.loads(cloud.created[0]["task"].http_request.body)
    assert body["events"] == [MESSAGE]


async def test_accepting_a_message_queues_it_rather_than_running_it(db, session, cloud, monkeypatch):
    """`send_events` must hand the turn over, not run it, when dispatch is
    `cloud` — the request is supposed to return before the agent starts."""
    ran: list[str] = []

    async def _should_not_run(*args, **kwargs):
        ran.append("inline")

    monkeypatch.setattr(service, "process_session", _should_not_run)
    # `never_dispatch` is autouse, so the real one has to go back for this test.
    monkeypatch.setattr(service, "_dispatch_turn", REAL_DISPATCH)

    await send(db, session)

    assert ran == []
    assert len(cloud.created) == 1


async def test_the_queued_turn_carries_the_generation_it_belongs_to(db, session, cloud, monkeypatch):
    """Two turns on one session must not collide on a task name."""
    monkeypatch.setattr(service, "process_session", lambda *a, **k: None)
    monkeypatch.setattr(service, "_dispatch_turn", REAL_DISPATCH)

    await send(db, session)
    await db.refresh(session)

    assert cloud.created[0]["task"].name.endswith(f"/tasks/turn-{session.id}-{session.lock_version}")


def test_cloud_dispatch_will_not_start_half_configured(monkeypatch):
    """A missing queue name would otherwise show up as messages that are
    accepted, committed, and then run by nobody."""
    from app.config import clear_settings_cache, get_settings

    monkeypatch.setenv("TURN_DISPATCH", "cloud")
    for name in ("TASKS_PROJECT", "TASKS_LOCATION", "TASKS_SERVICE_ACCOUNT", "WORKER_URL"):
        monkeypatch.setenv(name, "set")
    monkeypatch.delenv("TASKS_QUEUE", raising=False)
    clear_settings_cache()

    with pytest.raises(Exception) as caught:
        get_settings()
    assert "TASKS_QUEUE" in str(caught.value)

    clear_settings_cache()


# --- which model actually runs -----------------------------------------------
#
# Lives here rather than in its own file because the engine has no test module
# of its own, and this is a fact about one session's lifetime: what it was
# opened with is what it keeps running on.


@pytest.mark.parametrize(
    ("session_model", "expected"),
    [
        ({"id": "claude-opus-5"}, {"id": "claude-opus-5"}),
        (None, {"id": "agent-default"}),
    ],
    ids=["the session's own model wins", "no preference follows the agent"],
)
async def test_the_model_a_turn_runs_on(monkeypatch, session_model, expected):
    """Resolved per turn, not copied onto the session when it was created.

    A session that named no model keeps following its agent, so editing the
    agent still reaches it — which is the whole reason the column is nullable
    instead of holding a snapshot.
    """
    from types import SimpleNamespace

    from app.runtime import engine

    built: list[dict] = []

    class _Checkpoint:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Graph:
        async def aget_state(self, _config):
            return SimpleNamespace(next=(), values={"messages": []}, interrupts=())

        async def astream(self, *_args, **_kwargs):
            if False:
                yield {}

    def _capture(spec, *_args, **_kwargs):
        built.append(spec)
        return object()

    monkeypatch.setattr(engine, "_checkpoint_saver", lambda _session_id: _Checkpoint())
    monkeypatch.setattr(engine, "_build_chat_model", _capture)
    monkeypatch.setattr(engine, "read_image_tool", lambda *_a, **_kw: object())
    monkeypatch.setattr(engine, "create_deep_agent", lambda **_kw: _Graph())

    async def _emit(_event, _payload):
        return None

    await engine.execute_agent(
        session=SimpleNamespace(id="sess_model_resolution", model=session_model),
        version=SimpleNamespace(
            agent_id="agent_model_resolution",
            version=1,
            tools=[{"type": engine.AGENT_TOOLSET}],
            skills=[],
            model={"id": "agent-default"},
            system=None,
        ),
        events=[MESSAGE],
        sandbox=SimpleNamespace(to_deep_agent_backend=object()),
        emit=_emit,
        # Which model runs and whose key pays are separate questions; this test
        # only asks the first, so any credential will do.
        inference_key="sk-or-v1-test",
    )

    assert built == [expected]
