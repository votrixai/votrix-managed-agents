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


class MemoryStoreFileResponse(ApiModel):
    """One path-addressed file written into a Memory Store."""

    type: Literal["memory_store_file"] = "memory_store_file"
    memory_store_id: str
    path: str
    size_bytes: int
    sha256: str
