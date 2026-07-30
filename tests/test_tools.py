"""Tool config decides what needs approval — never what exists.

Whatever DeepAgents installs is what the model gets. The only question this
answers is which of those tools must stop and ask first.

`read_image` is the exception to the first sentence: it is ours, so the second
half of this module is about what it does rather than who may run it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.runtime import tools as tools_module
from app.runtime.tools import (
    READ_IMAGE_MAX_BYTES,
    TOOLSET_TOOL_NAMES,
    read_image_tool,
    resolve_tool_interrupts,
)

AGENT_TOOLSET = [{"type": "agent_toolset_20260401"}]

PNG = b"\x89PNG\r\n\x1a\n" + b"not really a png, but bytes are bytes"


def ask(config: dict) -> list[str]:
    return sorted(resolve_tool_interrupts([{"type": "agent_toolset_20260401", **config}]))


def test_nothing_asks_by_default():
    assert resolve_tool_interrupts(AGENT_TOOLSET) == {}


def test_a_default_policy_covers_every_tool_in_the_set():
    assert ask({"default_config": {"permission_policy": {"type": "always_ask"}}}) == sorted(
        TOOLSET_TOOL_NAMES["agent_toolset_20260401"]
    )


def test_one_tool_can_be_singled_out():
    assert ask({"configs": [{"name": "execute", "permission_policy": {"type": "always_ask"}}]}) == [
        "execute"
    ]


def test_a_tool_can_opt_out_of_the_default():
    asked = ask(
        {
            "default_config": {"permission_policy": {"type": "always_ask"}},
            "configs": [{"name": "read_file", "permission_policy": {"type": "always_allow"}}],
        }
    )
    assert "read_file" not in asked
    assert "execute" in asked


def test_an_interrupt_offers_approve_and_reject():
    interrupts = resolve_tool_interrupts(
        [{"type": "agent_toolset_20260401", "configs": [{"name": "execute", "permission_policy": {"type": "always_ask"}}]}]
    )
    assert interrupts["execute"] == {"allowed_decisions": ["approve", "reject"]}


def test_unknown_toolsets_are_ignored():
    assert resolve_tool_interrupts([{"type": "something_else"}, "not a dict"]) == {}


def test_a_name_that_is_not_in_the_set_is_ignored():
    assert ask({"configs": [{"name": "made_up", "permission_policy": {"type": "always_ask"}}]}) == []


def test_web_tools_are_their_own_toolset():
    """Declaring the agent toolset must not silently grant web access."""
    assert "web_fetch" not in TOOLSET_TOOL_NAMES["agent_toolset_20260401"]
    assert TOOLSET_TOOL_NAMES["web_toolset_20260401"] == ("web_fetch", "web_search")


def test_read_image_can_be_made_to_ask():
    """It is installed with the filesystem tools, so config has to reach it."""
    assert "read_image" in TOOLSET_TOOL_NAMES["agent_toolset_20260401"]
    assert ask({"configs": [{"name": "read_image", "permission_policy": {"type": "always_ask"}}]}) == [
        "read_image"
    ]


# --- read_image --------------------------------------------------------------


class FakeSandbox:
    """Files by absolute path, and a record of what was asked for."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[tuple[str, int]] = []

    async def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        self.asked.append((path, max_bytes))
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if len(data) > max_bytes:
            raise ValueError(f"{path} is {len(data)} bytes, over the {max_bytes} byte limit")
        return data


@pytest.fixture
def looked_at(monkeypatch) -> list[dict[str, Any]]:
    """Replace the vision call, and keep what it was handed."""
    calls: list[dict[str, Any]] = []

    async def _describe(data: bytes, mime_type: str, query: str) -> str:
        calls.append({"data": data, "mime_type": mime_type, "query": query})
        return "A cat, wearing a hat."

    monkeypatch.setattr(tools_module, "describe_image", _describe)
    monkeypatch.setattr(
        tools_module, "get_settings", lambda: type("S", (), {"gemini_api_key": "key"})()
    )
    return calls


async def test_a_relative_path_is_taken_from_the_workdir(looked_at):
    sandbox = FakeSandbox({"/home/user/uploads/cat.png": PNG})
    tool = read_image_tool(sandbox)

    answer = await tool.ainvoke({"path": "uploads/cat.png", "query": "What is in it?"})

    assert answer == "A cat, wearing a hat."
    assert sandbox.asked == [("/home/user/uploads/cat.png", READ_IMAGE_MAX_BYTES)]


async def test_an_absolute_path_is_used_as_given(looked_at):
    sandbox = FakeSandbox({"/tmp/elsewhere/cat.png": PNG})
    tool = read_image_tool(sandbox)

    await tool.ainvoke({"path": "/tmp/elsewhere/cat.png", "query": "What is in it?"})

    assert sandbox.asked == [("/tmp/elsewhere/cat.png", READ_IMAGE_MAX_BYTES)]


async def test_the_bytes_and_the_question_reach_the_vision_model(looked_at):
    tool = read_image_tool(FakeSandbox({"/home/user/a.jpeg": PNG}))

    await tool.ainvoke({"path": "a.jpeg", "query": "Is the logo legible?"})

    assert looked_at == [{"data": PNG, "mime_type": "image/jpeg", "query": "Is the logo legible?"}]


async def test_a_missing_file_is_a_sentence_rather_than_a_raise(looked_at):
    """A tool that raises ends the turn; a sentence lets the model try again."""
    tool = read_image_tool(FakeSandbox({}))

    answer = await tool.ainvoke({"path": "gone.png", "query": "What is in it?"})

    assert "no file at /home/user/gone.png" in answer
    assert looked_at == []


async def test_a_file_that_is_not_an_image_is_refused_before_it_is_read(looked_at):
    """Nothing is transferred to find out it was a video."""
    sandbox = FakeSandbox({"/home/user/clip.mp4": PNG})
    tool = read_image_tool(sandbox)

    answer = await tool.ainvoke({"path": "clip.mp4", "query": "What is in it?"})

    assert ".mp4" in answer or "cannot open" in answer
    assert sandbox.asked == []
    assert looked_at == []


async def test_an_oversized_image_says_so(looked_at):
    tool = read_image_tool(FakeSandbox({"/home/user/huge.png": b"x" * (READ_IMAGE_MAX_BYTES + 1)}))

    answer = await tool.ainvoke({"path": "huge.png", "query": "What is in it?"})

    assert "over the" in answer
    assert looked_at == []


async def test_a_vision_failure_comes_back_as_text(monkeypatch):
    async def _explode(data: bytes, mime_type: str, query: str) -> str:
        raise RuntimeError("gemini said no")

    monkeypatch.setattr(tools_module, "describe_image", _explode)
    monkeypatch.setattr(
        tools_module, "get_settings", lambda: type("S", (), {"gemini_api_key": "key"})()
    )
    tool = read_image_tool(FakeSandbox({"/home/user/a.png": PNG}))

    answer = await tool.ainvoke({"path": "a.png", "query": "What is in it?"})

    assert "gemini said no" in answer


async def test_no_vision_key_is_reported_rather_than_guessed_at(monkeypatch):
    monkeypatch.setattr(
        tools_module, "get_settings", lambda: type("S", (), {"gemini_api_key": ""})()
    )
    sandbox = FakeSandbox({"/home/user/a.png": PNG})

    answer = await read_image_tool(sandbox).ainvoke({"path": "a.png", "query": "What is in it?"})

    assert "no vision model is configured" in answer
    assert sandbox.asked == []
