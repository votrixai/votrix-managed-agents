"""Bounded raster handling in the E2B backend used by ``read_file``."""

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from app.utils.image_preview import (
    MAX_IMAGE_LONG_EDGE,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_PREVIEW_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
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
    Image.new(mode, size, color).save(buffer, format=format, exif=exif)
    return buffer.getvalue()


def _opened(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


async def test_large_png_bypasses_delegate_and_returns_a_bounded_native_read_result():
    image = Image.effect_noise((1600, 1200), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="PNG")
    source_bytes = source.getvalue()
    assert len(source_bytes) > 500 * 1024

    sandbox = SimpleNamespace(read_bytes=AsyncMock(return_value=source_bytes))
    backend = LazyE2BBackend(sandbox)
    backend._ready = AsyncMock()

    result = await backend.aread("/home/user/outputs/generated.png")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    preview = base64.b64decode(result.file_data["content"])
    assert len(preview) <= MAX_IMAGE_PREVIEW_BYTES
    with _opened(preview) as decoded:
        assert decoded.format == "PNG"
        assert max(decoded.size) <= MAX_IMAGE_LONG_EDGE
    sandbox.read_bytes.assert_awaited_once_with(
        "/home/user/outputs/generated.png", max_bytes=MAX_IMAGE_SOURCE_BYTES
    )
    backend._ready.assert_not_awaited()


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


async def test_missing_and_oversized_sources_return_clear_read_errors():
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


async def test_decoded_pixel_limit_and_corrupt_bytes_return_clear_read_errors():
    # One-bit pixels keep this safety test near 5 MiB in memory instead of
    # allocating the roughly 120 MiB an RGB image of these dimensions needs.
    buffer = BytesIO()
    Image.new("1", (MAX_IMAGE_PIXELS // 5000 + 1, 5000), 0).save(
        buffer, format="PNG"
    )
    too_many_pixels = buffer.getvalue()
    sandbox = SimpleNamespace(read_bytes=AsyncMock(return_value=too_many_pixels))
    result = await LazyE2BBackend(sandbox).aread("/home/user/large.png")
    assert result.error is not None
    assert "decoded-image limit" in result.error

    sandbox.read_bytes = AsyncMock(return_value=b"not an image")
    result = await LazyE2BBackend(sandbox).aread("/home/user/broken.jpg")
    assert result.error is not None
    assert "not a readable supported image" in result.error


def test_exif_orientation_is_applied_and_removed():
    exif = Image.Exif()
    exif[274] = 6
    source = _encoded_image((80, 40), format="JPEG", exif=exif)

    preview = prepare_image_preview(source, ".jpg")

    with _opened(preview) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (40, 80)
        assert decoded.getexif().get(274, 1) == 1


def test_transparency_and_output_suffix_format_stay_consistent():
    transparent_png = _encoded_image(
        (64, 64), mode="RGBA", color=(200, 10, 20, 0)
    )

    png_preview = prepare_image_preview(transparent_png, ".png")
    with _opened(png_preview) as decoded:
        assert decoded.format == "PNG"
        assert decoded.convert("RGBA").getpixel((0, 0))[3] == 0

    jpeg_preview = prepare_image_preview(transparent_png, ".jpg")
    with _opened(jpeg_preview) as decoded:
        assert decoded.format == "JPEG"
        red, green, blue = decoded.convert("RGB").getpixel((0, 0))
        assert min(red, green, blue) > 240


def test_animated_gif_is_reduced_to_one_bounded_frame():
    buffer = BytesIO()
    frames = [
        Image.new("RGBA", (32, 24), color)
        for color in ((255, 0, 0, 255), (0, 0, 255, 255))
    ]
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )

    preview = prepare_image_preview(buffer.getvalue(), ".gif")

    with _opened(preview) as decoded:
        assert decoded.format == "GIF"
        assert not getattr(decoded, "is_animated", False)
