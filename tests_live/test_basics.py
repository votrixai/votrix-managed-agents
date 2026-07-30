"""Scenarios 1–6: a turn that needs no permission from anybody.

These are the floor. If one of them fails, nothing in the other modules means
anything — a batch of confirmations is only interesting once a plain turn works.
"""

from __future__ import annotations

import pytest

from tests_live import stubs
from tests_live.helpers import (
    AGENT_MESSAGE,
    AGENT_THINKING,
    AGENT_TOOL_RESULT,
    AGENT_TOOL_USE,
    calls,
    download,
    kinds,
    of_type,
    output_files,
    results,
    run_to_end,
    run_turn,
    said,
    stop_reason,
    tool_output,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

# The four uploads, and a token that appears on the first line of each. Asking
# for first lines and asserting on tokens keeps the check on the tool result
# rather than on how the model chose to phrase its summary.
FIRST_LINE_TOKENS = {
    "revenue.csv": "month,region,revenue",
    "notes.txt": "NOTE-ALPHA",
    "config.json": "CONFIG-BRAVO",
    "readme.md": "README-CHARLIE",
}

# 120 + 80 + 150 + 90, over four data rows. Written out so a change to the CSV
# has to be made in two places rather than silently agreeing with itself.
EXPECTED_REPORT = "VOTRIX-REVENUE-REPORT total=440 rows=4"


async def test_1_a_skill_turn_produces_a_collected_file(api, session):
    """The whole pipeline: skill unpacked, work done, output collected.

    The assertion is the report's format. Nothing in the prompt says what a
    Votrix revenue report looks like — only the uploaded SKILL.md does — so a
    file in that shape is proof the package reached the container and was read.

    Whatever it stops to ask for is approved. Summing a column is the kind of
    thing a model reaches for the shell to do, and `execute` is `always_ask`
    here — but which tool it picked is not what this test is about.
    """
    events = await run_to_end(
        api,
        session,
        "Use the revenue-report skill on uploads/revenue.csv. "
        "Write the report to outputs/report.txt.",
    )
    assert stop_reason(events)["type"] == "end_turn", kinds(events)

    produced = await output_files(api, session)
    assert "report.txt" in produced, f"collected: {sorted(produced)}"

    content = (await download(api, produced["report.txt"])).decode().strip()
    assert content == EXPECTED_REPORT, content


async def test_2_four_reads_at_once_need_no_permission(api, session):
    """One reply, four calls, four results, and no pause anywhere.

    `read_file` is `always_allow`, so this is the shape of a turn that never
    enters the interrupt path at all — the control for everything in
    `test_confirm_batch.py`.
    """
    events = await run_turn(
        api,
        session,
        "Read these four files at the same time, in one go: uploads/revenue.csv, "
        "uploads/notes.txt, uploads/config.json, uploads/readme.md. "
        "Then tell me the first line of each.",
    )

    reads = [call for call in calls(events) if call["name"] == "read_file"]
    assert len(reads) == 4, [call["name"] for call in calls(events)]
    assert all(call["type"] == AGENT_TOOL_USE for call in reads)
    assert {call["evaluated_permission"] for call in reads} == {"allow"}

    # In *one* reply, which is the whole claim. Four separate replies would
    # produce the same four calls and the same four results, just interleaved —
    # and would be testing nothing about parallel tool calls at all.
    ordered = [e for e in events if e["type"] in (AGENT_TOOL_USE, AGENT_TOOL_RESULT)]
    first_result = next(i for i, e in enumerate(ordered) if e["type"] == AGENT_TOOL_RESULT)
    assert first_result == 4, (
        "the four reads were not announced together: "
        f"{[e['type'] for e in ordered]}"
    )

    assert stop_reason(events)["type"] == "end_turn", stop_reason(events)

    # Every call got its own result, and the results really are the files.
    seen = " ".join(tool_output(events, call["tool_use_id"]) for call in reads)
    for token in FIRST_LINE_TOKENS.values():
        assert token in seen, f"{token!r} missing from the four read results"


async def test_3_a_web_search_result_reaches_the_model(api, session):
    """The one stubbed tool. The pinned tokens exist nowhere else, so finding
    them in the result means the search path is wired end to end."""
    events = await run_turn(
        api,
        session,
        f"Use the web_search tool to look up {stubs.PINNED_TOKEN}, "
        "then tell me its launch date.",
    )

    searches = [call for call in calls(events) if call["name"] == "web_search"]
    assert len(searches) == 1, [call["name"] for call in calls(events)]

    output = tool_output(events, searches[0]["tool_use_id"])
    assert stubs.PINNED_TOKEN in output
    assert stubs.PINNED_DATE in output
    assert stubs.PINNED_DATE in said(events)


async def test_4_the_second_turn_remembers_the_first(api, session):
    """Proves the checkpoint carries the conversation.

    Nothing in the second message says what was calculated, and the event log
    is not fed back to the model — the only place the number survives is the
    graph state LangGraph wrote at the end of turn one.
    """
    first = await run_turn(
        api,
        session,
        "Read uploads/revenue.csv and add up the revenue column. "
        "Reply with just the total.",
    )
    assert "440" in said(first), said(first)

    second = await run_turn(
        api,
        session,
        "What number did you just calculate? Reply with the number and nothing else.",
    )
    assert "440" in said(second), said(second)
    # It answered from memory rather than by reading the file again.
    assert not calls(second), [call["name"] for call in calls(second)]


async def test_5_thinking_is_emitted_only_when_there_is_some(api, session):
    """A conditional assertion on purpose.

    Whether a provider returns reasoning is the provider's business — we do not
    turn extended thinking on. What is ours is the rule: emit when there is
    text, never emit an empty event. That is what this checks.
    """
    events = await run_turn(api, session, "Read uploads/notes.txt and summarise it in one line.")

    thoughts = of_type(events, AGENT_THINKING)
    for event in thoughts:
        assert event["content"], event
        assert all(block["text"].strip() for block in event["content"]), event
    if not thoughts:
        pytest.skip("this model returned no reasoning blocks; the rule is untestable here")


async def test_6_every_agent_message_carries_text(api, session):
    """The same rule for speech: something, or no event."""
    events = await run_turn(api, session, "Read uploads/readme.md and quote its heading.")

    spoken = of_type(events, AGENT_MESSAGE)
    assert spoken, kinds(events)
    for event in spoken:
        assert event["content"], event
        assert all(block["text"].strip() for block in event["content"]), event
    assert "readme-charlie" in said(events), said(events)


async def test_6b_a_turn_has_exactly_one_running_and_one_idle(api, session):
    """The bookends a client watches for. Two idles, or none, and a client
    either stops early or waits forever."""
    events = await run_turn(api, session, "Say the word ACKNOWLEDGED and nothing else.")

    assert kinds(events).count("session.status_running") == 1, kinds(events)
    assert kinds(events).count("session.status_idle") == 1, kinds(events)
    assert kinds(events)[0] == "user.message"
    assert kinds(events)[-1] == "session.status_idle"
    assert not results(events), "a turn with no tools produced tool results"
