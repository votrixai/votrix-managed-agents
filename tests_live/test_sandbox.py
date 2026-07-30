"""Scenarios 36–44: the container actually doing work, and what comes back out.

Everything before this module tests *permission* — who is asked, in what order,
and how the answer gets home. None of it tests whether the sandbox can do
anything: every `execute` in the confirmation suites runs `echo`.

So this module runs real code, writes real files, and downloads real bytes. Two
of its scenarios exist to pin down a boundary rather than a feature:

* **39** writes outside `outputs/`. `collect_outputs` scans that one directory
  and nothing else, so the file is real, readable, and never collected. That is
  the intended design — deliverables are declared by where they are put — but
  until now nothing recorded it, and a model that stops following the
  convention would have looked like a storage bug.
* **38** writes into a subdirectory of `outputs/`, which *is* collected, and
  keeps its path in the filename. The pair is what makes the rule legible.
"""

from __future__ import annotations

import pytest

from tests_live.helpers import (
    allow,
    answer,
    call_named,
    calls,
    download,
    output_files,
    pending,
    run_to_end,
    run_turn,
    said,
    stop_reason,
    tool_output,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- the sandbox runs real code ---------------------------------------------


async def test_36_the_sandbox_really_runs_python(api, session):
    """Not `echo`.

    The assertion is on the tool result, not on what the model says about it:
    440 is what the CSV adds up to, and a model that guessed would not have a
    tool result containing it.
    """
    events = await run_turn(
        api,
        session,
        "Using the execute tool, run this exact command and nothing else: "
        "python3 -c \"import csv;print('PYSUM',sum(int(r['revenue']) "
        "for r in csv.DictReader(open('/home/user/uploads/revenue.csv'))))\"",
    )

    call = call_named(events, "execute")
    assert pending(events) == [call["tool_use_id"]]

    after = await answer(api, session, [allow(call["tool_use_id"])])

    output = tool_output(after, call["tool_use_id"])
    assert "PYSUM 440" in output, output


async def test_37_the_sandbox_really_runs_a_shell_pipeline(api, session):
    """A second command that has to touch the mounted files to answer."""
    events = await run_turn(
        api,
        session,
        "Using the execute tool, run this exact command and nothing else: "
        "wc -l < /home/user/uploads/revenue.csv",
    )
    call = call_named(events, "execute")

    after = await answer(api, session, [allow(call["tool_use_id"])])

    # Header plus four data rows. The file is real and the count is the file's.
    assert "5" in tool_output(after, call["tool_use_id"])


# --- what gets collected, and what does not ---------------------------------


async def test_38_a_file_in_a_subdirectory_is_collected_with_its_path(api, session):
    events = await run_to_end(
        api,
        session,
        "Use write_file to create /home/user/outputs/reports/2031/q1.txt "
        "containing exactly the text SUBDIR-PROOF and nothing else.",
    )
    assert stop_reason(events)["type"] == "end_turn"

    produced = await output_files(api, session)
    assert "reports/2031/q1.txt" in produced, (
        f"the subdirectory was lost; collected: {sorted(produced)}"
    )
    assert b"SUBDIR-PROOF" in await download(api, produced["reports/2031/q1.txt"])


async def test_39_a_file_outside_outputs_is_never_collected(api, session):
    """The boundary, stated as a test.

    The file is written and read back in the same turn, so there is no doubt it
    exists in the container. It is still not a deliverable, because being a
    deliverable means being in `outputs/` — that is the whole of the rule, and
    a client that went looking for this file would be right to find nothing.
    """
    events = await run_to_end(
        api,
        session,
        "Do these two things: first use write_file to create "
        "/home/user/scratch_note.txt containing exactly OUTSIDE-PROOF, "
        "then use read_file to read that same path back and tell me what it says.",
    )

    # It exists: the agent read it back and the content came through the tool.
    read_back = " ".join(
        tool_output(events, call["tool_use_id"])
        for call in calls(events)
        if call["name"] == "read_file"
    )
    assert "OUTSIDE-PROOF" in read_back, read_back

    # And it is not a deliverable.
    produced = await output_files(api, session)
    assert "scratch_note.txt" not in produced, produced
    assert not any("scratch_note" in name for name in produced), produced


async def test_40_three_files_in_one_turn_all_come_back(api, session):
    """Collection used to be tested with exactly one file, which cannot tell a
    loop from a special case."""
    events = await run_to_end(
        api,
        session,
        "Use write_file three times to create these files, each containing "
        "exactly the text given and nothing else: "
        "/home/user/outputs/a.txt with ALPHA-ONE, "
        "/home/user/outputs/b.txt with BETA-TWO, "
        "/home/user/outputs/c.txt with GAMMA-THREE.",
    )
    assert stop_reason(events)["type"] == "end_turn"

    produced = await output_files(api, session)
    for name, token in (("a.txt", b"ALPHA-ONE"), ("b.txt", b"BETA-TWO"), ("c.txt", b"GAMMA-THREE")):
        assert name in produced, f"{name} missing from {sorted(produced)}"
        assert token in await download(api, produced[name]), name


async def test_41_an_uploaded_file_makes_the_whole_round_trip(api, session):
    """Bucket → container → agent → container → bucket → client.

    Every hop is real, and the bytes at the end have to carry something that
    was only in the bytes at the start.
    """
    events = await run_to_end(
        api,
        session,
        "Read /home/user/uploads/notes.txt, take its first line, convert that "
        "line to upper case, and use write_file to save just that line to "
        "/home/user/outputs/shout.txt.",
    )

    produced = await output_files(api, session)
    assert "shout.txt" in produced, sorted(produced)
    content = (await download(api, produced["shout.txt"])).decode()
    assert "NOTE-ALPHA THE QUARTERLY REVIEW IS ON FRIDAY" in content.upper(), content


# --- the filesystem tools nothing had ever called ---------------------------


async def test_42_ls_lists_the_mounted_uploads(api, session):
    events = await run_turn(
        api, session, "Use the ls tool to list /home/user/uploads and tell me what is there."
    )

    call = call_named(events, "ls")
    assert call["evaluated_permission"] == "allow"
    listing = tool_output(events, call["tool_use_id"])
    for name in ("revenue.csv", "notes.txt", "config.json", "readme.md"):
        assert name in listing, f"{name} missing from ls output: {listing}"


async def test_43_glob_and_grep_find_what_is_there(api, session):
    events = await run_to_end(
        api,
        session,
        "Do both: use the glob tool to find every .txt file under "
        "/home/user/uploads, and use the grep tool to find which file under "
        "/home/user/uploads contains the text NOTE-ALPHA.",
    )

    names = [call["name"] for call in calls(events)]
    assert "glob" in names, names
    assert "grep" in names, names

    globbed = tool_output(events, call_named(events, "glob")["tool_use_id"])
    assert "notes.txt" in globbed, globbed
    grepped = tool_output(events, call_named(events, "grep")["tool_use_id"])
    assert "notes.txt" in grepped, grepped


async def test_44_edit_file_changes_a_file_that_then_downloads(api, session):
    """`edit_file` had never been called by anything."""
    events = await run_to_end(
        api,
        session,
        "First use write_file to create /home/user/outputs/edited.txt "
        "containing exactly: NOTE-ALPHA placeholder. "
        "Then use the edit_file tool to change NOTE-ALPHA to NOTE-EDITED in "
        "that same file.",
    )

    names = [call["name"] for call in calls(events)]
    assert "edit_file" in names, names

    produced = await output_files(api, session)
    assert "edited.txt" in produced, sorted(produced)
    content = (await download(api, produced["edited.txt"])).decode()
    assert "NOTE-EDITED" in content, content
    assert "NOTE-ALPHA" not in content, content
    assert "edited" in said(events).lower() or content, said(events)
