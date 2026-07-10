from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import FlexibleApiModel


class MemoryStoreCreateRequest(FlexibleApiModel):
    name: str
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryStoreUpdateRequest(FlexibleApiModel):
    name: str | None = None
    description: str | None = None
    metadata: dict[str, str | None] | None = None


class MemoryStoreResponse(FlexibleApiModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    name: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryCreateRequest(FlexibleApiModel):
    path: str | list[str]
    content: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    session_id: str | None = None


class MemoryPrecondition(FlexibleApiModel):
    type: Literal["content_sha256"]
    content_sha256: str | None = None


class MemoryUpdateRequest(FlexibleApiModel):
    path: str | list[str] | None = None
    content: str | None = None
    precondition: MemoryPrecondition | None = None
    if_version: int | None = None
    expected_version: int | None = None
    actor: str | None = None
    updated_by: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] | None = None


class MemoryResponse(FlexibleApiModel):
    id: str
    type: Literal["memory"] = "memory"
    memory_store_id: str
    memory_version_id: str
    path: str
    content: str | None = None
    content_sha256: str
    content_size_bytes: int
    created_at: datetime
    updated_at: datetime


class MemoryPrefixResponse(FlexibleApiModel):
    type: Literal["memory_prefix"] = "memory_prefix"
    path: str


class MemoryVersionResponse(FlexibleApiModel):
    id: str
    type: Literal["memory_version"] = "memory_version"
    memory_store_id: str
    memory_id: str
    operation: Literal["created", "modified", "deleted"]
    created_at: datetime
    created_by: dict[str, Any] | None = None
    content: str | None = None
    content_sha256: str | None = None
    content_size_bytes: int | None = None
    path: str | None = None
    redacted_at: datetime | str | None = None
    redacted_by: dict[str, Any] | None = None
