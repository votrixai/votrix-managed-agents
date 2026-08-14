from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.models.common import ApiModel


class OrganizationApiKeyCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class OrganizationApiKeyMetadata(ApiModel):
    organization_id: str
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    can_revoke: bool = False


class OrganizationApiKeyCreated(OrganizationApiKeyMetadata):
    api_key: str


class OrganizationApiKeyListResponse(ApiModel):
    data: list[OrganizationApiKeyMetadata]


class OrganizationApiKeyCreateResponse(ApiModel):
    data: OrganizationApiKeyCreated


class OrganizationApiKeyRevokedResponse(ApiModel):
    id: str
    revoked: bool = True
