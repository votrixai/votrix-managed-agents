from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.db.queries.api_keys import DEFAULT_API_KEY_SCOPES
from app.models.common import ApiModel

ApiKeyScope = Literal["api", "api_keys:manage", "worker"]


class ApiKeyCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: list(DEFAULT_API_KEY_SCOPES),
        min_length=1,
        description="Permissions granted to this key; worker access is separate from ordinary API access.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="RFC 3339 expiration timestamp; omit for a key without automatic expiration.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, value: list[ApiKeyScope]) -> list[ApiKeyScope]:
        if len(value) != len(set(value)):
            raise ValueError("scopes must not contain duplicates")
        return value


class ApiKeyRevokeRequest(ApiModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Optional audit reason recorded with the revocation.",
    )


class ApiKeyRotateRequest(ApiModel):
    expires_at: datetime | None = Field(
        default=None,
        description="Replacement expiration timestamp; omit to preserve the current expiration.",
    )
    reason: str | None = Field(
        default="rotated",
        max_length=500,
        description="Audit reason recorded when the old key is revoked.",
    )


class ApiKeyResponse(ApiModel):
    id: str
    type: Literal["api_key"] = "api_key"
    organization_id: str = Field(description="Organization that owns this API key.")
    name: str
    prefix: str = Field(description="Non-secret prefix used to identify the key safely.")
    scopes: list[ApiKeyScope] = Field(description="Permissions granted to this API key.")
    expires_at: datetime | None = Field(description="Expiration timestamp, or null when the key does not expire.")
    created_by: str | None = Field(description="Identifier of the actor that created this key.")
    metadata: dict[str, Any]
    last_used_at: datetime | None = Field(description="Timestamp of the most recent successful authentication.")
    revoked_at: datetime | None = Field(description="Timestamp when the key was revoked, or null while active.")
    revoked_by: str | None = Field(description="Identifier of the actor that revoked this key.")
    revocation_reason: str | None = Field(description="Audit reason recorded for the revocation.")
    replaced_by_key_id: str | None = Field(description="Replacement key identifier when this key was rotated.")
    replaces_key_id: str | None = Field(description="Previous key identifier replaced by this key.")
    created_at: datetime
    updated_at: datetime


class ApiKeyCreatedResponse(ApiKeyResponse):
    secret: str = Field(
        description="Plaintext API key. Returned exactly once and never stored by VMA.",
        json_schema_extra={"readOnly": True, "x-sensitive": True},
    )


def api_key_to_response(api_key) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=api_key.id,
        organization_id=api_key.organization_id,
        name=api_key.name,
        prefix=api_key.prefix,
        scopes=list(api_key.scopes or []),
        expires_at=api_key.expires_at,
        created_by=api_key.created_by,
        metadata=dict(api_key.metadata_ or {}),
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
        revoked_by=api_key.revoked_by,
        revocation_reason=api_key.revocation_reason,
        replaced_by_key_id=api_key.replaced_by_key_id,
        replaces_key_id=api_key.replaces_key_id,
        created_at=api_key.created_at,
        updated_at=api_key.updated_at,
    )


def api_key_to_created_response(api_key, secret: str) -> ApiKeyCreatedResponse:
    return ApiKeyCreatedResponse(**api_key_to_response(api_key).model_dump(), secret=secret)
