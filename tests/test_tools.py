"""Tool config decides what needs approval — never what exists.

Whatever DeepAgents installs is what the model gets. The only question this
answers is which of those tools must stop and ask first.
"""

from __future__ import annotations

from deepagents.backends.protocol import BackendProtocol, ReadResult
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.tools import ToolRuntime

from app.runtime.tools import TOOLSET_TOOL_NAMES, resolve_tool_interrupts

AGENT_TOOLSET = [{"type": "agent_toolset_20260401"}]

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


def test_images_use_deepagents_native_read_file():
    tools = TOOLSET_TOOL_NAMES["agent_toolset_20260401"]

    assert "read_file" in tools
    assert "read_image" not in tools


async def test_deepagents_read_file_returns_a_native_image_block():
    class ImageBackend(BackendProtocol):
        async def aread(
            self, file_path: str, offset: int = 0, limit: int = 2000
        ) -> ReadResult:
            return ReadResult(
                file_data={"content": "aGVsbG8=", "encoding": "base64"}
            )

    middleware = FilesystemMiddleware(backend=ImageBackend())
    read_file = next(tool for tool in middleware.tools if tool.name == "read_file")
    runtime = ToolRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="call_image",
        store=None,
    )

    result = await read_file.coroutine(
        file_path="/image.png",
        runtime=runtime,
    )

    assert result.content_blocks == [
        {
            "type": "image",
            "base64": "aGVsbG8=",
            "mime_type": "image/png",
        }
    ]
