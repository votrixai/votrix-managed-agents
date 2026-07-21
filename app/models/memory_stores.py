from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import ApiModel


class MemoryStoreCreateRequest(ApiModel):
    name: str
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryStoreUpdateRequest(ApiModel):
    name: str | None = None
    description: str | None = None
    metadata: dict[str, str | None] | None = None


class MemoryStoreResponse(ApiModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    name: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    description: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryStoreDeletedResponse(ApiModel):
    id: str
    type: Literal["memory_store_deleted"] = "memory_store_deleted"
    deleted: Literal[True] = True


class MemoryCreateRequest(ApiModel):
    path: str | list[str]
    content: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    session_id: str | None = None


class MemoryPrecondition(ApiModel):
    type: Literal["content_sha256"]
    content_sha256: str | None = None


class MemoryUpdateRequest(ApiModel):
    path: str | list[str] | None = None
    content: str | None = None
    precondition: MemoryPrecondition | None = None
    if_version: int | None = None
    expected_version: int | None = None
    actor: str | None = None
    updated_by: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] | None = None


class MemoryResponse(ApiModel):
    id: str
    type: Literal["memory"] = "memory"
    memory_store_id: str = Field(description="Memory Store that owns this record.")
    memory_version_id: str = Field(description="Current immutable Memory Version ID.")
    path: str
    path_key: str = Field(description="Normalized path without the leading slash.")
    content: str | None = None
    content_sha256: str
    content_size_bytes: int = Field(description="UTF-8 content size in bytes.")
    version: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = Field(default=None, description="Actor that created the Memory.")
    updated_by: str | None = None
    session_id: str | None = None
    redacted: bool = Field(default=False, description="Whether content was redacted.")
    redacted_at: datetime | None = Field(default=None, description="When content was redacted.")
    created_at: datetime
    updated_at: datetime


class MemoryDeletedResponse(ApiModel):
    id: str
    type: Literal["memory_deleted"] = "memory_deleted"
    deleted: Literal[True] = True


class MemoryPrefixResponse(ApiModel):
    type: Literal["memory_prefix"] = "memory_prefix"
    path: str


class MemoryVersionResponse(ApiModel):
    id: str
    type: Literal["memory_version"] = "memory_version"
    memory_store_id: str = Field(description="Memory Store that owns this version.")
    memory_id: str = Field(description="Memory record captured by this version.")
    version: int
    memory_version: int | None = Field(default=None, description="Numeric Memory version.")
    operation: Literal["created", "modified", "deleted"] = Field(
        description="Mutation recorded by this immutable version."
    )
    created_at: datetime
    created_by: dict[str, Any] | None = Field(default=None, description="Actor that created this version.")
    content: str | None = None
    content_sha256: str | None = None
    content_size_bytes: int | None = Field(default=None, description="UTF-8 content size in bytes.")
    path: str | None = None
    redacted: bool = Field(default=False, description="Whether this version was redacted.")
    redacted_at: datetime | None = Field(default=None, description="When this version was redacted.")
    redacted_by: dict[str, Any] | None = Field(default=None, description="Actor that redacted this version.")
    updated_at: datetime
