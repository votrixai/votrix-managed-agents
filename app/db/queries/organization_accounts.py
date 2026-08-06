"""Reads and writes over billing Accounts.

No delete. An Account carries the record of what it was charged, so removing
one removes that record; suspension is how an Account stops being usable.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page

from app.db.models.organization_accounts import (
    ACCOUNT_PROVISIONING,
    CREDENTIAL_ACTIVE,
    AccountProviderCredential,
    OrganizationAccount,
)
from app.utils.id_generator import new_id


async def create_account(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    is_default: bool = False,
    limit_usd: Decimal | None = None,
    idempotency_key: str | None = None,
) -> OrganizationAccount:
    """Write the Account row, before it has anything to spend through.

    It starts in `provisioning` because minting the credential is a call to
    another service: the row has to exist first so a mint that fails halfway
    leaves something to find, rather than a key nobody can trace.
    """
    account = OrganizationAccount(
        id=new_id("acct"),
        organization_id=organization_id,
        name=name,
        status=ACCOUNT_PROVISIONING,
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
    key_hash: str,
    provider_key_name: str,
    encrypted_key: str,
    generation: int = 1,
) -> AccountProviderCredential:
    credential = AccountProviderCredential(
        id=new_id("acctcred"),
        account_id=account.id,
        organization_id=account.organization_id,
        key_hash=key_hash,
        provider_key_name=provider_key_name,
        encrypted_key=encrypted_key,
        status=CREDENTIAL_ACTIVE,
        generation=generation,
    )
    db.add(credential)
    await db.flush()
    return credential


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
    if account.credential is not None:
        account.credential.status = credential_status
    await db.flush()


__all__ = [
    "attach_credential",
    "create_account",
    "get_account",
    "get_by_idempotency_key",
    "get_default_account",
    "list_accounts",
    "set_status",
]
