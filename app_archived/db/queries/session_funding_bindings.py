from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ManagedSession, SessionFundingBinding
from app.db.queries import organization_funding as organization_funding_q
from app.db.queries import resources as resources_q
from app.ids import new_id
from app.organization import resolve_organization_id


class SessionFundingBindingSessionNotFoundError(RuntimeError):
    """Raised when the parent Session is outside the requested Organization."""


class SessionFundingBindingConflictError(RuntimeError):
    """Raised when code attempts to change an immutable funding binding."""


class SessionFundingBindingResourceError(RuntimeError):
    """Raised when a binding does not reference an active matching resource."""


_COORDINATE_FIELDS = (
    "vault_id",
    "model_credential_id",
    "organization_billing_account_id",
    "organization_provider_key_binding_id",
)


async def get_session_funding_binding(
    db: AsyncSession,
    session_id: str,
    *,
    organization_id: str | None = None,
    for_update: bool = False,
) -> SessionFundingBinding | None:
    """Return the immutable funding choice for one Organization-scoped Session."""

    scoped_organization_id = resolve_organization_id(organization_id)
    parent = await _get_scoped_session(
        db,
        session_id=session_id,
        organization_id=scoped_organization_id,
        for_update=for_update,
    )
    if parent is None:
        return None

    stmt = (
        select(SessionFundingBinding)
        .where(
            SessionFundingBinding.organization_id == scoped_organization_id,
            SessionFundingBinding.session_id == session_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_session_funding_binding(
    db: AsyncSession,
    *,
    session_id: str,
    source: str,
    provider: str,
    model_id: str,
    vault_id: str | None = None,
    model_credential_id: str | None = None,
    organization_billing_account_id: str | None = None,
    organization_provider_key_binding_id: str | None = None,
    organization_id: str | None = None,
) -> SessionFundingBinding:
    """Create one immutable binding, replaying only an identical request.

    Locking the parent Session serializes concurrent creators. The database
    uniqueness constraint is the final guard for dialects where row locking is
    limited. Funding resources are validated at creation and retained so a
    later revocation can fail closed without losing provenance.
    """

    scoped_organization_id = resolve_organization_id(organization_id)
    await _require_scoped_session(
        db,
        session_id=session_id,
        organization_id=scoped_organization_id,
        for_update=True,
    )

    normalized = _normalize_values(
        source=source,
        provider=provider,
        model_id=model_id,
        vault_id=vault_id,
        model_credential_id=model_credential_id,
        organization_billing_account_id=organization_billing_account_id,
        organization_provider_key_binding_id=organization_provider_key_binding_id,
    )

    existing = await get_session_funding_binding(
        db,
        session_id,
        organization_id=scoped_organization_id,
        for_update=True,
    )
    if existing is not None:
        if _binding_values(existing) == normalized:
            return existing
        raise SessionFundingBindingConflictError(
            f"Session {session_id} already has a different funding binding"
        )

    if normalized["source"] == "vault":
        await _validate_vault_coordinates(
            db,
            organization_id=scoped_organization_id,
            vault_id=str(normalized["vault_id"]),
            model_credential_id=str(normalized["model_credential_id"]),
        )
    elif normalized["source"] == "platform":
        await _validate_platform_coordinates(
            db,
            organization_id=scoped_organization_id,
            provider=str(normalized["provider"]),
            organization_billing_account_id=str(
                normalized["organization_billing_account_id"]
            ),
            organization_provider_key_binding_id=str(
                normalized["organization_provider_key_binding_id"]
            ),
        )

    binding = SessionFundingBinding(
        id=new_id("funding"),
        organization_id=scoped_organization_id,
        session_id=session_id,
        **normalized,
    )
    db.add(binding)
    await db.flush()
    return binding


async def _validate_vault_coordinates(
    db: AsyncSession,
    *,
    organization_id: str,
    vault_id: str,
    model_credential_id: str,
) -> None:
    vault = await resources_q.get_resource(
        db,
        resource_id=vault_id,
        resource_type="vault",
        organization_id=organization_id,
    )
    if vault is None or vault.archived_at is not None:
        raise SessionFundingBindingResourceError("Funding Vault is unavailable")

    credential = await resources_q.get_resource(
        db,
        resource_id=model_credential_id,
        resource_type="credential",
        parent_id=vault_id,
        organization_id=organization_id,
    )
    if credential is None or credential.archived_at is not None:
        raise SessionFundingBindingResourceError(
            "Funding model Credential is unavailable"
        )


async def _validate_platform_coordinates(
    db: AsyncSession,
    *,
    organization_id: str,
    provider: str,
    organization_billing_account_id: str,
    organization_provider_key_binding_id: str,
) -> None:
    try:
        await organization_funding_q.load_active_organization_provider_key_binding(
            db,
            organization_id=organization_id,
            provider=provider,
            provider_key_binding_id=organization_provider_key_binding_id,
            organization_billing_account_id=organization_billing_account_id,
        )
    except organization_funding_q.OrganizationFundingUnavailableError as exc:
        raise SessionFundingBindingResourceError(
            "Organization platform funding is unavailable"
        ) from exc


def _normalize_values(
    *,
    source: str,
    provider: str,
    model_id: str,
    vault_id: str | None,
    model_credential_id: str | None,
    organization_billing_account_id: str | None,
    organization_provider_key_binding_id: str | None,
) -> dict[str, Any]:
    normalized_source = str(source or "").strip().lower()
    normalized_provider = str(provider or "").strip().lower().replace("-", "_")
    normalized_model_id = str(model_id or "").strip()
    if normalized_source not in {"none", "vault", "platform"}:
        raise ValueError("Funding source must be none, vault, or platform")
    if not normalized_provider:
        raise ValueError("Funding provider is required")
    if not normalized_model_id:
        raise ValueError("Funding model_id is required")

    values: dict[str, Any] = {
        "source": normalized_source,
        "provider": normalized_provider,
        "model_id": normalized_model_id,
        "vault_id": _optional_id(vault_id),
        "model_credential_id": _optional_id(model_credential_id),
        "organization_billing_account_id": _optional_id(
            organization_billing_account_id
        ),
        "organization_provider_key_binding_id": _optional_id(
            organization_provider_key_binding_id
        ),
    }
    present = {field for field in _COORDINATE_FIELDS if values[field] is not None}
    expected = {
        "none": set(),
        "vault": {"vault_id", "model_credential_id"},
        "platform": {
            "organization_billing_account_id",
            "organization_provider_key_binding_id",
        },
    }[normalized_source]
    if present != expected:
        raise ValueError(
            f"Funding source {normalized_source} requires exactly {sorted(expected)}"
        )
    return values


def _optional_id(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _binding_values(binding: SessionFundingBinding) -> dict[str, Any]:
    return {
        "source": binding.source,
        "provider": binding.provider,
        "model_id": binding.model_id,
        "vault_id": binding.vault_id,
        "model_credential_id": binding.model_credential_id,
        "organization_billing_account_id": binding.organization_billing_account_id,
        "organization_provider_key_binding_id": (
            binding.organization_provider_key_binding_id
        ),
    }


async def _get_scoped_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    for_update: bool = False,
) -> ManagedSession | None:
    stmt = select(ManagedSession).where(
        ManagedSession.id == session_id,
        ManagedSession.organization_id == organization_id,
        ManagedSession.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _require_scoped_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    for_update: bool,
) -> ManagedSession:
    session = await _get_scoped_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
        for_update=for_update,
    )
    if session is None:
        raise SessionFundingBindingSessionNotFoundError(
            f"Session {session_id} was not found in Organization {organization_id}"
        )
    return session
