"""Scenarios 45–48: the tools nothing had ever called.

`ask_user` is the second custom tool and had never been invoked. `write_todos`
and `task` were forbidden outright by the strict agent's system prompt, which
kept batches predictable and left both with no coverage — and `task` is the
*other* `always_ask` tool, so until now exactly one tool had ever exercised the
permission path. These run against `planning_agent`, which lifts that ban.

Scenario 47 is here to record a gap rather than a feature: a sub-agent's own
work does not reach our event stream, and the test says so out loud.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests_live.helpers import (
    AGENT_CUSTOM_TOOL_USE,
    AGENT_TOOL_RESULT,
    AGENT_TOOL_USE,
    allow,
    answer,
    call_named,
    calls,
    deny,
    pending,
    respond,
    results,
    run_turn,
    said,
    stop_reason,
    tool_output,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def planning_session(new_session, planning_agent) -> str:
    """A session on the agent that is allowed to plan and delegate."""
    return await new_session(agent_id=planning_agent)


async def test_45_the_second_custom_tool_is_answered_by_the_client(api, session):
    """`ask_user` exists to put a question to the person, and the client
    answering it *is* the tool's implementation."""
    events = await run_turn(
        api,
        session,
        "Use the ask_user tool to ask me which region I want the revenue "
        "report for. Do not guess.",
    )

    call = call_named(events, "ask_user")
    assert call["type"] == AGENT_CUSTOM_TOOL_USE
    assert pending(events) == [call["tool_use_id"]]

    after = await answer(api, session, [respond(call["tool_use_id"], "north")])

    assert "north" in tool_output(after, call["tool_use_id"])
    assert "north" in said(after), said(after)


async def test_46_write_todos_runs_without_asking(api, planning_session):
    """It is `always_allow`, so it should appear, resolve, and never pause."""
    events = await run_turn(
        api,
        planning_session,
        "Use the write_todos tool to write a three-item plan for analysing "
        "uploads/revenue.csv, then tell me the plan.",
    )

    call = call_named(events, "write_todos")
    assert call["evaluated_permission"] == "allow", call
    assert stop_reason(events)["type"] == "end_turn", stop_reason(events)
    assert call["tool_use_id"] in results(events), results(events)


async def test_47_delegating_asks_first_and_hides_its_work(api, planning_session):
    """`task` is the other `always_ask` tool, and the only one that starts a
    second agent.

    Two things are asserted and the second is a gap, not a feature: the
    sub-agent's own tool calls never reach our stream. Whatever it read, wrote
    or ran happens inside one `agent.tool_result`, and a client watching a long
    delegation sees nothing at all until it lands. If sub-agent events are ever
    surfaced, this assertion is the one that should start failing.
    """
    events = await run_turn(
        api,
        planning_session,
        "Use the task tool to delegate this to a sub-agent: read "
        "/home/user/uploads/readme.md and report its heading back to me.",
    )

    call = call_named(events, "task")
    assert call["evaluated_permission"] == "ask", call
    assert pending(events) == [call["tool_use_id"]]

    after = await answer(api, planning_session, [allow(call["tool_use_id"])])

    assert call["tool_use_id"] in results(after), results(after)
    assert "README-CHARLIE" in tool_output(after, call["tool_use_id"]) + said(after)

    # The sub-agent read a file, and we never saw it do so: everything it did
    # arrives compressed into the one `agent.tool_result` for the `task` call,
    # with nothing in between.
    #
    # The assertion is on that gap and not on "no read_file anywhere", which is
    # what an earlier version said and what made this test wrong. The parent
    # agent may perfectly well read the file *afterwards* — on one run the
    # sub-agent mistyped the heading and the parent went and checked it — and
    # that is its own work, in the open, not a leak from inside the delegation.
    ordered = [e for e in after if e["type"] in (AGENT_TOOL_USE, AGENT_CUSTOM_TOOL_USE, AGENT_TOOL_RESULT)]
    landed = next(
        i for i, e in enumerate(ordered)
        if e["type"] == AGENT_TOOL_RESULT and e["tool_use_id"] == call["tool_use_id"]
    )
    assert landed == 0, (
        "something reached the stream from inside the sub-agent before its "
        f"result did: {[e['type'] for e in ordered[: landed + 1]]}"
    )


async def test_48_a_refused_delegation_makes_the_agent_do_it_itself(api, planning_session):
    events = await run_turn(
        api,
        planning_session,
        "Use the task tool to delegate this to a sub-agent: read "
        "/home/user/uploads/notes.txt and report its first line.",
    )
    call = call_named(events, "task")

    after = await answer(
        api,
        planning_session,
        [deny(call["tool_use_id"], "REFUSED-DELEGATION: do it yourself, no sub-agents")],
    )

    assert results(after)[call["tool_use_id"]]["is_error"] is True
    # Denied the shortcut, it does the work in the open, where we can see it.
    assert "NOTE-ALPHA" in said(after) + " ".join(
        tool_output(after, c["tool_use_id"])
        for c in calls(after)
        if c["tool_use_id"] in results(after)
    ), said(after)
