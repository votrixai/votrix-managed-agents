"""Bounded raster handling in the E2B backend used by ``read_file``."""

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.utils import image_preview, sandbox as sandbox_utils
from app.utils.image_preview import (
    MAX_IMAGE_PREVIEW_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
    ImagePreviewError,
    prepare_image_preview,
)
from app.utils.sandbox import LazyE2BBackend


def _encoded_image(
    size: tuple[int, int],
    *,
    format: str = "PNG",
    mode: str = "RGB",
    color: tuple[int, ...] = (20, 80, 140),
    exif: Image.Exif | None = None,
) -> bytes:
    buffer = BytesIO()
    save_kwargs = {"exif": exif} if exif is not None else {}
    Image.new(mode, size, color).save(buffer, format=format, **save_kwargs)
    return buffer.getvalue()


def _padded_to(data: bytes, size: int) -> bytes:
    assert len(data) <= size
    return data + b"\0" * (size - len(data))


def _opened(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


async def test_small_png_is_validated_and_returned_byte_for_byte_without_a_write():
    source = _encoded_image((320, 200))
    sandbox = SimpleNamespace(
        read_bytes=AsyncMock(return_value=source),
        write_bytes=AsyncMock(),
    )
    backend = LazyE2BBackend(sandbox)
    backend._ready = AsyncMock()

    result = await backend.aread("/home/user/outputs/generated.png")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == source
    sandbox.read_bytes.assert_awaited_once_with(
        "/home/user/outputs/generated.png", max_bytes=MAX_IMAGE_SOURCE_BYTES
    )
    sandbox.write_bytes.assert_not_awaited()
    backend._ready.assert_not_awaited()


async def test_large_png_becomes_a_bounded_jpeg_before_any_dimension_is_lost(
    monkeypatch,
):
    image = Image.effect_noise((900, 700), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="PNG")
    source_bytes = source.getvalue()
    assert len(source_bytes) > MAX_IMAGE_PREVIEW_BYTES

    attempts: list[tuple[tuple[int, int], int]] = []
    encode_jpeg = image_preview._encode_jpeg

    def recording_encode(image: Image.Image, *, quality: int) -> bytes:
        attempts.append((image.size, quality))
        return encode_jpeg(image, quality=quality)

    monkeypatch.setattr(image_preview, "_encode_jpeg", recording_encode)
    sandbox = SimpleNamespace(
        read_bytes=AsyncMock(return_value=source_bytes),
        write_bytes=AsyncMock(),
    )
    backend = LazyE2BBackend(sandbox)
    backend._ready = AsyncMock()

    result = await backend.aread("/home/user/outputs/generated.png")

    assert result.error is None
    assert result.file_data is not None
    preview = base64.b64decode(result.file_data["content"])
    assert len(preview) <= MAX_IMAGE_PREVIEW_BYTES
    with _opened(preview) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == image.size
    assert attempts[:2] == [((900, 700), 95), ((900, 700), 90)]
    assert all(size == image.size for size, _quality in attempts)
    sandbox.write_bytes.assert_not_awaited()
    backend._ready.assert_not_awaited()


def test_jpeg_resizes_only_after_the_full_quality_ladder(monkeypatch):
    image = Image.effect_noise((1600, 1200), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="PNG")
    assert len(source.getvalue()) > MAX_IMAGE_PREVIEW_BYTES

    attempts: list[tuple[tuple[int, int], int]] = []
    encode_jpeg = image_preview._encode_jpeg

    def recording_encode(image: Image.Image, *, quality: int) -> bytes:
        attempts.append((image.size, quality))
        return encode_jpeg(image, quality=quality)

    monkeypatch.setattr(image_preview, "_encode_jpeg", recording_encode)
    preview = prepare_image_preview(source.getvalue(), ".png")

    assert len(preview) <= MAX_IMAGE_PREVIEW_BYTES
    with _opened(preview) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.width < image.width
        assert decoded.height < image.height
    assert [quality for size, quality in attempts if size == image.size] == [
        95,
        90,
        85,
        80,
        75,
        70,
        65,
        60,
    ]
    first_resized = next(
        index for index, (size, _) in enumerate(attempts) if size != image.size
    )
    assert attempts[first_resized][1] == 95


def test_exact_preview_boundary_is_original_and_one_byte_over_is_jpeg():
    source = _encoded_image((16, 16))
    at_limit = _padded_to(source, MAX_IMAGE_PREVIEW_BYTES)
    over_limit = at_limit + b"\0"

    assert prepare_image_preview(at_limit, ".png") == at_limit

    preview = prepare_image_preview(over_limit, ".png")
    assert preview != over_limit
    assert len(preview) <= MAX_IMAGE_PREVIEW_BYTES
    with _opened(preview) as decoded:
        assert decoded.format == "JPEG"


def test_small_exif_and_animation_are_preserved_byte_for_byte():
    exif = Image.Exif()
    exif[274] = 6
    jpeg = _encoded_image((80, 40), format="JPEG", exif=exif)

    frames = [
        Image.new("RGBA", (32, 24), color)
        for color in ((255, 0, 0, 255), (0, 0, 255, 255))
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    gif = buffer.getvalue()

    assert prepare_image_preview(jpeg, ".jpg") == jpeg
    assert prepare_image_preview(gif, ".gif") == gif
    with Image.open(BytesIO(prepare_image_preview(gif, ".gif"))) as decoded:
        assert decoded.is_animated
        assert decoded.n_frames == 2


def test_large_exif_is_transposed_and_large_animation_uses_its_first_frame():
    exif = Image.Exif()
    exif[274] = 6
    jpeg = _padded_to(
        _encoded_image((80, 40), format="JPEG", exif=exif),
        MAX_IMAGE_PREVIEW_BYTES + 1,
    )
    with _opened(prepare_image_preview(jpeg, ".jpg")) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (40, 80)
        assert decoded.getexif().get(274, 1) == 1

    frames = [
        Image.new("RGB", (32, 24), color)
        for color in ((255, 0, 0), (0, 0, 255))
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    gif = _padded_to(buffer.getvalue(), MAX_IMAGE_PREVIEW_BYTES + 1)

    with _opened(prepare_image_preview(gif, ".gif")) as decoded:
        red, green, blue = decoded.convert("RGB").getpixel((0, 0))
        assert decoded.format == "JPEG"
        assert red > 200 and green < 40 and blue < 40


def test_large_transparency_is_composited_over_white():
    transparent = _padded_to(
        _encoded_image(
            (64, 64), mode="RGBA", color=(200, 10, 20, 0), format="PNG"
        ),
        MAX_IMAGE_PREVIEW_BYTES + 1,
    )

    with _opened(prepare_image_preview(transparent, ".png")) as decoded:
        assert decoded.format == "JPEG"
        red, green, blue = decoded.convert("RGB").getpixel((0, 0))
        assert min(red, green, blue) > 240


async def test_non_image_reads_keep_the_existing_delegate_contract():
    expected = SimpleNamespace(
        error=None, file_data={"content": "hello", "encoding": "utf-8"}
    )
    delegate = SimpleNamespace(aread=AsyncMock(return_value=expected))
    backend = LazyE2BBackend(SimpleNamespace())
    backend._ready = AsyncMock(return_value=delegate)

    result = await backend.aread("/home/user/notes.txt", offset=4, limit=10)

    assert result is expected
    delegate.aread.assert_awaited_once_with(
        "/home/user/notes.txt", offset=4, limit=10
    )


async def test_missing_and_oversized_sources_return_clear_read_errors(monkeypatch):
    missing_sandbox = SimpleNamespace(
        read_bytes=AsyncMock(side_effect=FileNotFoundError("gone"))
    )
    missing = await LazyE2BBackend(missing_sandbox).aread("/home/user/gone.png")
    assert missing.error == "File '/home/user/gone.png' does not exist"

    large_sandbox = SimpleNamespace(
        read_bytes=AsyncMock(side_effect=ValueError("over limit"))
    )
    large = await LazyE2BBackend(large_sandbox).aread("/home/user/huge.webp")
    assert large.error is not None
    assert "20 MiB image source limit" in large.error

    # Enforce the limit again after transfer in case a backend violates the
    # max_bytes contract or the remote file changes while it is being read.
    source = _encoded_image((8, 8))
    monkeypatch.setattr(image_preview, "MAX_IMAGE_SOURCE_BYTES", len(source))
    changed_sandbox = SimpleNamespace(
        read_bytes=AsyncMock(return_value=source + b"\0")
    )
    changed = await LazyE2BBackend(changed_sandbox).aread("/home/user/changed.png")
    assert changed.error is not None
    assert "image source exceeds" in changed.error

    non_regular_sandbox = SimpleNamespace(
        read_bytes=AsyncMock(
            side_effect=sandbox_utils._NonRegularFileError("not a regular file")
        )
    )
    non_regular = await LazyE2BBackend(non_regular_sandbox).aread(
        "/home/user/pipe.png"
    )
    assert non_regular.error == "File '/home/user/pipe.png' is not a regular file"


async def test_pixel_limit_corrupt_bytes_and_wrong_format_are_clear_errors(
    monkeypatch,
):
    monkeypatch.setattr(image_preview, "MAX_IMAGE_PIXELS", 99)
    sandbox = SimpleNamespace(
        read_bytes=AsyncMock(return_value=_encoded_image((10, 10)))
    )
    result = await LazyE2BBackend(sandbox).aread("/home/user/large.png")
    assert result.error is not None
    assert "decoded-image limit" in result.error

    sandbox.read_bytes = AsyncMock(return_value=b"not an image")
    result = await LazyE2BBackend(sandbox).aread("/home/user/broken.jpg")
    assert result.error is not None
    assert "not a readable supported image" in result.error

    tiff = _encoded_image((8, 8), format="TIFF")
    with pytest.raises(ImagePreviewError, match="unsupported image format TIFF"):
        prepare_image_preview(tiff, ".png")
