"""VMA's compatibility boundary for OpenRouter chat requests."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    convert_to_openai_data_block,
)
from langchain_openrouter import ChatOpenRouter


def _image_mime_type(block: dict[str, Any]) -> str | None:
    """Identify an inline image from its decoded signature, not its filename."""

    encoded = block.get("base64")
    if encoded is None and block.get("source_type") == "base64":
        encoded = block.get("data")
    if not isinstance(encoded, str):
        return None

    # Sixteen base64 characters decode to twelve bytes: enough for every
    # signature below, without duplicating a potentially large image in memory.
    try:
        prefix = base64.b64decode(encoded[:16], validate=True)
    except (binascii.Error, ValueError):
        return None

    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_block_for_openrouter(block: dict[str, Any]) -> dict[str, Any]:
    """Convert one image block, correcting inline MIME metadata when known."""

    # A URL is already the source of truth. Do not fetch it merely to inspect
    # its bytes; the provider is responsible for retrieving it.
    if "url" in block:
        return convert_to_openai_data_block(block)

    mime_type = _image_mime_type(block)
    if mime_type is None:
        return convert_to_openai_data_block(block)
    return convert_to_openai_data_block({**block, "mime_type": mime_type})


def _tool_content_for_openrouter(content: Any) -> Any:
    """Translate standard image blocks without changing other tool content."""

    if not isinstance(content, list):
        return content
    return [
        _image_block_for_openrouter(block)
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
        text_only = (self.profile or {}).get("image_inputs") is False
        for message, message_dict in zip(messages, message_dicts, strict=True):
            if text_only and isinstance(message_dict.get("content"), list):
                message_dict["content"] = [
                    {"type": "text", "text": "[Image omitted: text-only model.]"}
                    if isinstance(block, dict)
                    and block.get("type") in {"image", "image_url", "input_image"}
                    else block
                    for block in message_dict["content"]
                ]
            elif isinstance(message, ToolMessage):
                message_dict["content"] = _tool_content_for_openrouter(
                    message_dict.get("content")
                )
        return message_dicts, params


__all__ = ["VMAChatOpenRouter"]
