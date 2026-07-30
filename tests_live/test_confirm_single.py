"""Scenarios 7–12: one pending call at a time.

Three tools, three ways a call completes:

    direct + allow   runs in the sandbox, nobody is asked
    direct + ask     approve or reject
    custom           respond — always, there is no other option

The last two rows are the ones people get wrong: a custom tool is *never*
"ask". It has no implementation on our side, so the client's answer is the only
way it ever finishes, and asking permission first would be asking twice.
Sending the wrong answer type for a call is what 11 and 12 are about.
"""

from __future__ import annotations

import pytest

from tests_live.helpers import (
    AGENT_CUSTOM_TOOL_USE,
    AGENT_TOOL_USE,
    SESSION_ERROR,
    allow,
    answer,
    call_named,
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

CRM_RECORD = "Northwind Traders, tier gold, owner Wen Ibarra"


async def test_7_an_approved_command_really_runs(api, session):
    events = await run_turn(
        api, session, "Using the execute tool, run the shell command: echo LIVE-SEVEN"
    )

    call = call_named(events, "execute")
    assert call["type"] == AGENT_TOOL_USE
    assert call["evaluated_permission"] == "ask"
    assert pending(events) == [call["tool_use_id"]]
    # Nothing ran: the graph pauses before the tools node, not inside it.
    assert not results(events), results(events)

    after = await answer(api, session, [allow(call["tool_use_id"])])

    assert "LIVE-SEVEN" in tool_output(after, call["tool_use_id"])
    assert stop_reason(after)["type"] == "end_turn"


async def test_8_a_refused_command_tells_the_model_why(api, session):
    events = await run_turn(
        api, session, "Using the execute tool, run the shell command: echo LIVE-EIGHT"
    )
    call = call_named(events, "execute")

    reason = "REFUSED-BY-POLICY: shell commands are not allowed in this session"
    after = await answer(api, session, [deny(call["tool_use_id"], reason)])

    result = results(after)[call["tool_use_id"]]
    assert result["is_error"] is True, result
    assert "REFUSED-BY-POLICY" in tool_output(after, call["tool_use_id"])
    # The command did not run behind the refusal.
    assert "LIVE-EIGHT" not in tool_output(after, call["tool_use_id"])
    assert stop_reason(after)["type"] == "end_turn"


async def test_9_a_custom_tool_is_answered_by_the_client(api, session):
    events = await run_turn(
        api, session, "Use the get_crm_record tool to look up customer C001."
    )

    call = call_named(events, "get_crm_record")
    assert call["type"] == AGENT_CUSTOM_TOOL_USE
    # No permission was evaluated, because there is no policy to evaluate.
    assert "evaluated_permission" not in call, call
    assert call["input"] == {"customer_id": "C001"}
    assert pending(events) == [call["tool_use_id"]]

    after = await answer(api, session, [respond(call["tool_use_id"], CRM_RECORD)])

    assert CRM_RECORD in tool_output(after, call["tool_use_id"])
    assert "northwind" in said(after), said(after)


async def test_10_a_failed_custom_tool_reaches_the_model_as_a_failure(api, session):
    """`is_error` has to survive the trip, not just the text.

    The two decisions land differently on purpose: `respond` synthesises a
    successful `ToolMessage`, `reject` an errored one. Send a failure as a
    `respond` and the model is told the tool worked and returned the sentence
    "the CRM is unavailable" — which it will then reason about as data.
    """
    events = await run_turn(
        api, session, "Use the get_crm_record tool to look up customer C002."
    )
    call = call_named(events, "get_crm_record")

    failure = "CRM-DOWN: the CRM is unavailable, no record could be read"
    after = await answer(
        api, session, [respond(call["tool_use_id"], failure, is_error=True)]
    )

    # What is ours, asserted exactly: the flag survives the trip and the text
    # is unchanged.
    result = results(after)[call["tool_use_id"]]
    assert result["is_error"] is True, result
    assert "CRM-DOWN" in tool_output(after, call["tool_use_id"])

    # What the model then does with a failed tool is the model's business, and
    # it is not stable: the same prompt on the same model has both retried the
    # call and reported the failure and stopped. Asserting either one is
    # asserting a coin flip — an earlier draft of this test did exactly that,
    # twice, and was wrong both times in opposite directions.
    #
    # What can be asserted is that it did *not* mistake the error for data. A
    # model told the call succeeded would write "the CRM is unavailable" into
    # its answer as though that were C002's record; a model told it failed
    # either says so or tries again.
    reason = stop_reason(after)
    if reason["type"] == "requires_action":
        retry = call_named(after, "get_crm_record")
        assert retry["tool_use_id"] != call["tool_use_id"]
        finished = await answer(api, session, [respond(retry["tool_use_id"], CRM_RECORD)])
        assert results(finished)[retry["tool_use_id"]]["is_error"] is False
        assert "northwind" in said(finished), said(finished)
    else:
        assert reason["type"] == "end_turn", reason
        spoken = said(after)
        assert "crm-down" in spoken or "fail" in spoken or "unavailable" in spoken, spoken


async def test_10b_a_successful_custom_result_is_not_marked_as_an_error(api, session):
    """The control for the test above: without the flag, nothing is an error."""
    events = await run_turn(
        api, session, "Use the get_crm_record tool to look up customer C001."
    )
    call = call_named(events, "get_crm_record")

    after = await answer(api, session, [respond(call["tool_use_id"], CRM_RECORD)])

    assert results(after)[call["tool_use_id"]]["is_error"] is False


async def test_11_confirming_a_custom_call_fails_cleanly(api, session):
    """A custom call only accepts `respond`.

    An `approve` for one is a client that has confused the two kinds. It has to
    fail where it is caught — loudly, in a `session.error` — rather than run
    a tool we have no implementation for.
    """
    events = await run_turn(
        api, session, "Use the get_crm_record tool to look up customer C003."
    )
    call = call_named(events, "get_crm_record")

    after = await answer(api, session, [allow(call["tool_use_id"])])

    assert of_type(after, SESSION_ERROR), kinds(after)
    # The turn still *ends*. A client on the stream is waiting for idle and has
    # nothing else to wait for.
    assert stop_reason(after)["type"] == "error", stop_reason(after)
    assert await status(api, session) == "idle"

    # And the session is still usable afterwards.
    recovered = await run_turn(api, session, "Say the word RECOVERED and nothing else.")
    assert "recovered" in said(recovered), said(recovered)


async def test_12_responding_to_a_direct_call_fails_cleanly(api, session):
    """The mirror image: a `respond` for a call that wanted approve or reject."""
    events = await run_turn(
        api, session, "Using the execute tool, run the shell command: echo LIVE-TWELVE"
    )
    call = call_named(events, "execute")

    after = await answer(api, session, [respond(call["tool_use_id"], "pretend output")])

    assert of_type(after, SESSION_ERROR), kinds(after)
    assert stop_reason(after)["type"] == "error", stop_reason(after)
    assert await status(api, session) == "idle"

    recovered = await run_turn(api, session, "Say the word RECOVERED and nothing else.")
    assert "recovered" in said(recovered), said(recovered)


async def test_12b_answering_when_nothing_is_pending_is_refused(api, session):
    """There is no interrupt to resume, so there is nothing this could mean."""
    await run_turn(api, session, "Say the word READY and nothing else.")

    # Accepted at the door — the engine is what discovers there is no pause.
    after = await answer(api, session, [allow("toolu_nothing_is_waiting_on_this")])

    assert of_type(after, SESSION_ERROR), kinds(after)
    assert stop_reason(after)["type"] == "error", stop_reason(after)
    assert await status(api, session) == "idle"
    assert not calls(await run_turn(api, session, "Say OK and nothing else."))
