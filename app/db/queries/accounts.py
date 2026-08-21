"""Reads and writes over billing Accounts.

No delete. An Account carries the record of what it was charged, so removing
one removes that record; suspension is how an Account stops being usable.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.accounts import (
    ACCOUNT_PROVISIONING,
    CREDENTIAL_ACTIVE,
    FUNDING_PLATFORM,
    AccountModelCredential,
    OrganizationAccount,
)
from app.db.models.sessions import Session, SessionEvent
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.models import events as event_types
from app.utils.id_generator import new_id


async def create_account(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    is_default: bool = False,
    limit_usd: Decimal | None = None,
    idempotency_key: str | None = None,
    funding_mode: str = FUNDING_PLATFORM,
) -> OrganizationAccount:
    """Write the Account row, before it has anything to spend through.

    It starts in `provisioning`. Platform creation commits that row before its
    external mint so an interrupted key can be traced; BYOK creation attaches
    the encrypted user key and moves it to active in the same transaction.
    """
    account = OrganizationAccount(
        id=new_id("acct"),
        organization_id=organization_id,
        name=name,
        status=ACCOUNT_PROVISIONING,
        funding_mode=funding_mode,
        # NULL rather than False, so the one-default constraint counts only
        # the real default.
        is_default=True if is_default else None,
        limit_usd=limit_usd,
        idempotency_key=idempotency_key,
    )
    db.add(account)
    await db.flush()
    return account


async def get_account(
    db: AsyncSession, *, organization_id: str, account_id: str
) -> OrganizationAccount | None:
    result = await db.execute(
        select(OrganizationAccount).where(
            OrganizationAccount.id == account_id,
            OrganizationAccount.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def get_account_for_update(
    db: AsyncSession, *, organization_id: str, account_id: str
) -> OrganizationAccount | None:
    """Lock one Account while its credential set is changed.

    Serializing on the parent row makes two concurrent removals re-count the
    credentials in order, so both cannot independently decide they are not
    deleting the last one.
    """

    result = await db.execute(
        select(OrganizationAccount)
        .where(
            OrganizationAccount.id == account_id,
            OrganizationAccount.organization_id == organization_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_by_idempotency_key(
    db: AsyncSession, *, organization_id: str, idempotency_key: str
) -> OrganizationAccount | None:
    result = await db.execute(
        select(OrganizationAccount).where(
            OrganizationAccount.organization_id == organization_id,
            OrganizationAccount.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def get_default_account(
    db: AsyncSession, *, organization_id: str
) -> OrganizationAccount | None:
    result = await db.execute(
        select(OrganizationAccount).where(
            OrganizationAccount.organization_id == organization_id,
            OrganizationAccount.is_default.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def list_accounts(
    db: AsyncSession,
    *,
    organization_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    """Oldest first, so the default an Organization started with reads first.

    The other lists in this API run newest first, where the newest row is the
    one a caller just made. Accounts are a small, stable set that gets read as
    a whole, and the one that answers "who pays when nobody says" belongs at
    the top of it.
    """
    return await fetch_page(
        db,
        select(OrganizationAccount).where(
            OrganizationAccount.organization_id == organization_id
        ),
        sort=OrganizationAccount.created_at,
        id_column=OrganizationAccount.id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        descending=False,
    )


async def attach_credential(
    db: AsyncSession,
    *,
    account: OrganizationAccount,
    backend: str,
    key_hash: str,
    provider_key_name: str | None,
    encrypted_key: str,
    generation: int = 1,
    status: str = CREDENTIAL_ACTIVE,
) -> AccountModelCredential:
    credential = AccountModelCredential(
        id=new_id("acctcred"),
        account_id=account.id,
        funding_mode=account.funding_mode,
        backend=backend,
        organization_id=account.organization_id,
        key_hash=key_hash,
        provider_key_name=provider_key_name,
        encrypted_key=encrypted_key,
        status=status,
        generation=generation,
    )
    db.add(credential)
    await db.flush()
    return credential


async def replace_credential(
    db: AsyncSession,
    *,
    credential: AccountModelCredential,
    key_hash: str,
    encrypted_key: str,
    status: str,
) -> None:
    """Atomically replace secret material while retaining the backend slot."""

    credential.key_hash = key_hash
    credential.encrypted_key = encrypted_key
    credential.status = status
    credential.generation += 1
    await db.flush()


async def delete_credential(
    db: AsyncSession, *, credential: AccountModelCredential
) -> None:
    await db.delete(credential)
    await db.flush()


async def set_status(
    db: AsyncSession,
    *,
    account: OrganizationAccount,
    account_status: str,
    credential_status: str,
) -> None:
    """Move the Account and its credential together.

    Separately would let the two disagree, and the pair is what a suspension
    means: an Account marked suspended whose key still spends is a suspension
    only on paper.
    """
    account.status = account_status
    for credential in account.model_credentials:
        credential.status = credential_status
    await db.flush()


async def get_observed_token_usage(
    db: AsyncSession,
    *,
    account: OrganizationAccount,
) -> tuple[int, int, int]:
    """Sum normalized usage events emitted by calls billed to an Account.

    Sessions predating Account pinning have ``account_id = NULL`` and resolve
    to the Organization default at call time, so those belong to the default
    Account's observed total as well.
    """

    usage = SessionEvent.payload["usage"]
    input_tokens = usage["input_tokens"].as_integer()
    output_tokens = usage["output_tokens"].as_integer()
    total_tokens = usage["total_tokens"].as_integer()
    account_match = Session.account_id == account.id
    if account.is_default:
        account_match = or_(
            account_match,
            (
                Session.account_id.is_(None)
                & (Session.organization_id == account.organization_id)
            ),
        )

    result = await db.execute(
        select(
            func.coalesce(func.sum(input_tokens), 0),
            func.coalesce(func.sum(output_tokens), 0),
            func.coalesce(func.sum(total_tokens), 0),
        )
        .select_from(SessionEvent)
        .join(Session, Session.id == SessionEvent.session_id)
        .where(
            SessionEvent.organization_id == account.organization_id,
            SessionEvent.type == event_types.MODEL_USAGE,
            account_match,
        )
    )
    found = result.one()
    return int(found[0]), int(found[1]), int(found[2])


__all__ = [
    "attach_credential",
    "create_account",
    "delete_credential",
    "get_account",
    "get_account_for_update",
    "get_by_idempotency_key",
    "get_default_account",
    "get_observed_token_usage",
    "list_accounts",
    "replace_credential",
    "set_status",
]
