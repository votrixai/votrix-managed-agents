"""Safely validate raster images and bound large ``read_file`` results.

The default ``langchain-e2b`` binary reader stops at 500 KiB. Verified images
already at or below that budget are returned byte-for-byte; larger supported
rasters become one bounded JPEG preview without modifying the sandbox source.
"""

from __future__ import annotations

import math
import warnings
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_PREVIEW_BYTES = 500 * 1024

SUPPORTED_IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})

_SUPPORTED_SOURCE_FORMATS = frozenset({"GIF", "JPEG", "PNG", "WEBP"})
_JPEG_QUALITIES = (95, 90, 85, 80, 75, 70, 65, 60)
_MAX_RESIZE_ATTEMPTS = 4


class ImagePreviewError(ValueError):
    """An image cannot safely become a bounded model preview."""


def prepare_image_preview(data: bytes, suffix: str) -> bytes:
    """Return a verified original or one bounded JPEG preview.

    ``suffix`` decides whether this backend claims the file, but the decoder
    validates the actual format. Large outputs are always JPEG; the provider
    adapter sniffs their bytes instead of assuming the original path's MIME.
    """
    if suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ImagePreviewError(f"unsupported image suffix {suffix!r}")
    if len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ImagePreviewError(
            f"image source exceeds the {MAX_IMAGE_SOURCE_BYTES // (1024 * 1024)} MiB limit"
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                source_format = str(opened.format or "").upper()
                if source_format not in _SUPPORTED_SOURCE_FORMATS:
                    label = source_format or "unknown"
                    raise ImagePreviewError(
                        f"unsupported image format {label}; expected PNG, JPEG, WebP, or GIF"
                    )

                width, height = opened.size
                if width <= 0 or height <= 0:
                    raise ImagePreviewError(f"invalid image dimensions {width}x{height}")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ImagePreviewError(
                        f"image dimensions {width}x{height} exceed the "
                        f"{MAX_IMAGE_PIXELS:,}-pixel decoded-image limit"
                    )

                # ``Image.open`` is lazy. Verify the compressed stream before
                # any original bytes are allowed through to the model.
                opened.verify()

            # Loading the first frame catches decoder errors that an image
            # plugin's structural ``verify`` implementation may not surface.
            with Image.open(BytesIO(data)) as opened:
                opened.seek(0)
                opened.load()
                if len(data) <= MAX_IMAGE_PREVIEW_BYTES:
                    return data

                # Large animated images intentionally become their first
                # displayed frame. Applying EXIF before copying also removes
                # the orientation tag from the newly encoded JPEG.
                opened.seek(0)
                image = ImageOps.exif_transpose(opened)
                image.load()
                image = image.copy()
    except ImagePreviewError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePreviewError("image exceeds the decoded-pixel safety limit") from exc
    except (
        EOFError,
        UnidentifiedImageError,
        OSError,
        RuntimeError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImagePreviewError("file bytes are not a readable supported image") from exc

    try:
        return _encode_bounded_jpeg(_on_white_rgb(image))
    except ImagePreviewError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ImagePreviewError("image could not be encoded as a JPEG preview") from exc


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )


def _on_white_rgb(image: Image.Image) -> Image.Image:
    if not _has_alpha(image):
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    return Image.alpha_composite(background, rgba).convert("RGB")


def _encode_bounded_jpeg(image: Image.Image) -> bytes:
    current = image
    for _attempt in range(_MAX_RESIZE_ATTEMPTS):
        smallest_size: int | None = None
        for quality in _JPEG_QUALITIES:
            encoded = _encode_jpeg(current, quality=quality)
            if len(encoded) <= MAX_IMAGE_PREVIEW_BYTES:
                return encoded
            smallest_size = (
                len(encoded)
                if smallest_size is None
                else min(smallest_size, len(encoded))
            )

        # Preserve the original dimensions through the full quality range.
        # Only then use encoded area as a bounded estimate for the next size.
        if current.size == (1, 1) or smallest_size is None:
            break
        scale = math.sqrt(MAX_IMAGE_PREVIEW_BYTES / smallest_size) * 0.94
        scale = min(0.90, max(0.25, scale))
        next_size = (
            max(1, min(current.width - 1, int(current.width * scale)))
            if current.width > 1
            else 1,
            max(1, min(current.height - 1, int(current.height * scale)))
            if current.height > 1
            else 1,
        )
        if next_size == current.size:
            break
        current = current.resize(next_size, Image.Resampling.LANCZOS)

    raise ImagePreviewError(
        f"image could not be reduced below the {MAX_IMAGE_PREVIEW_BYTES // 1024} KiB preview limit"
    )


def _encode_jpeg(image: Image.Image, *, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


__all__ = [
    "MAX_IMAGE_PIXELS",
    "MAX_IMAGE_PREVIEW_BYTES",
    "MAX_IMAGE_SOURCE_BYTES",
    "SUPPORTED_IMAGE_SUFFIXES",
    "ImagePreviewError",
    "prepare_image_preview",
]
