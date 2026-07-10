import base64
from typing import Any

from app.db.models import ManagedResource
from app.models.common import FlexibleApiModel


class GenericBody(FlexibleApiModel):
    pass


def resource_to_response(resource: ManagedResource, *, public_type: str | None = None) -> dict[str, Any]:
    data = _redacted_resource_data(resource)
    data.update(
        {
            "id": resource.id,
            "type": public_type or resource.resource_type,
            "created_at": resource.created_at,
            "updated_at": resource.updated_at,
            "archived_at": resource.archived_at,
        }
    )
    if resource.version is not None:
        data.setdefault("version", resource.version)
    if resource.name is not None:
        data.setdefault("name", resource.name)
    if resource.status:
        data.setdefault("status", resource.status)
    if resource.deleted_at is not None:
        data["deleted_at"] = resource.deleted_at
    if resource.filename is not None:
        data.setdefault("filename", resource.filename)
    if resource.content_type is not None:
        data.setdefault("mime_type", resource.content_type)
    if resource.size_bytes is not None:
        data.setdefault("size_bytes", resource.size_bytes)
    elif resource.content is not None:
        data.setdefault("size_bytes", len(resource.content))
    if resource.sha256 is not None:
        data.setdefault("sha256", resource.sha256)
    if resource.storage_backend is not None:
        data.setdefault(
            "storage",
            {
                "backend": resource.storage_backend,
                "key": resource.storage_key,
                "url": resource.storage_url,
            },
        )
    return data


def deleted_response(resource: ManagedResource, *, public_type: str | None = None) -> dict[str, Any]:
    return {
        "id": resource.id,
        "type": public_type or f"deleted_{resource.resource_type}",
        "deleted": True,
    }


def encode_content(content: bytes | None) -> str | None:
    if content is None:
        return None
    return base64.b64encode(content).decode("ascii")


SECRET_KEY_PARTS = ("secret", "token", "api_key", "apikey", "password", "private_key", "client_secret")


def _redacted_resource_data(resource: ManagedResource) -> dict[str, Any]:
    data = dict(resource.data or {})
    if resource.resource_type in {"credential", "vault", "user_profile"}:
        return _redact_secret_values(data)
    return data


def _redact_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if _looks_secret_key(key):
                redacted[key] = "redacted"
            else:
                redacted[key] = _redact_secret_values(child)
        return redacted
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    return value


def _looks_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SECRET_KEY_PARTS)
