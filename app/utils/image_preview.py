"""Bounded raster previews for DeepAgents' native ``read_file`` blocks.

The E2B adapter bundled with DeepAgents refuses binary files over 500 KiB.
Generated screenshots and PNGs commonly cross that threshold, even though a
smaller representation is enough for a model to inspect them.  This module
validates and decodes a bounded source, then emits one still image whose bytes
agree with the MIME type DeepAgents infers from the file suffix.
"""

from __future__ import annotations

import math
import warnings
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_LONG_EDGE = 2048
# Stay near DeepAgents' existing 500 KiB preview budget while bounding the
# base64/checkpoint and provider-request growth caused by a tool result.
MAX_IMAGE_PREVIEW_BYTES = 460 * 1024

IMAGE_SUFFIX_FORMATS = {
    ".gif": "GIF",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}

_SUPPORTED_SOURCE_FORMATS = frozenset(IMAGE_SUFFIX_FORMATS.values())
_EXIF_ORIENTATION = 274


class ImagePreviewError(ValueError):
    """An image cannot safely become a bounded model preview."""


def prepare_image_preview(data: bytes, suffix: str) -> bytes:
    """Validate, orient, resize, and encode one raster image.

    The output format follows ``suffix`` rather than trusting the compressed
    stream. DeepAgents derives the content block MIME type from that same
    suffix, so this keeps the declared MIME and actual bytes consistent even
    for a misnamed but otherwise supported image.
    """
    output_format = IMAGE_SUFFIX_FORMATS.get(suffix.lower())
    if output_format is None:
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

                animated = bool(getattr(opened, "is_animated", False))
                # ``Image.open`` is lazy. Verify the compressed stream before
                # unchanged bytes can be returned to the model.
                opened.verify()

            with Image.open(BytesIO(data)) as opened:
                # Some decoders (notably PNG) load metadata while reading EXIF,
                # so do this only after the separate verification pass above.
                orientation = opened.getexif().get(_EXIF_ORIENTATION, 1)
                if (
                    source_format == output_format
                    and orientation in (None, 1)
                    and not animated
                    and max(width, height) <= MAX_IMAGE_LONG_EDGE
                    and len(data) <= MAX_IMAGE_PREVIEW_BYTES
                ):
                    return data

                # An animated image is deliberately reduced to its first frame:
                # one bounded preview is useful here; decoding every frame is
                # an unbounded multiplication of otherwise safe dimensions.
                opened.seek(0)
                image = ImageOps.exif_transpose(opened)
                image.load()
                image = image.copy()
    except ImagePreviewError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePreviewError("image exceeds the decoded-pixel safety limit") from exc
    except (UnidentifiedImageError, OSError, RuntimeError, SyntaxError, ValueError) as exc:
        raise ImagePreviewError("file bytes are not a readable supported image") from exc

    image = _fit_long_edge(image, MAX_IMAGE_LONG_EDGE)
    return _encode_bounded(image, output_format)


def _fit_long_edge(image: Image.Image, edge: int) -> Image.Image:
    if max(image.size) <= edge:
        return image.copy()
    scale = edge / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )


def _normalized_for_format(image: Image.Image, output_format: str) -> Image.Image:
    has_alpha = _has_alpha(image)
    if output_format == "JPEG":
        if has_alpha:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            return Image.alpha_composite(background, rgba).convert("RGB")
        return image.convert("RGB")
    if output_format in {"PNG", "WEBP"}:
        return image.convert("RGBA" if has_alpha else "RGB")
    # GIF has one-bit transparency. Keep an RGBA working image so the encoder
    # can reserve one palette entry instead of silently painting alpha black.
    return image.convert("RGBA" if has_alpha else "RGB")


def _encode_bounded(image: Image.Image, output_format: str) -> bytes:
    current = _normalized_for_format(image, output_format)
    for attempt in range(10):
        encoded = _encode(current, output_format, quality=max(45, 85 - attempt * 5))
        if len(encoded) <= MAX_IMAGE_PREVIEW_BYTES:
            return encoded

        # Encoded raster size trends with pixel area. Use that relationship to
        # converge quickly, with a margin for headers and imperfect scaling.
        scale = math.sqrt(MAX_IMAGE_PREVIEW_BYTES / len(encoded)) * 0.92
        scale = min(0.85, max(0.25, scale))
        next_size = (
            max(1, round(current.width * scale)),
            max(1, round(current.height * scale)),
        )
        if next_size == current.size:
            break
        current = current.resize(next_size, Image.Resampling.LANCZOS)

    raise ImagePreviewError(
        f"image could not be reduced below the {MAX_IMAGE_PREVIEW_BYTES // 1024} KiB preview limit"
    )


def _encode(image: Image.Image, output_format: str, *, quality: int) -> bytes:
    buffer = BytesIO()
    if output_format == "JPEG":
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
    elif output_format == "PNG":
        image.save(buffer, format="PNG", optimize=True, compress_level=9)
    elif output_format == "WEBP":
        image.save(buffer, format="WEBP", quality=quality, method=6)
    else:
        _save_gif(image, buffer)
    return buffer.getvalue()


def _save_gif(image: Image.Image, buffer: BytesIO) -> None:
    if not _has_alpha(image):
        image.convert("P", palette=Image.Palette.ADAPTIVE, colors=256).save(
            buffer, format="GIF", optimize=True
        )
        return

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    palette = rgba.convert("RGB").convert(
        "P", palette=Image.Palette.ADAPTIVE, colors=255
    )
    transparent = alpha.point(lambda value: 255 if value <= 127 else 0)
    palette.paste(255, mask=transparent)
    palette.save(buffer, format="GIF", optimize=True, transparency=255)


__all__ = [
    "IMAGE_SUFFIX_FORMATS",
    "MAX_IMAGE_LONG_EDGE",
    "MAX_IMAGE_PIXELS",
    "MAX_IMAGE_PREVIEW_BYTES",
    "MAX_IMAGE_SOURCE_BYTES",
    "ImagePreviewError",
    "prepare_image_preview",
]
