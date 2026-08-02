from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


class OrganizationCreateRequest(ApiModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)


class OrganizationUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class OrganizationResponse(ApiModel):
    id: str
    type: Literal["organization"] = "organization"
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class MemberAddRequest(ApiModel):
    user_id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    role: Literal["owner", "admin", "member"] = "member"


class MemberResponse(ApiModel):
    id: str
    type: Literal["organization_member"] = "organization_member"
    organization_id: str
    user_id: str
    email: str | None = None
    role: Literal["owner", "admin", "member"]
    created_at: datetime
