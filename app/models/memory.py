"""Public Memory Store request and response shapes."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.models.common import ApiModel


def _validate_name(value: str | None) -> str | None:
    if value is None:
        return value
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("name cannot contain control characters")
    return value


def _validate_metadata(
    value: dict[str, str | None] | None,
) -> dict[str, str | None] | None:
    if value is None:
        return value
    if len(value) > 16:
        raise ValueError("metadata may contain at most 16 keys")
    for key, item in value.items():
        if not 1 <= len(key) <= 64:
            raise ValueError("metadata keys must contain 1 to 64 characters")
        if item is not None and len(item) > 512:
            raise ValueError("metadata values may contain at most 512 characters")
    return value


class MemoryStoreCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1024)
    metadata: dict[str, str] = Field(default_factory=dict)

    _name_has_no_controls = field_validator("name")(_validate_name)
    _metadata_is_bounded = field_validator("metadata")(_validate_metadata)


class MemoryStoreUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    # A patch: null removes one key, a string upserts it.
    metadata: dict[str, str | None] | None = None

    _name_has_no_controls = field_validator("name")(_validate_name)
    _metadata_is_bounded = field_validator("metadata")(_validate_metadata)

    @model_validator(mode="after")
    def _name_is_not_explicitly_null(self) -> "MemoryStoreUpdateRequest":
        # Omitted means unchanged. ``null`` cannot mean that as well because
        # it would otherwise reach the query layer and become the string
        # ``"None"`` while satisfying the database's non-null constraint.
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class MemoryStoreResponse(ApiModel):
    id: str
    type: Literal["memory_store"] = "memory_store"
    name: str
    description: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class MemoryStoreListResponse(ApiModel):
    data: list[MemoryStoreResponse]
    next_page: str | None = None
    # Existing VMA cursor fields remain additive on this CMA-specific route.
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class DeletedMemoryStoreResponse(ApiModel):
    id: str
    type: Literal["memory_store_deleted"] = "memory_store_deleted"


MAX_MEMORY_PATH_BYTES = 1024
MAX_MEMORY_CONTENT_BYTES = 100 * 1024
MAX_MEMORIES_PER_STORE = 2000


def normalize_memory_path(value: str) -> str:
    """Validate the exact slash-prefixed path used by the CMA API."""
    if not isinstance(value, str):
        raise ValueError("Memory path must be a string")
    if not value.startswith("/"):
        raise ValueError("Memory path must start with '/'")
    if len(value.encode("utf-8")) > MAX_MEMORY_PATH_BYTES:
        raise ValueError("Memory path must be at most 1024 bytes")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("Memory path must be NFC-normalized")

    parts = value.split("/")[1:]
    if not parts or parts == [""]:
        raise ValueError("Memory path must contain at least one segment")
    if any(part == "" for part in parts):
        raise ValueError("Memory path must not contain empty segments")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Memory path must not contain '.' or '..' segments")
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for part in parts
        for character in part
    ):
        raise ValueError("Memory path must not contain control or format characters")
    return value


def normalize_memory_path_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("path_prefix must be a string")
    if value == "/":
        return value
    if not value.endswith("/"):
        raise ValueError("path_prefix must end with '/'")
    normalize_memory_path(value[:-1])
    return value


class MemoryCreateRequest(ApiModel):
    path: str
    # CMA's generated SDK types this nullable, while its prose says to pass an
    # explicit empty string. Accepting null as empty keeps both clients usable.
    content: str | None

    _path_is_valid = field_validator("path")(normalize_memory_path)


class MemoryPrecondition(ApiModel):
    type: Literal["content_sha256"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoryUpdateRequest(ApiModel):
    content: str | None = None
    path: str | None = None
    precondition: MemoryPrecondition | None = None

    @field_validator("path")
    @classmethod
    def _path_is_valid(cls, value: str | None) -> str | None:
        return normalize_memory_path(value) if value is not None else None


class ApiActor(ApiModel):
    type: Literal["api_actor"] = "api_actor"
    api_key_id: str


class SessionActor(ApiModel):
    type: Literal["session_actor"] = "session_actor"
    session_id: str


class UserActor(ApiModel):
    type: Literal["user_actor"] = "user_actor"
    user_id: str


MemoryActor = Annotated[ApiActor | SessionActor | UserActor, Field(discriminator="type")]


class MemoryResponse(ApiModel):
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


class MemoryPrefixResponse(ApiModel):
    type: Literal["memory_prefix"] = "memory_prefix"
    path: str


MemoryListItem = Annotated[
    MemoryResponse | MemoryPrefixResponse,
    Field(discriminator="type"),
]


class MemoryListResponse(ApiModel):
    data: list[MemoryListItem]
    next_page: str | None = None
    # Additive compatibility with the rest of VMA's cursor responses.
    has_more: bool = False


class DeletedMemoryResponse(ApiModel):
    id: str
    type: Literal["memory_deleted"] = "memory_deleted"


class MemoryVersionResponse(ApiModel):
    id: str
    type: Literal["memory_version"] = "memory_version"
    memory_store_id: str
    memory_id: str
    operation: Literal["created", "modified", "deleted"]
    created_at: datetime
    content: str | None = None
    content_sha256: str | None = None
    content_size_bytes: int | None = None
    path: str | None = None
    created_by: MemoryActor | None = None
    redacted_at: datetime | None = None
    redacted_by: MemoryActor | None = None


class MemoryVersionListResponse(ApiModel):
    data: list[MemoryVersionResponse]
    next_page: str | None = None
    has_more: bool = False
