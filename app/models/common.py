from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FlexibleApiModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ListResponse(ApiModel, Generic[T]):
    data: list[T]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None
    next_page: str | None = None

    @classmethod
    def from_items(
        cls,
        items: list[T],
        *,
        has_more: bool = False,
        next_page: str | None = None,
    ) -> "ListResponse[T]":
        first_id = _item_id(items[0]) if items else None
        last_id = _item_id(items[-1]) if items else None
        return cls(data=items, has_more=has_more, first_id=first_id, last_id=last_id, next_page=next_page)


class MetadataModel(ApiModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _item_id(item) -> str | None:
    if isinstance(item, dict):
        value = item.get("id")
        return value if isinstance(value, str) else None
    value = getattr(item, "id", None)
    return value if isinstance(value, str) else None
