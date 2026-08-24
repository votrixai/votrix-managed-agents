"""`/v1/accounts` — the billing boundaries an Organization spends through.

There is no delete. An Account carries the record of what it was charged, and
that record has to outlive whatever the Account was for, so the way one stops
being usable is `suspend`.
"""

from fastapi import APIRouter, status

from app.db.models import OrganizationAccount
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import ListResponse, page_of
from app.models.accounts import (
    AccountCreateRequest,
    AccountResponse,
    AccountUsageResponse,
    AccountUsageSummary,
    OrganizationUsageResponse,
)
from app.routers.deps import Db, OrganizationId
from app.services import accounts as service

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Create an Account with its own provider credential.

    Spend on this Account is measured and capped separately from every other
    one, because a separate credential is what the provider enforces the
    boundary with: a request either carries this Account's key or it does not.

    The credential is minted during this call, so the Account comes back
    `active` and ready to be spent through.
    """
    account = await service.create_account(
        db,
        organization_id=organization_id,
        name=body.name,
        limit_usd=body.limit_usd,
        idempotency_key=body.idempotency_key,
    )
    return to_account(account)


@router.get("", response_model=ListResponse[AccountResponse])
async def list_accounts(
    db: Db,
    organization_id: OrganizationId,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    """List this Organization's Accounts, oldest first."""
    found = await service.list_accounts(
        db,
        organization_id=organization_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(found, to_account)


@router.get("/usage", response_model=OrganizationUsageResponse)
async def retrieve_organization_usage(
    db: Db,
    organization_id: OrganizationId,
):
    """Provider-authoritative usage across every Account in this Organization."""
    usage = await service.get_organization_usage(
        db,
        organization_id=organization_id,
    )
    return OrganizationUsageResponse(
        organization_id=organization_id,
        usage_usd=usage.usage_usd,
        usage_daily_usd=usage.usage_daily_usd,
        usage_weekly_usd=usage.usage_weekly_usd,
        usage_monthly_usd=usage.usage_monthly_usd,
        as_of=usage.as_of,
        accounts=[
            AccountUsageSummary(
                account_id=row.account_id,
                name=row.name,
                status=row.status,
                is_default=row.is_default,
                usage_usd=row.usage_usd,
                usage_daily_usd=row.usage_daily_usd,
                usage_weekly_usd=row.usage_weekly_usd,
                usage_monthly_usd=row.usage_monthly_usd,
            )
            for row in usage.accounts
        ],
    )


@router.get("/{account_id}", response_model=AccountResponse)
async def retrieve_account(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    account = await service.get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    return to_account(account)


@router.get("/{account_id}/usage", response_model=AccountUsageResponse)
async def retrieve_account_usage(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """What this Account has spent, in USD.

    Read live from the provider that charges it, so the answer includes every
    call made on this Account's credential rather than only the ones this
    platform recorded. It is current as of the request; there is no settlement
    delay to wait out.

    Answered for a suspended Account too. What one spent has to stay readable,
    which is most of the reason Accounts are suspended rather than removed.
    """
    usage = await service.get_account_usage(
        db, organization_id=organization_id, account_id=account_id
    )
    return AccountUsageResponse(
        account_id=account_id,
        usage_usd=usage.usage_usd,
        usage_daily_usd=usage.usage_daily_usd,
        usage_weekly_usd=usage.usage_weekly_usd,
        usage_monthly_usd=usage.usage_monthly_usd,
        limit_usd=usage.limit_usd,
        limit_remaining_usd=usage.limit_remaining_usd,
    )


@router.post("/{account_id}/suspend", response_model=AccountResponse)
async def suspend_account(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """Stop this Account spending, without giving up what it spent.

    The credential is disabled at the provider, not removed, so the Account
    keeps its id, its limit, and everything recorded against it — and `resume`
    turns the same credential back on.

    Sessions already running on this Account fail their next model call. That
    is the point: a suspension that let existing work finish would not be one.

    The Organization's default Account is refused. A request naming no Account
    resolves to it, so suspending it stops the whole Organization while reading
    as one Account's business.
    """
    account = await service.suspend_account(
        db, organization_id=organization_id, account_id=account_id
    )
    return to_account(account)


@router.post("/{account_id}/resume", response_model=AccountResponse)
async def resume_account(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """Let a suspended Account spend again, on the credential it already had."""
    account = await service.resume_account(
        db, organization_id=organization_id, account_id=account_id
    )
    return to_account(account)


def to_account(account: OrganizationAccount) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        organization_id=account.organization_id,
        name=account.name,
        status=account.status,
        # Stored as NULL for a non-default so one Organization can only have
        # one; that is a storage detail, and the API says true or false.
        is_default=bool(account.is_default),
        limit_usd=account.limit_usd,
    )
