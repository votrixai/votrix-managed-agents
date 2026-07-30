"""Scenarios 27–32: what happens around a turn rather than inside one.

Dispatch is `inline`, so `POST /events` holds the connection for the whole
turn. Every test here that needs to do something *during* a turn fires that
POST as a task and works alongside it — which is also exactly what a real
client does, from another process.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests_live.helpers import (
    AGENT_CUSTOM_TOOL_USE,
    SESSION_ERROR,
    answer,
    call_named,
    download,
    events_after,
    kinds,
    message,
    of_type,
    pending,
    respond,
    results,
    run_turn,
    said,
    status,
    stop_reason,
    tool_output,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

LONG_TASK = (
    "Write a detailed essay of at least 1200 words on the history of the "
    "shipping container. Do not use any tools; just write it out."
)


def fire(api, session_id: str, text: str) -> asyncio.Task:
    """Start a turn without waiting for it."""
    return asyncio.create_task(
        api.post(f"/v1/sessions/{session_id}/events", json={"events": [message(text)]})
    )


async def wait_until_running(api, session_id: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await status(api, session_id) == "running":
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"session {session_id} never started running")


async def test_27_a_new_message_cancels_what_was_pending(api, session):
    """Changing your mind instead of answering.

    The pending calls are not answered; a message arrives instead. LangGraph
    cancels every one of them and the model carries on from there, so the
    session must come back to a plain `end_turn` with nothing still waiting.
    """
    events = await run_turn(
        api,
        session,
        "Use the get_crm_record tool to look up customers C001 and C002 at the "
        "same time, in one reply.",
    )
    assert len(pending(events)) == 2

    after = await run_turn(
        api,
        session,
        "Forget the CRM lookups. Just say the word ABANDONED and nothing else.",
    )

    assert stop_reason(after)["type"] == "end_turn", stop_reason(after)
    assert "abandoned" in said(after), said(after)


async def test_28_an_interrupt_stops_a_running_turn(api, session):
    turn = fire(api, session, LONG_TASK)
    await wait_until_running(api, session)
    await asyncio.sleep(5)

    stopped = await api.post(
        f"/v1/sessions/{session}/events", json={"events": [{"type": "user.interrupt"}]}
    )
    stopped.raise_for_status()

    (await turn).raise_for_status()

    detail = (await api.get(f"/v1/sessions/{session}")).json()
    assert detail["status"] == "idle", detail
    assert detail["stop_reason"] == {"type": "interrupted"}, detail

    # Nothing was written after the session went idle: the emitter refuses the
    # cancelled turn's next write rather than letting it trail in afterwards.
    events = await events_after(api, session, 0)
    idle_at = max(e["seq"] for e in events if e["type"] == "session.status_idle")
    assert [e["type"] for e in events if e["seq"] > idle_at] == [], events

    # And the conversation continues.
    resumed = await run_turn(api, session, "Say the word CONTINUED and nothing else.")
    assert "continued" in said(resumed), said(resumed)


async def test_28b_concurrent_appends_never_take_the_same_seq(
    api, organization, agent, environment
):
    """The race scenario 28 exposed, on its own and without a model.

    Scenario 28 is a real regression test but a timing-dependent one: it can
    pass by being lucky. This provokes the same collision directly — several
    appends to one session at once, each on its own database session, which is
    exactly the shape of a worker emitting output while an interrupt arrives.

    It needs no sandbox and no model, so it costs seconds. Before the sequence
    number was allocated by the database it failed here every time.

    Eight at once, not eighty: each needs its own connection, and the pooler in
    front of this database hands out fifteen in total. Provoking the race takes
    two — the rest is margin, not ambition.
    """
    from app.db.engine import session_scope
    from app.db.queries import sessions as sessions_q

    async with session_scope() as db:
        row = await sessions_q.create_session(
            db,
            organization_id=organization,
            agent_id=agent,
            agent_version=1,
            environment_id=environment,
            title="seq race",
        )
        await db.commit()
        session_id = row.id

    async def append(index: int) -> int:
        async with session_scope() as db:
            live = await sessions_q.get_session_by_id(db, session_id=session_id)
            event = await sessions_q.append_event(
                db,
                live,
                type="agent.message",
                source="agent",
                payload={"content": [{"type": "text", "text": str(index)}]},
            )
            await db.commit()
            return event.seq

    seqs = await asyncio.gather(*[append(i) for i in range(8)])

    assert sorted(seqs) == list(range(1, 9)), sorted(seqs)

    # And the log agrees: no gaps, no repeats.
    written = await events_after(api, session_id, 0)
    assert [event["seq"] for event in written] == list(range(1, 9))


async def test_29_one_id_ties_the_call_to_its_answer_across_the_pause(api, session):
    """The same string in four places, with no translation anywhere.

    It is the engine's own id for the call. We announce it, we list it in the
    stop reason, the client sends it back, and it comes out on the result —
    across a pause, and in production across two processes.
    """
    events = await run_turn(
        api, session, "Use the get_crm_record tool to look up customer C001."
    )

    announced = call_named(events, "get_crm_record")
    assert announced["type"] == AGENT_CUSTOM_TOOL_USE
    call_id = announced["tool_use_id"]

    # 1. announced on the call, 2. listed in the stop reason
    assert pending(events) == [call_id]

    # 3. sent back by the client, unchanged
    record = "C001 = Northwind Traders"
    after = await answer(api, session, [respond(call_id, record)])

    # 4. and it is the key the result arrives under
    assert call_id in results(after), results(after)
    assert record in tool_output(after, call_id)

    # The client's own answer is stored under the same string too, so a
    # transcript reads straight through without a lookup.
    sent = of_type(after, "user.custom_tool_result")
    assert [event["custom_tool_use_id"] for event in sent] == [call_id]


async def test_30_a_file_can_be_taken_out_mid_turn(api, session):
    """A deliverable is finished long before the turn is."""
    turn = fire(
        api,
        session,
        "First use write_file to write exactly the text LIVE-THIRTY to "
        "outputs/live.txt. Then read uploads/notes.txt, uploads/readme.md, "
        "uploads/config.json and uploads/revenue.csv one at a time, and write a "
        "detailed paragraph about each one.",
    )
    await wait_until_running(api, session)

    captured = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and captured is None:
        if turn.done():
            break
        response = await api.post(
            f"/v1/sessions/{session}/live/files", json={"path": "live.txt"}
        )
        if response.status_code == 200:
            captured = response.json()
            break
        await asyncio.sleep(2)

    assert captured is not None, "the agent never produced outputs/live.txt in time"
    # Where it lives in the bucket is ours. A client gets a signed URL or
    # nothing, and never a key.
    assert "storage_key" not in captured, captured
    assert captured["scope"] == {"type": "session", "id": session}

    assert b"LIVE-THIRTY" in await download(api, captured["id"])

    (await turn).raise_for_status()


async def test_31_a_second_message_is_refused_and_leaves_no_trace(api, session):
    """There is no queue, so a refusal has to mean nothing was written."""
    turn = fire(api, session, LONG_TASK)
    await wait_until_running(api, session)

    refused = await api.post(
        f"/v1/sessions/{session}/events",
        json={"events": [message("SHOULD-NEVER-BE-ACCEPTED")]},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "session_busy", refused.json()

    (await turn).raise_for_status()

    events = await events_after(api, session, 0)
    written = " ".join(str(event) for event in events)
    assert "SHOULD-NEVER-BE-ACCEPTED" not in written


async def test_32_a_session_whose_container_is_gone_ends_for_good(api, session, mark_sandbox_gone):
    """E2B reclaimed the container. The next turn cannot run and must say so,
    rather than fail somewhere deep inside a connect."""
    await run_turn(api, session, "Say the word READY and nothing else.")
    await mark_sandbox_gone(session)

    events = await run_turn(api, session, "Read uploads/notes.txt.")

    errors = of_type(events, SESSION_ERROR)
    assert errors, kinds(events)
    assert errors[-1]["error"]["type"] == "sandbox_unavailable", errors[-1]

    # The session says it is over, and says so as an event. A client that does
    # not recognise `sandbox_unavailable` still has to be able to stop waiting.
    assert kinds(events)[-1] == "session.status_terminated", kinds(events)

    detail = (await api.get(f"/v1/sessions/{session}")).json()
    assert detail["status"] == "terminated", detail

    # And it stays ended: nothing further is accepted.
    refused = await api.post(
        f"/v1/sessions/{session}/events", json={"events": [message("anyone there?")]}
    )
    assert refused.status_code == 409, refused.text


@pytest.fixture
def mark_sandbox_gone():
    """Record what a reaper would record when E2B takes a container back.

    Killing the container at the provider is not enough on its own: nothing
    tells us it happened, and the session row still says `running`. This writes
    the fact, which is the state the code under test actually reads.
    """

    async def _mark(session_id: str) -> None:
        from app.db.engine import session_scope
        from app.db.queries import sessions as sessions_q
        from app.utils.sandbox import Sandbox

        async with session_scope() as db:
            row = await sessions_q.get_session_by_id(db, session_id=session_id)
            sandbox = await sessions_q.get_sandbox(
                db, session_id=session_id, organization_id=row.organization_id
            )
            container = Sandbox.from_id(
                sandbox.external_sandbox_id, session_id, row.organization_id
            )
            await sessions_q.update_sandbox_state(db, sandbox, state="terminated")
            await db.commit()
        try:
            await container.kill()
        except Exception:
            pass

    return _mark
