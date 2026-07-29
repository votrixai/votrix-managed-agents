from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every request and response shape. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ListResponse(ApiModel, Generic[T]):
    """One page, and how to ask for the next.

    Feed `last_id` back as `after_id` to go forward, `first_id` as `before_id`
    to go back. The ids are the caller's only handle on where it is: they come
    from here rather than being constructed, which is what lets the cursor stay
    valid while the table is written to underneath it.
    """

    data: list[T]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


def page_of(page, to_model) -> ListResponse:
    """Wrap a query page, converting each row on the way out.

    The cursor ids come off the rows themselves rather than off whatever they
    were converted into, so a response model is free to omit `id` or rename it
    without silently breaking paging.
    """
    return ListResponse(
        data=[to_model(row) for row in page.items],
        has_more=page.has_more,
        first_id=page.first_id,
        last_id=page.last_id,
    )


class DeletedResponse(ApiModel):
    id: str
    deleted: bool = True


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
