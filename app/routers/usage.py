from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_organization, require_api_access
from app.db.engine import get_session
from app.db.queries import governance as governance_q
from app.models.usage import (
    UsageListQuery,
    UsagePageResponse,
    usage_entry_to_response,
)
from app.organization import CurrentOrganization

router = APIRouter(
    prefix="/v1/usage",
    tags=["usage"],
    dependencies=[Depends(require_api_access)],
)


@router.get("", response_model=UsagePageResponse)
async def list_usage(
    limit: int = Query(default=50, ge=1, le=100),
    page: str | None = None,
    session_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Only return usage facts attributed to this Session.",
    ),
    metric: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Only return usage facts for this exact metric.",
    ),
    occurred_at_gt: datetime | None = Query(default=None, alias="occurred_at[gt]"),
    occurred_at_gte: datetime | None = Query(default=None, alias="occurred_at[gte]"),
    occurred_at_lt: datetime | None = Query(default=None, alias="occurred_at[lt]"),
    occurred_at_lte: datetime | None = Query(default=None, alias="occurred_at[lte]"),
    organization: CurrentOrganization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_session),
) -> UsagePageResponse:
    """List append-only raw usage facts visible to the current Organization.

    Results contain recorded quantities and provider-reported token dimensions.
    They do not infer downstream identities, prices, or monetary costs.
    """

    query = UsageListQuery(
        limit=limit,
        page=page,
        session_id=session_id,
        metric=metric,
        occurred_at_gt=occurred_at_gt,
        occurred_at_gte=occurred_at_gte,
        occurred_at_lt=occurred_at_lt,
        occurred_at_lte=occurred_at_lte,
    )
    try:
        result = await governance_q.list_usage_entries_page(
            db,
            organization_id=organization.id,
            limit=query.limit,
            page=query.page,
            source_type="session" if query.session_id is not None else None,
            source_id=query.session_id,
            metric=query.metric,
            occurred_at_gt=query.occurred_at_gt,
            occurred_at_gte=query.occurred_at_gte,
            occurred_at_lt=query.occurred_at_lt,
            occurred_at_lte=query.occurred_at_lte,
        )
    except governance_q.UsagePageCursorError as exc:
        raise HTTPException(status_code=400, detail="Invalid usage page cursor") from exc

    entries = [usage_entry_to_response(entry) for entry in result.entries]
    return UsagePageResponse(
        data=entries,
        has_more=result.next_page is not None,
        first_id=entries[0].id if entries else None,
        last_id=entries[-1].id if entries else None,
        next_page=result.next_page,
    )
