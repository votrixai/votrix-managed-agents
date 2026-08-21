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
    ByokFundingResponse,
    ByokModelCredentialResponse,
    ByokModelCredentialSetRequest,
    DirectAccountBackend,
    ObservedTokenUsage,
    PlatformFundingResponse,
)
from app.routers.deps import Db, OrganizationId
from app.services import accounts as service
from app.services.account_credentials import SubmittedByokCredential

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Create a Platform or BYOK Account with its own inference key.

    Spend on this Account is measured and capped separately from every other
    one, because a separate credential is what the provider enforces the
    boundary with: a request either carries this Account's key or it does not.

    Platform funding mints an isolated OpenRouter key. BYOK validates and
    encrypts one key per supplied direct backend. Either comes back `active`
    and ready to be spent through.
    """
    account = await service.create_account(
        db,
        organization_id=organization_id,
        name=body.name,
        limit_usd=body.limit_usd,
        idempotency_key=body.idempotency_key,
        funding_mode=body.funding.type,
        byok_credentials=(
            tuple(
                SubmittedByokCredential(
                    backend=credential.backend,
                    api_key=credential.api_key,
                )
                for credential in body.funding.credentials
            )
            if body.funding.type == "byok"
            else None
        ),
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


@router.put(
    "/{account_id}/credentials/{backend}",
    response_model=AccountResponse,
)
async def set_byok_model_credential(
    account_id: str,
    backend: DirectAccountBackend,
    body: ByokModelCredentialSetRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Add or replace one direct-provider key on a BYOK Account.

    The new key is validated before the stored credential changes. Repeating
    the same request is idempotent. Platform Accounts reject this operation.
    """

    account = await service.set_byok_model_credential(
        db,
        organization_id=organization_id,
        account_id=account_id,
        backend=backend,
        api_key=body.api_key,
    )
    return to_account(account)


@router.delete(
    "/{account_id}/credentials/{backend}",
    response_model=AccountResponse,
)
async def delete_byok_model_credential(
    account_id: str,
    backend: DirectAccountBackend,
    db: Db,
    organization_id: OrganizationId,
):
    """Remove one direct-provider key from a BYOK Account.

    At least one key must remain. Platform keys are VMA-managed and cannot be
    changed through this endpoint.
    """

    account = await service.delete_byok_model_credential(
        db,
        organization_id=organization_id,
        account_id=account_id,
        backend=backend,
    )
    return to_account(account)


@router.get("/{account_id}/usage", response_model=AccountUsageResponse)
async def retrieve_account_usage(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """Billing and normalized token usage for this Account.

    Platform USD figures are read live from its managed OpenRouter key.
    ``observed_usage`` is built from model calls recorded on Sessions pinned to
    this Account. BYOK returns null USD figures rather than inventing a price.

    Answered for a suspended Account too. What one spent has to stay readable,
    which is most of the reason Accounts are suspended rather than removed.
    """
    usage = await service.get_account_usage(
        db, organization_id=organization_id, account_id=account_id
    )
    return AccountUsageResponse(
        account_id=account_id,
        funding=(
            PlatformFundingResponse()
            if usage.funding_mode == "platform"
            else ByokFundingResponse(
                credentials=[
                    ByokModelCredentialResponse(backend=backend)
                    for backend in usage.backends
                ]
            )
        ),
        usage_usd=usage.usage_usd,
        usage_daily_usd=usage.usage_daily_usd,
        usage_weekly_usd=usage.usage_weekly_usd,
        usage_monthly_usd=usage.usage_monthly_usd,
        limit_usd=usage.limit_usd,
        limit_remaining_usd=usage.limit_remaining_usd,
        observed_usage=ObservedTokenUsage(
            input_tokens=usage.observed_input_tokens,
            output_tokens=usage.observed_output_tokens,
            total_tokens=usage.observed_total_tokens,
        ),
    )


@router.post("/{account_id}/suspend", response_model=AccountResponse)
async def suspend_account(
    account_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """Stop this Account spending, without giving up what it spent.

    A managed Platform credential is disabled at OpenRouter, not removed. A
    BYOK credential stays user-owned and is blocked locally. Either way the
    Account keeps its id and usage, and `resume` reuses the same credential.

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
        funding=(
            PlatformFundingResponse()
            if account.funding_mode == "platform"
            else ByokFundingResponse(
                credentials=[
                    ByokModelCredentialResponse(backend=credential.backend)
                    for credential in account.model_credentials
                ]
            )
        ),
    )
