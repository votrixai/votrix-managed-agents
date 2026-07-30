"""Scenarios 13–26: one model reply, several calls, one pause.

Three facts are under test here and nothing else is:

1. **A batch stops as a batch.** The graph pauses in `after_model`, a node
   *before* `tools`. So a call that needed no permission is waiting too, and
   its result does not arrive until the ones that did are answered. Scenarios
   17 and 18 exist to pin that down, because it surprises everyone.

2. **Decisions are matched by position.** `HumanInTheLoopMiddleware` pairs the
   list it is handed with its own request, index by index. Build the pending
   list wrong by one and an approval lands on a different call, with nothing
   anywhere reporting it. That is the only silent failure in the system, and
   scenarios 22–24 are aimed squarely at it.

3. **The client may answer in any order.** Every answer names its call, so
   `_build_resume` puts them back in the graph's order. A test that answers in
   order proves nothing; the ones that scramble do.

Every assertion here is per-call: *this* result belongs to *that* input. A test
that only counted results would pass with every decision shifted by one.
"""

from __future__ import annotations

import json

import pytest

from tests_live.helpers import (
    AGENT_CUSTOM_TOOL_USE,
    AGENT_TOOL_USE,
    SESSION_ERROR,
    allow,
    answer,
    calls,
    deny,
    kinds,
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

RECORDS = {
    "C001": "C001 = Northwind Traders, tier gold",
    "C002": "C002 = Southridge Video, tier silver",
    "C003": "C003 = Contoso Pharmaceuticals, tier bronze",
}


def call_containing(events, token: str) -> dict:
    """The one announced call whose arguments mention `token`.

    Matching on the serialised input rather than on a named parameter: the
    argument Deep Agents' `execute` takes its command in is Deep Agents'
    business, and a test that hard-codes it breaks on an upgrade that changed
    nothing we care about.
    """
    found = [call for call in calls(events) if token in json.dumps(call["input"])]
    assert len(found) == 1, (
        f"expected one call mentioning {token!r}, found {len(found)}: "
        f"{[(c['name'], c['input']) for c in calls(events)]}"
    )
    return found[0]


def needs_an_answer(call: dict) -> bool:
    """Custom calls always; direct calls only when the policy said ask."""
    return call["type"] == AGENT_CUSTOM_TOOL_USE or call.get("evaluated_permission") == "ask"


# --- one kind at a time ------------------------------------------------------


async def test_13_three_commands_all_approved(api, session):
    events = await run_turn(
        api,
        session,
        "Using the execute tool, run these three shell commands at the same "
        "time, in one reply: `echo BATCH-A`, `echo BATCH-B`, `echo BATCH-C`",
    )
    assert len(pending(events)) == 3, [c["input"] for c in calls(events)]

    ids = {token: call_containing(events, token)["tool_use_id"] for token in ("BATCH-A", "BATCH-B", "BATCH-C")}
    after = await answer(api, session, [allow(call_id) for call_id in ids.values()])

    for token, call_id in ids.items():
        assert token in tool_output(after, call_id), (token, tool_output(after, call_id))
    assert stop_reason(after)["type"] == "end_turn"


async def test_14_half_a_batch_may_be_refused(api, session):
    """Approvals and refusals in one reply. If decisions were being applied by
    arrival order rather than by call, the refusal would land on `DELTA`."""
    events = await run_turn(
        api,
        session,
        "Using the execute tool, run these three shell commands at the same "
        "time, in one reply: `echo BATCH-DELTA`, `echo BATCH-ECHO`, `echo BATCH-FOX`",
    )
    assert len(pending(events)) == 3

    delta = call_containing(events, "BATCH-DELTA")["tool_use_id"]
    echo = call_containing(events, "BATCH-ECHO")["tool_use_id"]
    fox = call_containing(events, "BATCH-FOX")["tool_use_id"]

    after = await answer(
        api,
        session,
        [allow(delta), deny(echo, "REFUSED-ECHO: not this one"), allow(fox)],
    )

    assert "BATCH-DELTA" in tool_output(after, delta)
    assert "BATCH-FOX" in tool_output(after, fox)
    assert results(after)[echo]["is_error"] is True, results(after)[echo]
    assert "REFUSED-ECHO" in tool_output(after, echo)
    assert "BATCH-ECHO" not in tool_output(after, echo)


async def test_15_three_custom_calls_answered_in_order(api, session):
    """The plain case, so the scrambled one below has something to be a
    variation of."""
    events = await run_turn(
        api,
        session,
        "Use the get_crm_record tool to look up customers C001, C002 and C003 "
        "at the same time, in one reply.",
    )
    ids = {cid: call_containing(events, cid)["tool_use_id"] for cid in RECORDS}
    assert sorted(pending(events)) == sorted(ids.values())

    after = await answer(
        api,
        session,
        [respond(ids[cid], RECORDS[cid]) for cid in ("C001", "C002", "C003")],
    )

    for cid, call_id in ids.items():
        assert RECORDS[cid] in tool_output(after, call_id)


async def test_16_three_custom_calls_answered_backwards(api, session):
    """The same batch, answered C003 → C002 → C001.

    Each answer names its call, so the reordering happens in `_build_resume`.
    Get it wrong and Contoso's record comes back for Northwind — which is
    exactly what the per-call assertion catches.
    """
    events = await run_turn(
        api,
        session,
        "Use the get_crm_record tool to look up customers C001, C002 and C003 "
        "at the same time, in one reply.",
    )
    ids = {cid: call_containing(events, cid)["tool_use_id"] for cid in RECORDS}

    after = await answer(
        api,
        session,
        [respond(ids[cid], RECORDS[cid]) for cid in ("C003", "C002", "C001")],
    )

    for cid, call_id in ids.items():
        assert RECORDS[cid] in tool_output(after, call_id), cid


# --- mixed kinds -------------------------------------------------------------


async def test_17_an_allowed_call_waits_for_one_that_asks(api, session):
    """The counter-intuitive one.

    `read_file` needs nobody's permission, and it still does not run: the graph
    stopped in `after_model`, which is upstream of every tool. A client that
    assumed otherwise would sit waiting for a result that cannot arrive until
    it answers something else.
    """
    events = await run_turn(
        api,
        session,
        "Do both of these at the same time, in one reply: read uploads/notes.txt, "
        "and use the execute tool to run `echo SEVENTEEN`.",
    )

    read = call_containing(events, "notes.txt")
    shell = call_containing(events, "SEVENTEEN")
    assert pending(events) == [shell["tool_use_id"]], pending(events)
    # Announced, permitted, and still without a result.
    assert read["evaluated_permission"] == "allow"
    assert not results(events), results(events)

    after = await answer(api, session, [allow(shell["tool_use_id"])])

    assert "NOTE-ALPHA" in tool_output(after, read["tool_use_id"])
    assert "SEVENTEEN" in tool_output(after, shell["tool_use_id"])


async def test_18_an_allowed_call_waits_for_a_custom_one(api, session):
    """The same fact, with the pause coming from a custom tool instead."""
    events = await run_turn(
        api,
        session,
        "Do both of these at the same time, in one reply: read uploads/readme.md, "
        "and use the get_crm_record tool to look up customer C001.",
    )

    read = call_containing(events, "readme.md")
    crm = call_containing(events, "C001")
    assert pending(events) == [crm["tool_use_id"]], pending(events)
    assert not results(events), results(events)

    after = await answer(api, session, [respond(crm["tool_use_id"], RECORDS["C001"])])

    assert "README-CHARLIE" in tool_output(after, read["tool_use_id"])
    assert RECORDS["C001"] in tool_output(after, crm["tool_use_id"])


async def test_19_a_confirmation_and_a_result_in_one_request(api, session):
    """Two different event types answering one pause, in a single POST.

    This is the shape the old plan never covered, and it is the ordinary case
    in production the moment an agent has both kinds of tool.
    """
    events = await run_turn(
        api,
        session,
        "Do both of these at the same time, in one reply: use the execute tool "
        "to run `echo NINETEEN`, and use the get_crm_record tool to look up customer C002.",
    )

    shell = call_containing(events, "NINETEEN")
    crm = call_containing(events, "C002")
    # The two kinds really are mixed. `call_containing` matches on arguments,
    # not on type, so without this a run where the model reached for the wrong
    # tool would still line up and pass.
    assert shell["type"] == AGENT_TOOL_USE, shell
    assert crm["type"] == AGENT_CUSTOM_TOOL_USE, crm
    assert sorted(pending(events)) == sorted([shell["tool_use_id"], crm["tool_use_id"]])

    after = await answer(
        api,
        session,
        [allow(shell["tool_use_id"]), respond(crm["tool_use_id"], RECORDS["C002"])],
    )

    assert "NINETEEN" in tool_output(after, shell["tool_use_id"])
    assert RECORDS["C002"] in tool_output(after, crm["tool_use_id"])


async def test_20_mixed_kinds_answered_backwards_with_a_refusal(api, session):
    """Order reversed and polarity mixed at once.

    If the two answers were swapped by the reordering, the refusal would land
    on the CRM lookup — and the CRM result would come back as an error instead
    of Southridge's record.
    """
    events = await run_turn(
        api,
        session,
        "Do both of these at the same time, in one reply: use the get_crm_record "
        "tool to look up customer C003, and use the execute tool to run `echo TWENTY`.",
    )

    crm = call_containing(events, "C003")
    shell = call_containing(events, "TWENTY")
    # The two kinds really are mixed. `call_containing` matches on arguments,
    # not on type, so without this a run where the model reached for the wrong
    # tool would still line up and pass.
    assert shell["type"] == AGENT_TOOL_USE, shell
    assert crm["type"] == AGENT_CUSTOM_TOOL_USE, crm
    waiting = pending(events)
    assert sorted(waiting) == sorted([crm["tool_use_id"], shell["tool_use_id"]])

    # Sent in the reverse of whatever order the graph asked for.
    reversed_answers = {
        shell["tool_use_id"]: deny(shell["tool_use_id"], "REFUSED-TWENTY: no shell"),
        crm["tool_use_id"]: respond(crm["tool_use_id"], RECORDS["C003"]),
    }
    after = await answer(api, session, [reversed_answers[call_id] for call_id in reversed(waiting)])

    assert results(after)[shell["tool_use_id"]]["is_error"] is True
    assert "REFUSED-TWENTY" in tool_output(after, shell["tool_use_id"])
    assert RECORDS["C003"] in tool_output(after, crm["tool_use_id"])
    assert results(after)[crm["tool_use_id"]]["is_error"] is False


async def test_21_an_approval_beside_a_failed_custom_tool(api, session):
    """The other diagonal: the direct call succeeds, the custom one reports a
    failure, and neither contaminates the other."""
    events = await run_turn(
        api,
        session,
        "Do both of these at the same time, in one reply: use the execute tool "
        "to run `echo TWENTYONE`, and use the get_crm_record tool to look up customer C001.",
    )

    shell = call_containing(events, "TWENTYONE")
    crm = call_containing(events, "C001")
    # The two kinds really are mixed. `call_containing` matches on arguments,
    # not on type, so without this a run where the model reached for the wrong
    # tool would still line up and pass.
    assert shell["type"] == AGENT_TOOL_USE, shell
    assert crm["type"] == AGENT_CUSTOM_TOOL_USE, crm

    after = await answer(
        api,
        session,
        [
            allow(shell["tool_use_id"]),
            respond(crm["tool_use_id"], "CRM-DOWN: lookup failed", is_error=True),
        ],
    )

    assert "TWENTYONE" in tool_output(after, shell["tool_use_id"])
    assert results(after)[shell["tool_use_id"]]["is_error"] is False
    assert "CRM-DOWN" in tool_output(after, crm["tool_use_id"])
    assert results(after)[crm["tool_use_id"]]["is_error"] is True


# --- the silent failure ------------------------------------------------------
#
# 22, 23 and 24 are one statement made three times: whichever position the
# permission-free call lands in, it must be filtered out of the pending list
# without shifting anything after it. The model chooses the order, so the test
# reads the order back and asserts against what actually happened rather than
# against what the prompt asked for.
#
# The answers are deliberately a refusal and a result. A decision list shifted
# by one puts the refusal on the wrong call, and these assertions notice.


async def _mixed_three(
    api, session, prompt: str, shell_token: str, customer: str, free_at: int
):
    events = await run_turn(api, session, prompt)

    announced = calls(events)
    assert len(announced) == 3, [(c["name"], c["input"]) for c in announced]

    # The position is asserted, not merely hoped for. Without this the three
    # tests below are the same test three times over: the pending list is
    # derived from whatever the model actually sent, so it would agree with
    # itself no matter where `read_file` landed, and a filter that only works
    # at index 0 would pass all three.
    names = [call["name"] for call in announced]
    assert names[free_at] == "read_file", (
        f"this scenario tests the free call at index {free_at}; "
        f"the model ordered them {names}"
    )
    # And the two kinds really are mixed, which is the other half of the point.
    assert {call["type"] for call in announced} == {
        AGENT_TOOL_USE,
        AGENT_CUSTOM_TOOL_USE,
    }, [(c["name"], c["type"]) for c in announced]

    expected = [call["tool_use_id"] for call in announced if needs_an_answer(call)]
    assert len(expected) == 2, [(c["name"], c.get("evaluated_permission")) for c in announced]
    # The heart of it: same members, same order, minus the one nobody is asked
    # about — at whatever position the model happened to put it.
    assert pending(events) == expected, (pending(events), expected)
    assert not results(events), "the batch ran something before it was answered"

    read = next(call for call in announced if call["name"] == "read_file")
    shell = call_containing(events, shell_token)
    crm = call_containing(events, customer)

    after = await answer(
        api,
        session,
        [
            deny(shell["tool_use_id"], f"REFUSED-{shell_token}: no shell"),
            respond(crm["tool_use_id"], RECORDS[customer]),
        ],
    )

    # If the pending list had kept the read, every decision would be one place
    # out: the read would be refused and the shell would get the CRM record.
    assert results(after)[read["tool_use_id"]]["is_error"] is False, results(after)[read["tool_use_id"]]
    assert results(after)[shell["tool_use_id"]]["is_error"] is True
    assert f"REFUSED-{shell_token}" in tool_output(after, shell["tool_use_id"])
    assert RECORDS[customer] in tool_output(after, crm["tool_use_id"])
    return read, after


async def test_22_the_free_call_is_filtered_from_the_front(api, session):
    read, after = await _mixed_three(
        api,
        session,
        "Do all three of these at the same time, in one reply, in this order: "
        "read uploads/notes.txt; use the execute tool to run `echo TWENTYTWO`; "
        "use the get_crm_record tool to look up customer C001.",
        "TWENTYTWO",
        "C001",
        free_at=0,
    )
    assert "NOTE-ALPHA" in tool_output(after, read["tool_use_id"])


async def test_23_the_free_call_is_filtered_from_the_middle(api, session):
    read, after = await _mixed_three(
        api,
        session,
        "Do all three of these at the same time, in one reply, in this order: "
        "use the execute tool to run `echo TWENTYTHREE`; read uploads/readme.md; "
        "use the get_crm_record tool to look up customer C002.",
        "TWENTYTHREE",
        "C002",
        free_at=1,
    )
    assert "README-CHARLIE" in tool_output(after, read["tool_use_id"])


async def test_24_the_free_call_is_filtered_from_the_end(api, session):
    read, after = await _mixed_three(
        api,
        session,
        "Do all three of these at the same time, in one reply, in this order: "
        "use the execute tool to run `echo TWENTYFOUR`; use the get_crm_record "
        "tool to look up customer C003; read uploads/config.json.",
        "TWENTYFOUR",
        "C003",
        free_at=2,
    )
    assert "CONFIG-BRAVO" in tool_output(after, read["tool_use_id"])


# --- answers that do not fit -------------------------------------------------


async def test_25_an_incomplete_answer_fails_the_turn_not_the_request(api, session):
    """Two of three.

    Nothing here counts the answers. The short list goes to the graph as it is,
    and LangGraph's own check is what refuses it — one rule, in one place, and
    it is the place that would have had to agree with us anyway.
    """
    events = await run_turn(
        api,
        session,
        "Use the get_crm_record tool to look up customers C001, C002 and C003 "
        "at the same time, in one reply.",
    )
    ids = {cid: call_containing(events, cid)["tool_use_id"] for cid in RECORDS}
    assert len(pending(events)) == 3

    after = await answer(
        api,
        session,
        [respond(ids["C001"], RECORDS["C001"]), respond(ids["C002"], RECORDS["C002"])],
    )

    assert of_type(after, SESSION_ERROR), kinds(after)
    assert stop_reason(after)["type"] == "error", stop_reason(after)
    assert await status(api, session) == "idle"

    recovered = await run_turn(api, session, "Say the word RECOVERED and nothing else.")
    assert "recovered" in said(recovered), said(recovered)


async def test_26_an_answer_for_an_unknown_call_is_ignored(api, session):
    """One answer too many.

    Decisions are built by walking the pending list, so an id that is not on it
    never becomes one. The extra is dropped, and the three real answers land
    where they belong.
    """
    events = await run_turn(
        api,
        session,
        "Use the get_crm_record tool to look up customers C001, C002 and C003 "
        "at the same time, in one reply.",
    )
    ids = {cid: call_containing(events, cid)["tool_use_id"] for cid in RECORDS}

    after = await answer(
        api,
        session,
        [
            respond(ids["C001"], RECORDS["C001"]),
            respond("toolu_this_call_never_existed", "should be ignored"),
            respond(ids["C002"], RECORDS["C002"]),
            respond(ids["C003"], RECORDS["C003"]),
        ],
    )

    for cid, call_id in ids.items():
        assert RECORDS[cid] in tool_output(after, call_id), cid
    assert stop_reason(after)["type"] == "end_turn"
