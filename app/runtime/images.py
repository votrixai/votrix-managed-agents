"""Native image inputs, from durable VMA File ids to model content blocks.

Session events and LangGraph checkpoints keep the small, stable File id. The
bytes are resolved and prepared only at the model-call boundary, so neither a
base64 payload nor an expiring signed URL becomes conversation state.
"""

from __future__ import annotations

import asyncio
import base64
import math
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import HumanMessage, ToolMessage
from PIL import Image, ImageOps, UnidentifiedImageError

ImageResolver = Callable[[str], Awaitable[tuple[bytes, str | None]]]

# A source is bounded before Pillow decodes it. The prepared form is smaller so
# base64 expansion and the rest of the request remain comfortably below common
# provider limits.
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_LONG_EDGE = 2048
MAX_PREPARED_IMAGE_BYTES = 3_500_000

# A long screenshot is useful only while its text remains legible. Send one
# overview plus bounded, overlapping tiles instead of crushing the whole page
# into a single 2048px strip.
LONG_IMAGE_RATIO = 3.0
IMAGE_TILE_EDGE = 2048
IMAGE_TILE_OVERLAP = 128
MAX_IMAGE_TILES = 12

_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class ImageInputError(ValueError):
    """A File cannot safely or usefully become native image input."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str


def prepare_image(data: bytes, declared_mime_type: str | None = None) -> tuple[PreparedImage, ...]:
    """Validate, orient, resize, and encode one provider-neutral image input."""
    if len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ImageInputError(
            f"Image source is over the {MAX_IMAGE_SOURCE_BYTES // (1024 * 1024)} MiB limit"
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                actual_format = str(opened.format or "").upper()
                mime_type = _FORMAT_MIME_TYPES.get(actual_format)
                if mime_type is None:
                    claimed = declared_mime_type or actual_format or "unknown"
                    raise ImageInputError(f"Unsupported image format: {claimed}")

                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ImageInputError(
                        f"Image dimensions {width}x{height} exceed the decoded-pixel limit"
                    )

                # `Image.open` reads only enough bytes to identify a file.
                # Verify the compressed stream before an unchanged image is
                # allowed through as model input.
                opened.verify()

            with Image.open(BytesIO(data)) as opened:
                orientation = opened.getexif().get(274, 1)
                animated = bool(getattr(opened, "is_animated", False))
                keep_original = (
                    not animated
                    and orientation in (None, 1)
                    and max(width, height) <= MAX_IMAGE_LONG_EDGE
                    and len(data) <= MAX_PREPARED_IMAGE_BYTES
                )
                if keep_original:
                    # The format was decoded and verified above. Keeping these
                    # bytes avoids a second lossy compression pass.
                    return (PreparedImage(data=data, mime_type=mime_type),)

                opened.seek(0)
                image = ImageOps.exif_transpose(opened).copy()
    except ImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageInputError("Image exceeds the decoded-pixel safety limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageInputError("File bytes are not a readable supported image") from exc

    lossless = actual_format in {"PNG", "GIF"} or "A" in image.getbands()
    width, height = image.size
    ratio = max(width, height) / min(width, height)
    if ratio >= LONG_IMAGE_RATIO and max(width, height) > MAX_IMAGE_LONG_EDGE:
        return _prepare_long_image(
            image,
            output_format=actual_format,
            lossless=lossless,
        )

    resized = _fit_long_edge(image, MAX_IMAGE_LONG_EDGE)
    return (
        _encode_bounded(
            resized,
            output_format=actual_format,
            lossless=lossless,
        ),
    )


def _prepare_long_image(
    image: Image.Image,
    *,
    output_format: str,
    lossless: bool,
) -> tuple[PreparedImage, ...]:
    vertical = image.height >= image.width
    short_edge = image.width if vertical else image.height
    if short_edge > IMAGE_TILE_EDGE:
        scale = IMAGE_TILE_EDGE / short_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    long_edge = image.height if vertical else image.width
    stride = IMAGE_TILE_EDGE - IMAGE_TILE_OVERLAP
    tile_count = max(1, math.ceil((long_edge - IMAGE_TILE_EDGE) / stride) + 1)
    if tile_count > MAX_IMAGE_TILES:
        target_long = IMAGE_TILE_EDGE + stride * (MAX_IMAGE_TILES - 1)
        scale = target_long / long_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        long_edge = image.height if vertical else image.width
        tile_count = MAX_IMAGE_TILES

    prepared = [
        _encode_bounded(
            _fit_long_edge(image, MAX_IMAGE_LONG_EDGE),
            output_format=output_format,
            lossless=lossless,
        )
    ]
    for index in range(tile_count):
        start = min(index * stride, max(0, long_edge - IMAGE_TILE_EDGE))
        end = min(long_edge, start + IMAGE_TILE_EDGE)
        box = (
            (0, start, image.width, end)
            if vertical
            else (start, 0, end, image.height)
        )
        prepared.append(
            _encode_bounded(
                image.crop(box),
                output_format=output_format,
                lossless=lossless,
            )
        )
    return tuple(prepared)


def _fit_long_edge(image: Image.Image, edge: int) -> Image.Image:
    if max(image.size) <= edge:
        return image.copy()
    scale = edge / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def _encode_bounded(
    image: Image.Image,
    *,
    output_format: str,
    lossless: bool,
) -> PreparedImage:
    """Encode within the byte limit without making bytes disagree with MIME."""
    current = image
    for _attempt in range(8):
        buffer = BytesIO()
        if output_format == "JPEG":
            rgb = current.convert("RGB") if current.mode != "RGB" else current
            rgb.save(buffer, format="JPEG", quality=85, optimize=True)
        elif output_format == "PNG":
            png = current
            if png.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                png = png.convert("RGBA" if "A" in png.getbands() else "RGB")
            png.save(buffer, format="PNG", optimize=True, compress_level=9)
        elif output_format == "GIF":
            palette = current.convert(
                "P",
                palette=Image.Palette.ADAPTIVE,
                colors=256,
            )
            palette.save(buffer, format="GIF", optimize=True)
        else:
            rgb = current.convert("RGB") if current.mode != "RGB" else current
            if lossless:
                current.save(buffer, format="WEBP", lossless=True, method=6)
            else:
                rgb.save(buffer, format="WEBP", quality=85, method=6)
        encoded = buffer.getvalue()
        if len(encoded) <= MAX_PREPARED_IMAGE_BYTES:
            return PreparedImage(
                data=encoded,
                mime_type=_FORMAT_MIME_TYPES[output_format],
            )
        current = current.resize(
            (max(1, round(current.width * 0.85)), max(1, round(current.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
    raise ImageInputError("Image could not be reduced to the model-input byte limit")


class NativeImageMiddleware(AgentMiddleware):
    """Make durable and tool-produced images valid OpenRouter model input.

    VMA File references are hydrated late. DeepAgents' `read_file` returns
    standard image blocks inside a ToolMessage, but OpenRouter accepts images
    in user content while tool results are strings. Those tool blocks are
    therefore exposed to the model as a textual tool result followed by a
    temporary user image message. Neither transformation mutates graph state.
    """

    def __init__(self, resolve: ImageResolver | None = None) -> None:
        self._resolve = resolve
        self._cache: dict[str, tuple[dict[str, Any], ...]] = {}

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        messages = await self._prepare_messages(request.messages)
        return await handler(request.override(messages=messages))

    async def _prepare_messages(self, messages: list[Any]) -> list[Any]:
        """Hydrate File ids and keep parallel tool-result messages contiguous."""
        prepared: list[Any] = []
        pending_tool_images: list[tuple[str, dict[str, Any]]] = []

        for original in messages:
            message = await self._hydrate_message(original)
            if isinstance(message, ToolMessage):
                tool_message, images = self._split_read_file_images(message)
                prepared.append(tool_message)
                pending_tool_images.extend(images)
                continue

            if pending_tool_images:
                prepared.append(self._tool_images_message(pending_tool_images))
                pending_tool_images = []
            prepared.append(message)

        if pending_tool_images:
            prepared.append(self._tool_images_message(pending_tool_images))
        return prepared

    @staticmethod
    def _split_read_file_images(
        message: ToolMessage,
    ) -> tuple[ToolMessage, list[tuple[str, dict[str, Any]]]]:
        content = message.content
        if message.name != "read_file" or not isinstance(content, list):
            return message, []

        images = [
            block
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "image"
            and isinstance(block.get("base64"), str)
            and isinstance(block.get("mime_type"), str)
        ]
        if not images:
            return message, []

        path = str(
            message.additional_kwargs.get("read_file_path")
            or "an image file"
        )
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        )
        notice = (
            f"read_file returned native image content for {path}. "
            "The image is attached after this tool-result batch."
        )
        if text:
            notice = f"{text}\n\n{notice}"
        tool_message = message.model_copy(update={"content": notice})
        return tool_message, [(path, block) for block in images]

    @staticmethod
    def _tool_images_message(images: list[tuple[str, dict[str, Any]]]) -> HumanMessage:
        paths = "\n".join(f"- {path}" for path, _block in images)
        return HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": f"Native image content returned by read_file:\n{paths}",
                },
                *(block for _path, block in images),
            ]
        )

    async def _hydrate_message(self, message: Any) -> Any:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return message

        hydrated: list[Any] = []
        changed = False
        for block in content:
            if not (
                isinstance(block, dict)
                and block.get("type") == "image"
                and isinstance(block.get("file_id"), str)
                and block.get("file_id")
            ):
                hydrated.append(block)
                continue

            changed = True
            file_id = str(block["file_id"])
            replacements = self._cache.get(file_id)
            if replacements is None:
                if self._resolve is None:
                    raise ImageInputError(
                        f"No resolver is available for image File {file_id}"
                    )
                data, mime_type = await self._resolve(file_id)
                # Pillow decoding and resampling are CPU work. Keep one large
                # screenshot from blocking every other async turn on this
                # process while it is prepared.
                prepared = await asyncio.to_thread(prepare_image, data, mime_type)
                replacements = tuple(
                    {
                        "type": "image",
                        "base64": base64.b64encode(item.data).decode("ascii"),
                        "mime_type": item.mime_type,
                    }
                    for item in prepared
                )
                self._cache[file_id] = replacements

            if len(replacements) == 1:
                hydrated.extend(replacements)
                continue

            hydrated.append(
                {
                    "type": "text",
                    "text": f"Image {file_id} overview, followed by overlapping detail tiles:",
                }
            )
            hydrated.append(replacements[0])
            for index, replacement in enumerate(replacements[1:], start=1):
                hydrated.append(
                    {
                        "type": "text",
                        "text": f"Image {file_id}, detail tile {index} of {len(replacements) - 1}:",
                    }
                )
                hydrated.append(replacement)

        if not changed:
            return message
        return message.model_copy(update={"content": hydrated})


__all__ = [
    "ImageInputError",
    "ImageResolver",
    "MAX_IMAGE_SOURCE_BYTES",
    "NativeImageMiddleware",
    "PreparedImage",
    "prepare_image",
]
