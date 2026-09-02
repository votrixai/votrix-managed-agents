"""Provider-neutral preparation and late hydration of native image inputs."""

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openrouter.chat_models import _convert_message_to_dict
from PIL import Image

from app.runtime.images import (
    ImageInputError,
    MAX_IMAGE_LONG_EDGE,
    MAX_PREPARED_IMAGE_BYTES,
    NativeImageMiddleware,
    prepare_image,
)
from app.utils.sandbox import LazyE2BBackend


def encoded_image(
    size: tuple[int, int],
    *,
    format: str = "PNG",
    color: tuple[int, int, int] = (20, 80, 140),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=format)
    return buffer.getvalue()


def dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.size


def test_a_small_verified_image_is_not_reencoded():
    source = encoded_image((640, 480))

    prepared = prepare_image(source, "image/png")

    assert prepared[0].data == source
    assert prepared[0].mime_type == "image/png"


def test_a_large_photo_is_resized_and_bounded():
    source = encoded_image((5000, 3000), format="JPEG")

    prepared = prepare_image(source, "image/jpeg")

    assert len(prepared) == 1
    assert max(dimensions(prepared[0].data)) <= MAX_IMAGE_LONG_EDGE
    assert len(prepared[0].data) <= MAX_PREPARED_IMAGE_BYTES
    assert prepared[0].mime_type == "image/jpeg"


def test_a_long_screenshot_gets_an_overview_and_legible_tiles():
    source = encoded_image((800, 6000))

    prepared = prepare_image(source, "image/png")

    assert len(prepared) > 2
    assert max(dimensions(prepared[0].data)) <= MAX_IMAGE_LONG_EDGE
    assert all(max(dimensions(item.data)) <= MAX_IMAGE_LONG_EDGE for item in prepared[1:])
    assert all(len(item.data) <= MAX_PREPARED_IMAGE_BYTES for item in prepared)


def test_non_image_bytes_are_refused():
    with pytest.raises(ImageInputError, match="not a readable supported image"):
        prepare_image(b"not an image", "image/png")


def test_a_truncated_small_image_is_not_passed_through_unchanged():
    source = encoded_image((20, 20))

    with pytest.raises(ImageInputError, match="not a readable supported image"):
        prepare_image(source[:-10], "image/png")


async def test_file_ids_are_hydrated_late_and_cached_without_mutating_state():
    source = encoded_image((320, 200))
    calls: list[str] = []

    async def resolve(file_id: str):
        calls.append(file_id)
        return source, "image/png"

    middleware = NativeImageMiddleware(resolve)
    message = HumanMessage(
        content=[
            {"type": "text", "text": "inspect this"},
            {"type": "image", "file_id": "file_screen"},
        ]
    )

    first = await middleware._hydrate_message(message)
    second = await middleware._hydrate_message(message)

    assert calls == ["file_screen"]
    assert message.content[-1] == {"type": "image", "file_id": "file_screen"}
    assert first.content[-1]["type"] == "image"
    assert first.content[-1]["mime_type"] == "image/png"
    assert "base64" in first.content[-1]
    assert second.content == first.content


async def test_sandbox_read_file_prepares_large_images_as_native_content():
    image = Image.effect_noise((1600, 1200), 100).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    source = buffer.getvalue()
    assert len(source) > 500 * 1024

    sandbox = SimpleNamespace(read_bytes=AsyncMock(return_value=source))
    backend = LazyE2BBackend(sandbox)

    result = await backend.aread("/home/user/uploads/large.png")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    prepared = base64.b64decode(result.file_data["content"])
    assert len(prepared) <= MAX_PREPARED_IMAGE_BYTES
    assert max(dimensions(prepared)) <= 1600
    with Image.open(BytesIO(prepared)) as image:
        assert image.format == "PNG"
    sandbox.read_bytes.assert_awaited_once()


async def test_sandbox_read_file_leaves_non_images_with_the_existing_backend():
    expected = SimpleNamespace(error=None, file_data={"content": "hello", "encoding": "utf-8"})
    delegate = SimpleNamespace(aread=AsyncMock(return_value=expected))
    backend = LazyE2BBackend(SimpleNamespace())
    backend._ready = AsyncMock(return_value=delegate)

    result = await backend.aread("/home/user/uploads/notes.txt", offset=4, limit=10)

    assert result is expected
    delegate.aread.assert_awaited_once_with(
        "/home/user/uploads/notes.txt",
        offset=4,
        limit=10,
    )


async def test_read_file_images_become_valid_openrouter_user_content():
    image_block = {
        "type": "image",
        "base64": base64.b64encode(encoded_image((16, 16))).decode("ascii"),
        "mime_type": "image/png",
    }
    middleware = NativeImageMiddleware()
    first = ToolMessage(
        content=[image_block],
        tool_call_id="call_first",
        name="read_file",
        additional_kwargs={"read_file_path": "/home/user/uploads/first.png"},
    )
    second = ToolMessage(
        content=[image_block],
        tool_call_id="call_second",
        name="read_file",
        additional_kwargs={"read_file_path": "/home/user/uploads/second.png"},
    )

    prepared = await middleware._prepare_messages(
        [AIMessage(content="", tool_calls=[]), first, second]
    )

    assert [message.type for message in prepared] == ["ai", "tool", "tool", "human"]
    assert isinstance(prepared[1].content, str)
    assert isinstance(prepared[2].content, str)
    payload = [_convert_message_to_dict(message) for message in prepared]
    assert payload[1]["role"] == "tool"
    assert payload[2]["role"] == "tool"
    assert payload[3]["role"] == "user"
    assert [block["type"] for block in payload[3]["content"]] == [
        "text",
        "image_url",
        "image_url",
    ]
