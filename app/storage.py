"""S3-compatible object storage.

Relational state lives in Postgres/SQLite. Object bytes live in S3-compatible
storage. Cloudflare R2 and similar providers are configured through S3_*.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.ids import new_id
from app.organization import resolve_organization_id
_session: Any | None = None
_lock = asyncio.Lock()


@dataclass(frozen=True)
class StoredObject:
    backend: str
    key: str
    content_type: str
    size_bytes: int
    sha256: str


class StorageConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectStorageSettings:
    backend: str
    bucket_name: str
    access_key_id: str
    secret_access_key: str
    endpoint_url: str | None
    region: str


OBJECT_STORAGE_BACKENDS = {"s3"}


def object_storage_configured() -> bool:
    return _object_storage_settings() is not None


def object_storage_backend_label() -> str:
    return _require_object_storage().backend


def is_object_storage_backend(value: str | None) -> bool:
    return bool(value and value.lower() in OBJECT_STORAGE_BACKENDS)


def should_store_in_object_storage() -> bool:
    _require_object_storage()
    return True


def _object_storage_settings() -> ObjectStorageSettings | None:
    s = get_settings()

    if all([s.s3_access_key_id, s.s3_secret_access_key, s.s3_bucket_name]):
        return ObjectStorageSettings(
            backend="s3",
            bucket_name=s.s3_bucket_name,
            access_key_id=s.s3_access_key_id,
            secret_access_key=s.s3_secret_access_key,
            endpoint_url=s.s3_endpoint_url or None,
            region=s.s3_region or "auto",
        )

    return None


def _require_object_storage() -> ObjectStorageSettings:
    config = _object_storage_settings()
    if config is None:
        raise StorageConfigurationError(
            "Private S3-compatible object storage requires S3_ACCESS_KEY_ID, "
            "S3_SECRET_ACCESS_KEY, and S3_BUCKET_NAME"
        )
    return config


def object_key(
    *,
    namespace: str,
    category: str,
    filename: str,
    content_sha256: str | None = None,
    organization_id: str,
) -> str:
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    safe_organization = _organization_path_part(organization_id)
    safe_namespace = _safe_path_part(namespace or "vma")
    safe_category = _safe_path_part(category or "general")
    safe_filename = _safe_filename(filename)
    unique = content_sha256[:16] if content_sha256 else new_id("obj")
    return f"organizations/{safe_organization}/{safe_namespace}/{safe_category}/{date_str}/{unique}_{safe_filename}"


async def save_file_bytes(
    data: bytes,
    mime_type: str | None,
    *,
    namespace: str,
    filename: str,
    category: str = "general",
    organization_id: str,
) -> StoredObject:
    """Upload bytes to object storage and return object metadata."""
    config = _require_object_storage()
    content_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    sha256 = hashlib.sha256(data).hexdigest()
    key = object_key(
        namespace=namespace,
        category=category,
        filename=filename,
        content_sha256=sha256,
        organization_id=organization_id,
    )
    async with _get_session().client(**_client_kwargs()) as client:
        await client.put_object(
            Bucket=config.bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    return StoredObject(
        backend=config.backend,
        key=key,
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256,
    )


async def download_file(key: str) -> bytes:
    data, _content_type = await download_file_with_type(key)
    return data


async def download_file_with_type(key: str) -> tuple[bytes, str | None]:
    config = _require_object_storage()
    async with _get_session().client(**_client_kwargs()) as client:
        resp = await client.get_object(Bucket=config.bucket_name, Key=key)
        data = await resp["Body"].read()
        return data, resp.get("ContentType")


async def create_presigned_upload_url(
    key: str,
    mime_type: str,
    *,
    expires_in: int = 900,
) -> str:
    config = _require_object_storage()
    async with _get_session().client(**_client_kwargs()) as client:
        return await client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": config.bucket_name,
                "Key": key,
                "ContentType": mime_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )


async def create_presigned_download_url(
    key: str,
    *,
    expires_in: int = 300,
) -> str:
    config = _require_object_storage()
    async with _get_session().client(**_client_kwargs()) as client:
        return await client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": config.bucket_name,
                "Key": key,
            },
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )


async def get_file_info(key: str) -> dict[str, Any]:
    config = _require_object_storage()
    async with _get_session().client(**_client_kwargs()) as client:
        return await client.head_object(Bucket=config.bucket_name, Key=key)


async def copy_file(
    source_key: str,
    destination_key: str,
    *,
    content_type: str | None = None,
) -> None:
    config = _require_object_storage()
    params: dict[str, Any] = {
        "Bucket": config.bucket_name,
        "Key": destination_key,
        "CopySource": {"Bucket": config.bucket_name, "Key": source_key},
    }
    if content_type:
        params["ContentType"] = content_type
        params["MetadataDirective"] = "REPLACE"
    async with _get_session().client(**_client_kwargs()) as client:
        await client.copy_object(**params)


async def delete_file(key: str) -> None:
    config = _require_object_storage()
    async with _get_session().client(**_client_kwargs()) as client:
        await client.delete_object(Bucket=config.bucket_name, Key=key)


def _get_session() -> Any:
    global _session
    if _session is None:
        import aioboto3

        _session = aioboto3.Session()
    return _session


def _client_kwargs() -> dict[str, str]:
    config = _require_object_storage()
    kwargs = {
        "service_name": "s3",
        "aws_access_key_id": config.access_key_id,
        "aws_secret_access_key": config.secret_access_key,
        "region_name": config.region,
    }
    if config.endpoint_url:
        kwargs["endpoint_url"] = config.endpoint_url
    return kwargs


def _safe_filename(value: str | None) -> str:
    candidate = (value or "object").split("/")[-1].strip()
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate)
    return candidate[:180] or "object"


def _safe_path_part(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._=-]+", "_", value.strip())
    return candidate[:120] or "vma"


def _organization_path_part(value: str) -> str:
    return resolve_organization_id(value)
