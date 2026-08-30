"""The guard on a stream that has stopped saying anything.

An endpoint serving a broken build answers 200 with `finish_reason: "stop"`,
so this is the only place the difference between an answer and a wall of one
character can be noticed.
"""

from __future__ import annotations

import pytest

from app.models import events as event_types
from app.runtime.engine import (
    REPETITION_LIMIT,
    DegenerateOutputError,
    _DeltaBuffer,
)

SPEECH = event_types.AGENT_MESSAGE_DELTA
THINKING = event_types.AGENT_THINKING_DELTA


def _buffer() -> _DeltaBuffer:
    async def publish(event_type: str, text: str) -> None:  # pragma: no cover
        raise AssertionError("nothing should be published in these tests")

    return _DeltaBuffer(publish)


def test_ordinary_prose_passes():
    buffer = _buffer()
    for piece in ("Happy to build you ", "a new site. ", "Tell me about ", "the business."):
        buffer.add(SPEECH, piece)


def test_a_rule_or_a_table_border_is_not_a_collapse():
    """The longest runs real output contains, well under the limit."""
    buffer = _buffer()
    buffer.add(SPEECH, "\n" + "-" * 80 + "\n")
    buffer.add(SPEECH, "|" + "=" * 120 + "|\n")
    buffer.add(SPEECH, " " * 64 + "indented deeply\n")


def test_a_wall_of_one_character_stops_the_turn():
    buffer = _buffer()
    with pytest.raises(DegenerateOutputError) as excinfo:
        buffer.add(SPEECH, "!" * (REPETITION_LIMIT + 1))
    assert "!" in str(excinfo.value)


def test_a_run_split_across_chunks_still_counts():
    """The collapse arrives as hundreds of small frames, not one big one.

    Counting within a chunk would miss it entirely: each frame carries a few
    characters and is unremarkable on its own.
    """
    buffer = _buffer()
    with pytest.raises(DegenerateOutputError):
        for _ in range(REPETITION_LIMIT + 1):
            buffer.add(SPEECH, "!")


def test_the_run_resets_when_the_model_says_something_else():
    """Two long runs either side of real text are two runs, not one."""
    buffer = _buffer()
    buffer.add(SPEECH, "!" * (REPETITION_LIMIT - 1))
    buffer.add(SPEECH, "still writing")
    buffer.add(SPEECH, "!" * (REPETITION_LIMIT - 1))


def test_reasoning_that_collapses_before_a_word_is_spoken_is_caught():
    """One of the two observed turns died after `The` in its reasoning.

    Nothing reached the visible message at all, so a guard watching only
    speech would have let that turn finish and commit.
    """
    buffer = _buffer()
    with pytest.raises(DegenerateOutputError):
        buffer.add(THINKING, "The")
        buffer.add(THINKING, "!" * (REPETITION_LIMIT + 1))


def test_a_run_spanning_speech_and_reasoning_is_still_one_run():
    """The two interleave in one stream; a collapse does not respect the kind."""
    buffer = _buffer()
    with pytest.raises(DegenerateOutputError):
        buffer.add(THINKING, "!" * (REPETITION_LIMIT // 2))
        buffer.add(SPEECH, "!" * (REPETITION_LIMIT // 2 + 1))


def test_the_limit_is_the_boundary_not_an_approximation():
    buffer = _buffer()
    buffer.add(SPEECH, "!" * (REPETITION_LIMIT - 1))

    tripped = _buffer()
    with pytest.raises(DegenerateOutputError):
        tripped.add(SPEECH, "!" * REPETITION_LIMIT)
