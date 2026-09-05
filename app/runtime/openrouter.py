"""VMA's compatibility boundary for OpenRouter chat requests."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    convert_to_openai_data_block,
)
from langchain_openrouter import ChatOpenRouter


def _tool_content_for_openrouter(content: Any) -> Any:
    """Translate standard image blocks without changing other tool content."""

    if not isinstance(content, list):
        return content
    return [
        convert_to_openai_data_block(block)
        if isinstance(block, dict) and block.get("type") == "image"
        else block
        for block in content
    ]


class VMAChatOpenRouter(ChatOpenRouter):
    """Make DeepAgents' multimodal tool results valid OpenRouter messages.

    ``langchain-openrouter`` formats standard data blocks on user messages but
    currently leaves the same blocks untouched on tool messages. The OpenRouter
    SDK then serializes an image returned by DeepAgents' ``read_file`` as an
    ``UNKNOWN`` block instead of an ``image_url``.

    Work on the wire dictionaries returned by the parent so it remains the
    owner of role, tool-call identity, names, and future message metadata. Only
    the content of LangChain ``ToolMessage`` instances is translated here.
    """

    def _create_message_dicts(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        message_dicts, params = super()._create_message_dicts(messages, stop)
        for message, message_dict in zip(messages, message_dicts, strict=True):
            if isinstance(message, ToolMessage):
                message_dict["content"] = _tool_content_for_openrouter(
                    message_dict.get("content")
                )
        return message_dicts, params


__all__ = ["VMAChatOpenRouter"]
