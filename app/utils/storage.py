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
from typing import Any, AsyncIterator
from urllib.parse import quote

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


class ObjectTooLarge(RuntimeError):
    """More bytes arrived than the caller said it would accept."""


# Big enough that a 100 MB object is a dozen parts rather than a hundred round
# trips, small enough that this is what the process holds at any moment. S3
# requires every part but the last to be at least 5 MiB.
_PART_BYTES = 8 * 1024 * 1024


async def save_stream(
    chunks: AsyncIterator[bytes],
    *,
    organization_id: str,
    category: str,
    filename: str,
    mime_type: str | None = None,
    max_bytes: int | None = None,
) -> StoredObject:
    """Store bytes that arrive a piece at a time, never holding them all.

    ``save_bytes`` needs the whole object in memory — twice over, since hashing
    and sending both read it — which is what a buffered request body already
    costs anyway. Bytes pulled from somewhere else have no such excuse, and a
    100 MB import held in memory on a 4 GiB instance serving eighty other
    requests is how one upload becomes everyone's problem.

    The key cannot carry a content fingerprint here: the hash is only known
    after the last chunk, and the key has to exist before the first one goes
    out. ``object_key`` already allows for that — it falls back to a random
    segment — so an imported object is addressed by identity rather than by
    content, and two imports of identical bytes get two objects.

    A failure aborts the multipart upload rather than leaving its parts to be
    billed forever.
    """

    settings = _require_settings()
    content_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    key = object_key(
        organization_id=organization_id,
        category=category,
        filename=filename,
    )

    digest = hashlib.sha256()
    parts: list[dict[str, Any]] = []
    buffer = bytearray()
    total = 0

    async with _client() as client:
        created = await client.create_multipart_upload(
            Bucket=settings.s3_bucket_name, Key=key, ContentType=content_type
        )
        upload_id = created["UploadId"]

        async def send(body: bytes) -> None:
            number = len(parts) + 1
            part = await client.upload_part(
                Bucket=settings.s3_bucket_name,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=body,
            )
            parts.append({"ETag": part["ETag"], "PartNumber": number})

        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ObjectTooLarge(
                        f"the source sent more than {max_bytes} bytes"
                    )
                digest.update(chunk)
                buffer.extend(chunk)
                while len(buffer) >= _PART_BYTES:
                    await send(bytes(buffer[:_PART_BYTES]))
                    del buffer[:_PART_BYTES]

            # The tail, which may be under the 5 MiB floor — allowed for the
            # last part only. An empty source still gets one empty part, so it
            # ends up as a real zero-byte object rather than a failed upload.
            if buffer or not parts:
                await send(bytes(buffer))

            await client.complete_multipart_upload(
                Bucket=settings.s3_bucket_name,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            await client.abort_multipart_upload(
                Bucket=settings.s3_bucket_name, Key=key, UploadId=upload_id
            )
            raise

    return StoredObject(
        key=key,
        content_type=content_type,
        size_bytes=total,
        sha256=digest.hexdigest(),
    )


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


async def presigned_download_url(
    key: str,
    *,
    expires_in: int = 300,
    filename: str | None = None,
    inline: bool = False,
) -> str:
    """A short-lived read URL — how a sandbox fetches its own skills.

    The sandbox gets one URL for one object, never a standing credential.

    `filename` names the file in the response. Without it a browser following
    this URL saves the object under the tail of its key, and a key deliberately
    carries a content fingerprint in front of the name — see `object_key`. The
    name is signed in rather than appended, so nobody can rename an object by
    editing the URL they were given.
    """
    settings = _require_settings()
    params: dict[str, str] = {"Bucket": settings.s3_bucket_name, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = _content_disposition(
            "inline" if inline else "attachment", filename
        )
    async with _client() as client:
        return await client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )


def _content_disposition(disposition: str, filename: str) -> str:
    """Name a file in a header, for names a header cannot spell.

    HTTP headers are latin-1 and an agent writes files called whatever the work
    was in, so the name goes out twice per RFC 6266: a plain `filename` reduced
    to characters latin-1 can hold, and `filename*` carrying the real one
    percent-encoded. Every current browser prefers the second; anything that
    does not still gets something with the right extension.

    Quotes and backslashes are replaced rather than escaped — they only ever
    arrive from a name that was already strange, and a stray quote would end
    the header value early.
    """
    fallback = "".join(
        char if char.isascii() and char.isprintable() and char not in '"\\' else "_"
        for char in filename
    )
    return f"{disposition}; filename=\"{fallback or 'file'}\"; filename*=UTF-8''{quote(filename)}"


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
