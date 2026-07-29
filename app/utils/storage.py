"""Object storage: the bytes that do not belong in a database.

Rows live in Postgres, bytes live in R2. Nothing here knows what it is storing
— files, skill packages, whatever — it only knows keys and bytes.

Keys are never shown to clients. A caller holds a `file_id` or `skill_id` and
the service looks the key up, so nobody outside can name an object directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.utils.id_generator import new_id

# R2 has no regions, but the S3 client insists on being told one.
S3_REGION = "auto"

_session: Any | None = None
_lock = asyncio.Lock()


class StorageNotConfigured(RuntimeError):
    """No bucket credentials, so there is nowhere to put anything."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    size_bytes: int
    sha256: str


def object_key(
    *,
    organization_id: str,
    category: str,
    filename: str,
    content_sha256: str | None = None,
) -> str:
    """Where an object goes.

    Organization first so one tenant's objects are never interleaved with
    another's, then the date, then a content fingerprint — which means the same
    bytes uploaded twice land on the same key instead of accumulating copies.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unique = content_sha256[:16] if content_sha256 else new_id("obj")
    return "/".join(
        (
            "organizations",
            _safe(organization_id),
            _safe(category),
            date,
            f"{unique}_{_safe_filename(filename)}",
        )
    )


async def save_bytes(
    data: bytes,
    *,
    organization_id: str,
    category: str,
    filename: str,
    mime_type: str | None = None,
) -> StoredObject:
    settings = _require_settings()
    content_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    sha256 = hashlib.sha256(data).hexdigest()
    key = object_key(
        organization_id=organization_id,
        category=category,
        filename=filename,
        content_sha256=sha256,
    )
    async with _client() as client:
        await client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    return StoredObject(key=key, content_type=content_type, size_bytes=len(data), sha256=sha256)


async def download_bytes(key: str) -> tuple[bytes, str | None]:
    settings = _require_settings()
    async with _client() as client:
        response = await client.get_object(Bucket=settings.s3_bucket_name, Key=key)
        return await response["Body"].read(), response.get("ContentType")


async def object_size(key: str) -> int | None:
    """How big the stored object is, or None if it is not there.

    The size a client declared before uploading is a claim; this is what the
    bucket actually received. It is also how a two-step upload finds out
    whether step two ever happened.
    """
    settings = _require_settings()
    async with _client() as client:
        try:
            response = await client.head_object(Bucket=settings.s3_bucket_name, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise
    return int(response["ContentLength"])


def _is_missing(exc: Exception) -> bool:
    """S3 answers a HEAD for a key that is not there with a bare 404."""
    response = getattr(exc, "response", None) or {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code")
    return status == 404 or code in ("404", "NoSuchKey", "NotFound")


async def delete_object(key: str) -> None:
    settings = _require_settings()
    async with _client() as client:
        await client.delete_object(Bucket=settings.s3_bucket_name, Key=key)


async def presigned_upload_url(key: str, *, mime_type: str, expires_in: int = 900) -> str:
    """A URL the client PUTs to directly, so the bytes never touch this service."""
    settings = _require_settings()
    async with _client() as client:
        return await client.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket_name, "Key": key, "ContentType": mime_type},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )


async def presigned_download_url(key: str, *, expires_in: int = 300) -> str:
    """A short-lived read URL — how a sandbox fetches its own skills.

    The sandbox gets one URL for one object, never a standing credential.
    """
    settings = _require_settings()
    async with _client() as client:
        return await client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket_name, "Key": key},
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )


def _require_settings():
    settings = get_settings()
    if not (settings.s3_bucket_name and settings.s3_access_key_id and settings.s3_secret_access_key):
        raise StorageNotConfigured("S3_BUCKET_NAME, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY are required")
    return settings


def _client():
    global _session
    if _session is None:
        import aioboto3

        _session = aioboto3.Session()
    settings = _require_settings()
    kwargs = {
        "service_name": "s3",
        "aws_access_key_id": settings.s3_access_key_id,
        "aws_secret_access_key": settings.s3_secret_access_key,
        "region_name": S3_REGION,
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return _session.client(**kwargs)


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]+", "_", value.strip())[:120] or "vma"


def _safe_filename(value: str | None) -> str:
    """Strip anything that could climb out of the key's directory."""
    candidate = (value or "object").split("/")[-1].strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", candidate)[:180] or "object"
