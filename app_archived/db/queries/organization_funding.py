from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    OrganizationBillingAccount,
    OrganizationProviderKeyBinding,
)
from app.ids import new_id
from app.organization import resolve_organization_id
from app.secret_cipher import decrypt_secret, encrypt_secret


BILLING_ACCOUNT_STATUSES = frozenset({"active", "suspended", "closed"})
FUNDING_POLICIES = frozenset(
    {"byok_only", "platform_only", "prefer_byok", "prefer_platform"}
)
PROVIDER_KEY_STATUSES = frozenset({"active", "revoked"})
USD_CURRENCY = "USD"

_UNSET = object()


class OrganizationFundingConflictError(RuntimeError):
    """Raised when one-Organization persistence invariants would be violated."""


class OrganizationFundingUnavailableError(RuntimeError):
    """Raised when an Organization funding record cannot authorize a model call."""


async def load_organization_billing_account(
    db: AsyncSession,
    *,
    organization_id: str | None = None,
    for_update: bool = False,
) -> OrganizationBillingAccount | None:
    """Load the single billing account belonging to an Organization."""

    scoped_organization_id = resolve_organization_id(organization_id)
    stmt = select(OrganizationBillingAccount).where(
        OrganizationBillingAccount.organization_id == scoped_organization_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_organization_billing_account(
    db: AsyncSession,
    *,
    policy: str = "byok_only",
    status: str = "active",
    trial_expires_at: datetime | None = None,
    organization_id: str | None = None,
) -> OrganizationBillingAccount:
    """Create exactly one USD billing account for an Organization."""

    scoped_organization_id = resolve_organization_id(organization_id)
    normalized_policy = _normalize_policy(policy)
    normalized_status = _normalize_account_status(status)
    existing = await load_organization_billing_account(
        db,
        organization_id=scoped_organization_id,
        for_update=True,
    )
    if existing is not None:
        raise OrganizationFundingConflictError(
            f"Organization {scoped_organization_id} already has a billing account"
        )

    account = OrganizationBillingAccount(
        id=new_id("billacct"),
        organization_id=scoped_organization_id,
        status=normalized_status,
        policy=normalized_policy,
        currency=USD_CURRENCY,
        trial_expires_at=trial_expires_at,
    )
    db.add(account)
    await db.flush()
    return account


async def update_organization_billing_account(
    db: AsyncSession,
    *,
    status: str | None = None,
    policy: str | None = None,
    trial_expires_at: datetime | None | object = _UNSET,
    organization_id: str | None = None,
) -> OrganizationBillingAccount:
    """Update mutable Organization funding policy and lifecycle fields."""

    scoped_organization_id = resolve_organization_id(organization_id)
    account = await load_organization_billing_account(
        db,
        organization_id=scoped_organization_id,
        for_update=True,
    )
    if account is None:
        raise OrganizationFundingUnavailableError(
            f"Organization {scoped_organization_id} has no billing account"
        )
    if status is not None:
        account.status = _normalize_account_status(status)
    if policy is not None:
        account.policy = _normalize_policy(policy)
    if trial_expires_at is not _UNSET:
        account.trial_expires_at = trial_expires_at
    await db.flush()
    return account


async def load_organization_provider_key_binding(
    db: AsyncSession,
    *,
    provider: str,
    organization_id: str | None = None,
    for_update: bool = False,
) -> OrganizationProviderKeyBinding | None:
    """Load provider-key metadata without decrypting or returning its API key."""

    scoped_organization_id = resolve_organization_id(organization_id)
    normalized_provider = _normalize_provider(provider)
    stmt = select(OrganizationProviderKeyBinding).where(
        OrganizationProviderKeyBinding.organization_id == scoped_organization_id,
        OrganizationProviderKeyBinding.provider == normalized_provider,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def load_organization_provider_key_binding_by_id(
    db: AsyncSession,
    *,
    provider_key_binding_id: str,
    organization_id: str | None = None,
    for_update: bool = False,
) -> OrganizationProviderKeyBinding | None:
    """Load one exact, Organization-scoped provider-key binding row."""

    scoped_organization_id = resolve_organization_id(organization_id)
    stmt = select(OrganizationProviderKeyBinding).where(
        OrganizationProviderKeyBinding.id == str(provider_key_binding_id).strip(),
        OrganizationProviderKeyBinding.organization_id == scoped_organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_organization_provider_key_binding(
    db: AsyncSession,
    *,
    organization_billing_account_id: str,
    provider: str,
    api_key: str,
    upstream_key_id: str | None = None,
    spending_limit_usd_micros: int | None = None,
    expires_at: datetime | None = None,
    status: str = "active",
    organization_id: str | None = None,
) -> OrganizationProviderKeyBinding:
    """Store one encrypted platform-provider API key for an Organization."""

    scoped_organization_id = resolve_organization_id(organization_id)
    normalized_provider = _normalize_provider(provider)
    account = await _require_billing_account(
        db,
        organization_id=scoped_organization_id,
        account_id=organization_billing_account_id,
        for_update=True,
    )
    existing = await load_organization_provider_key_binding(
        db,
        organization_id=scoped_organization_id,
        provider=normalized_provider,
        for_update=True,
    )
    if existing is not None:
        raise OrganizationFundingConflictError(
            f"Organization {scoped_organization_id} already has a {normalized_provider} provider key binding"
        )

    binding = OrganizationProviderKeyBinding(
        id=new_id("providerkey"),
        organization_id=scoped_organization_id,
        organization_billing_account_id=account.id,
        provider=normalized_provider,
        encrypted_api_key=encrypt_secret(_require_api_key(api_key)),
        upstream_key_id=_optional_string(upstream_key_id),
        spending_limit_usd_micros=_normalize_spending_limit(
            spending_limit_usd_micros
        ),
        expires_at=expires_at,
        status=_normalize_provider_key_status(status),
    )
    db.add(binding)
    await db.flush()
    return binding


async def rotate_organization_provider_key_binding(
    db: AsyncSession,
    *,
    provider: str,
    api_key: str,
    upstream_key_id: str | None | object = _UNSET,
    spending_limit_usd_micros: int | None | object = _UNSET,
    expires_at: datetime | None | object = _UNSET,
    organization_id: str | None = None,
) -> OrganizationProviderKeyBinding:
    """Rotate a secret in place so durable Session coordinates remain stable."""

    scoped_organization_id = resolve_organization_id(organization_id)
    binding = await _require_provider_key_binding(
        db,
        organization_id=scoped_organization_id,
        provider=provider,
        for_update=True,
    )
    await _require_billing_account(
        db,
        organization_id=scoped_organization_id,
        account_id=binding.organization_billing_account_id,
        for_update=True,
    )
    binding.encrypted_api_key = encrypt_secret(_require_api_key(api_key))
    binding.status = "active"
    if upstream_key_id is not _UNSET:
        binding.upstream_key_id = _optional_string(upstream_key_id)
    if spending_limit_usd_micros is not _UNSET:
        binding.spending_limit_usd_micros = _normalize_spending_limit(
            spending_limit_usd_micros
        )
    if expires_at is not _UNSET:
        binding.expires_at = expires_at
    await db.flush()
    return binding


async def update_organization_provider_key_binding(
    db: AsyncSession,
    *,
    provider: str,
    status: str | None = None,
    upstream_key_id: str | None | object = _UNSET,
    spending_limit_usd_micros: int | None | object = _UNSET,
    expires_at: datetime | None | object = _UNSET,
    organization_id: str | None = None,
) -> OrganizationProviderKeyBinding:
    """Update non-secret provider-key lifecycle and upstream metadata."""

    scoped_organization_id = resolve_organization_id(organization_id)
    binding = await _require_provider_key_binding(
        db,
        organization_id=scoped_organization_id,
        provider=provider,
        for_update=True,
    )
    if status is not None:
        binding.status = _normalize_provider_key_status(status)
    if upstream_key_id is not _UNSET:
        binding.upstream_key_id = _optional_string(upstream_key_id)
    if spending_limit_usd_micros is not _UNSET:
        binding.spending_limit_usd_micros = _normalize_spending_limit(
            spending_limit_usd_micros
        )
    if expires_at is not _UNSET:
        binding.expires_at = expires_at
    await db.flush()
    return binding


async def load_active_organization_provider_key_binding(
    db: AsyncSession,
    *,
    provider: str,
    provider_key_binding_id: str | None = None,
    organization_billing_account_id: str | None = None,
    organization_id: str | None = None,
    now: datetime | None = None,
) -> OrganizationProviderKeyBinding:
    """Load metadata only after active-account and expiration validation."""

    scoped_organization_id = resolve_organization_id(organization_id)
    normalized_provider = _normalize_provider(provider)
    if provider_key_binding_id is None:
        binding = await _require_provider_key_binding(
            db,
            organization_id=scoped_organization_id,
            provider=normalized_provider,
            for_update=False,
        )
    else:
        binding = await load_organization_provider_key_binding_by_id(
            db,
            organization_id=scoped_organization_id,
            provider_key_binding_id=provider_key_binding_id,
        )
        if binding is None:
            raise OrganizationFundingUnavailableError(
                "Organization provider key binding was not found"
            )
        if binding.provider != normalized_provider:
            raise OrganizationFundingUnavailableError(
                "Organization provider key binding does not match the Session provider"
            )
    if (
        organization_billing_account_id is not None
        and binding.organization_billing_account_id
        != str(organization_billing_account_id).strip()
    ):
        raise OrganizationFundingUnavailableError(
            "Organization provider key binding does not match the Session billing account"
        )
    account = await _require_billing_account(
        db,
        organization_id=scoped_organization_id,
        account_id=binding.organization_billing_account_id,
        for_update=False,
    )
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    if account.status != "active":
        raise OrganizationFundingUnavailableError(
            "Organization billing account is not active"
        )
    if _is_expired(account.trial_expires_at, now=effective_now):
        raise OrganizationFundingUnavailableError(
            "Organization trial funding has expired"
        )
    if binding.status != "active":
        raise OrganizationFundingUnavailableError(
            f"Organization {binding.provider} provider key binding is not active"
        )
    if _is_expired(binding.expires_at, now=effective_now):
        raise OrganizationFundingUnavailableError(
            f"Organization {binding.provider} provider key binding has expired"
        )
    return binding


async def _load_active_organization_provider_api_key(
    db: AsyncSession,
    *,
    provider: str,
    provider_key_binding_id: str | None = None,
    organization_billing_account_id: str | None = None,
    organization_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Private runtime seam that is allowed to return a decrypted API key."""

    binding = await load_active_organization_provider_key_binding(
        db,
        provider=provider,
        provider_key_binding_id=provider_key_binding_id,
        organization_billing_account_id=organization_billing_account_id,
        organization_id=organization_id,
        now=now,
    )
    return decrypt_secret(binding.encrypted_api_key)


async def _require_billing_account(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    for_update: bool,
) -> OrganizationBillingAccount:
    stmt = select(OrganizationBillingAccount).where(
        OrganizationBillingAccount.id == str(account_id).strip(),
        OrganizationBillingAccount.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    account = (await db.execute(stmt)).scalar_one_or_none()
    if account is None:
        raise OrganizationFundingUnavailableError(
            "Organization billing account was not found"
        )
    return account


async def _require_provider_key_binding(
    db: AsyncSession,
    *,
    organization_id: str,
    provider: str,
    for_update: bool,
) -> OrganizationProviderKeyBinding:
    binding = await load_organization_provider_key_binding(
        db,
        organization_id=organization_id,
        provider=provider,
        for_update=for_update,
    )
    if binding is None:
        raise OrganizationFundingUnavailableError(
            f"Organization has no {_normalize_provider(provider)} provider key binding"
        )
    return binding


def _normalize_account_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in BILLING_ACCOUNT_STATUSES:
        raise ValueError(
            "Organization billing account status must be active, suspended, or closed"
        )
    return normalized


def _normalize_policy(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in FUNDING_POLICIES:
        raise ValueError(
            "Organization funding policy must be byok_only, platform_only, prefer_byok, or prefer_platform"
        )
    return normalized


def _normalize_provider_key_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in PROVIDER_KEY_STATUSES:
        raise ValueError("Organization provider key status must be active or revoked")
    return normalized


def _normalize_provider(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if not normalized:
        raise ValueError("Provider is required")
    return normalized


def _normalize_spending_limit(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("spending_limit_usd_micros must be a non-negative integer")
    if value < 0:
        raise ValueError("spending_limit_usd_micros must be a non-negative integer")
    return value


def _require_api_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Provider API key is required")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _is_expired(value: datetime | None, *, now: datetime) -> bool:
    return value is not None and _aware_utc(value) <= now


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
