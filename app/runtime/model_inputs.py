"""Translate Managed Agents file blocks into provider-neutral model content.

Public events keep the Claude Managed Agents wire shape.  Model adapters must
not receive those VMA file IDs directly: they are control-plane identifiers,
not IDs issued by Anthropic, OpenAI, or OpenRouter.  This module resolves them
only against immutable files already mounted in the current Session and emits
LangChain standard content blocks backed by the verified Session copy.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any


SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
SUPPORTED_DOCUMENT_MIME_TYPES = frozenset({"application/pdf", "text/plain"})
MAX_INLINE_IMAGES = 10
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INLINE_PDF_BYTES = 20 * 1024 * 1024
MAX_INLINE_TEXT_BYTES = 1 * 1024 * 1024
MAX_INLINE_TOTAL_BYTES = 32 * 1024 * 1024


class ModelInputValidationError(ValueError):
    """A public content block cannot be bound to this Session safely."""


def validate_user_message_content(
    content: Any,
    *,
    session_files: Sequence[dict[str, Any]],
) -> None:
    """Validate the supported Votrix/Claude user-message content subset.

    Image and document blocks must use ``source.type=file`` and reference the
    scoped copy ID or source upload ID of a file already mounted in this
    Session.  Arbitrary provider IDs, URLs, and inline bytes are deliberately
    rejected at the public boundary.
    """

    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list):
        raise ModelInputValidationError("user.message content must be a string or array")

    index = _mounted_file_index(session_files)
    for block in content:
        if not isinstance(block, dict):
            raise ModelInputValidationError("user.message content blocks must be objects")
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise ModelInputValidationError("text content blocks require text")
            continue
        if block_type not in {"image", "document"}:
            raise ModelInputValidationError(
                f"Unsupported user.message content block type: {block_type}"
            )

        file_id = _managed_file_id(block)
        mounted = _resolve_mounted_file(index, file_id)
        mime_type = _mime_type(mounted)
        if block_type == "image" and mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ModelInputValidationError(
                f"Image file {file_id} has unsupported MIME type {mime_type}"
            )
        if block_type == "document" and mime_type not in SUPPORTED_DOCUMENT_MIME_TYPES:
            raise ModelInputValidationError(
                f"Document file {file_id} has unsupported MIME type {mime_type}"
            )


def adapt_user_message_content(
    content: Any,
    *,
    session_files: Sequence[dict[str, Any]],
    multimodal_input: bool,
) -> Any:
    """Resolve VMA file references and return LangChain standard blocks.

    A model profile without multimodal input receives a trusted sandbox-path
    marker for images and PDFs instead of an invalid Anthropic file ID.  Plain
    UTF-8 text files are ordinary text input and do not require a multimodal
    model.  Oversized or format-mismatched data is also left for sandbox tools
    rather than amplified into the provider request and checkpoint.
    """

    validate_user_message_content(content, session_files=session_files)
    if not isinstance(content, list):
        return content

    index = _mounted_file_index(session_files)
    adapted: list[dict[str, Any]] = []
    inline_images = 0
    inline_total = 0
    for block in content:
        if block.get("type") == "text":
            adapted.append(dict(block))
            continue

        file_id = _managed_file_id(block)
        mounted = _resolve_mounted_file(index, file_id)
        raw = mounted.get("content")
        if not isinstance(raw, bytes):
            raise ModelInputValidationError(
                f"Mounted file {file_id} does not expose verified runtime bytes"
            )
        mime_type = _mime_type(mounted)
        filename = str(mounted.get("filename") or mounted.get("path") or file_id).split("/")[-1]

        if block["type"] == "image":
            eligible = (
                multimodal_input
                and inline_images < MAX_INLINE_IMAGES
                and len(raw) <= MAX_INLINE_IMAGE_BYTES
                and inline_total + len(raw) <= MAX_INLINE_TOTAL_BYTES
                and _valid_image_bytes(raw, mime_type)
            )
            if not eligible:
                adapted.append(_sandbox_marker(mounted, kind="image"))
                continue
            adapted.append(
                {
                    "type": "image",
                    "base64": base64.b64encode(raw).decode("ascii"),
                    "mime_type": mime_type,
                }
            )
            inline_images += 1
            inline_total += len(raw)
            continue

        if mime_type == "text/plain":
            if len(raw) > MAX_INLINE_TEXT_BYTES or inline_total + len(raw) > MAX_INLINE_TOTAL_BYTES:
                adapted.append(_sandbox_marker(mounted, kind="document"))
                continue
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                adapted.append(_sandbox_marker(mounted, kind="document"))
                continue
            adapted.append(
                {
                    "type": "text",
                    "text": f"Contents of attached text file {filename!r}:\n\n{text}",
                }
            )
            inline_total += len(raw)
            continue

        eligible = (
            multimodal_input
            and len(raw) <= MAX_INLINE_PDF_BYTES
            and inline_total + len(raw) <= MAX_INLINE_TOTAL_BYTES
            and raw.startswith(b"%PDF-")
        )
        if not eligible:
            adapted.append(_sandbox_marker(mounted, kind="document"))
            continue
        adapted.append(
            {
                "type": "file",
                "base64": base64.b64encode(raw).decode("ascii"),
                "mime_type": mime_type,
                "filename": filename,
            }
        )
        inline_total += len(raw)

    return adapted


def _managed_file_id(block: dict[str, Any]) -> str:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "file":
        raise ModelInputValidationError(
            f"{block.get('type')} content blocks must use source.type=file"
        )
    file_id = source.get("file_id")
    # The official Managed Agents shape is ``source.file_id``.  Accept the
    # older/custom nested wrapper defensively, but never forward either shape
    # to the model provider unchanged.
    if file_id is None and isinstance(source.get("file"), dict):
        file_id = source["file"].get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise ModelInputValidationError("File content blocks require source.file_id")
    return file_id


def _mounted_file_index(
    session_files: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    index: dict[str, dict[str, Any] | None] = {}
    for mounted in session_files:
        if not isinstance(mounted, dict):
            continue
        aliases = {
            value
            for value in (mounted.get("file_id"), mounted.get("source_file_id"))
            if isinstance(value, str) and value
        }
        for alias in aliases:
            previous = index.get(alias)
            if previous is None and alias in index:
                continue
            if previous is not None and previous is not mounted:
                if (
                    previous.get("path") != mounted.get("path")
                    or previous.get("sha256") != mounted.get("sha256")
                ):
                    index[alias] = None
                    continue
            index[alias] = mounted
    return index


def _resolve_mounted_file(
    index: dict[str, dict[str, Any] | None],
    file_id: str,
) -> dict[str, Any]:
    if file_id not in index:
        raise ModelInputValidationError(
            f"File {file_id} is not mounted in this Session"
        )
    mounted = index[file_id]
    if mounted is None:
        raise ModelInputValidationError(
            f"File {file_id} is mounted more than once; use its Session-scoped file ID"
        )
    return mounted


def _mime_type(mounted: dict[str, Any]) -> str:
    value = str(mounted.get("mime_type") or "application/octet-stream")
    return value.split(";", 1)[0].strip().lower()


def _valid_image_bytes(content: bytes, mime_type: str) -> bool:
    if mime_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def _sandbox_marker(mounted: dict[str, Any], *, kind: str) -> dict[str, str]:
    details = {
        "kind": kind,
        "filename": mounted.get("filename"),
        "mime_type": _mime_type(mounted),
        "path": mounted.get("path"),
    }
    return {
        "type": "text",
        "text": "Attached file is available through sandbox tools: "
        + json.dumps(details, ensure_ascii=False, sort_keys=True),
    }


__all__ = [
    "MAX_INLINE_IMAGES",
    "MAX_INLINE_IMAGE_BYTES",
    "MAX_INLINE_PDF_BYTES",
    "MAX_INLINE_TEXT_BYTES",
    "MAX_INLINE_TOTAL_BYTES",
    "ModelInputValidationError",
    "SUPPORTED_DOCUMENT_MIME_TYPES",
    "SUPPORTED_IMAGE_MIME_TYPES",
    "adapt_user_message_content",
    "validate_user_message_content",
]
