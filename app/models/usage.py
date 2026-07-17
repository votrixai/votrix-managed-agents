from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from app.models.common import ApiModel


class UsageListQuery(ApiModel):
    limit: int = Field(default=50, ge=1, le=100)
    page: str | None = None
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Return usage facts whose source is this Session.",
    )
    metric: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Return usage facts for one exact metric.",
    )
    occurred_at_gt: datetime | None = Field(default=None, alias="occurred_at[gt]")
    occurred_at_gte: datetime | None = Field(default=None, alias="occurred_at[gte]")
    occurred_at_lt: datetime | None = Field(default=None, alias="occurred_at[lt]")
    occurred_at_lte: datetime | None = Field(default=None, alias="occurred_at[lte]")


class UsageEntryResponse(ApiModel):
    id: str
    type: Literal["usage"] = "usage"
    organization_id: str
    metric: str
    quantity: int
    unit: str
    provider: str | None = None
    model: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class UsagePageResponse(ApiModel):
    data: list[UsageEntryResponse]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None
    next_page: str | None = None


def usage_entry_to_response(entry) -> UsageEntryResponse:
    return UsageEntryResponse(
        id=entry.id,
        organization_id=entry.organization_id,
        metric=entry.metric,
        quantity=entry.quantity,
        unit=entry.unit,
        provider=entry.provider,
        model=entry.model,
        source_type=entry.source_type,
        source_id=entry.source_id,
        dimensions=dict(entry.dimensions or {}),
        data=dict(entry.data or {}),
        occurred_at=_as_utc(entry.occurred_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
