"""Create-time funding selection for managed Sessions.

The caller may require Organization Vault BYOK, require Organization platform
credits, or defer to the Organization's default policy.  Selection happens
exactly once when the Session is created.  Runtime turns use the resulting
durable binding and never re-run this fallback order.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import organization_funding as organization_funding_q
from app.runtime.model_credentials import (
    MODEL_CREDENTIAL_BINDING_VERSION,
    ModelCredentialRequiredError,
    resolve_model_credential_binding,
)
from app.runtime.providers import runtime_provider_api_key_env, runtime_provider_id


SessionFundingType = Literal[
    "organization_default",
    "byok",
    "platform_credits",
]


class SessionFundingUnavailableError(ModelCredentialRequiredError):
    """Raised when the requested Session funding source cannot be fixed."""


async def resolve_session_funding_binding(
    db: AsyncSession,
    *,
    model: dict[str, Any],
    runtime: dict[str, Any] | None,
    vault_ids: list[str] | None,
    funding_type: SessionFundingType | str | None,
    organization_id: str,
) -> dict[str, Any]:
    """Resolve one immutable model-credential binding for a new Session."""

    requested = _normalize_funding_type(funding_type)
    provider = runtime_provider_id(model, runtime=runtime)
    secret_name = runtime_provider_api_key_env(model, runtime=runtime)
    if secret_name is None:
        return _keyless_binding(provider)

    account = await organization_funding_q.load_organization_billing_account(
        db,
        organization_id=organization_id,
    )
    policy = account.policy if account is not None else "byok_only"
    if requested == "byok":
        if policy == "platform_only":
            raise SessionFundingUnavailableError(
                "The Organization funding policy does not allow BYOK for this Session"
            )
        return await _resolve_byok(
            db,
            model=model,
            runtime=runtime,
            vault_ids=vault_ids,
            organization_id=organization_id,
        )
    if requested == "platform_credits":
        if policy == "byok_only":
            raise SessionFundingUnavailableError(
                "The Organization funding policy does not allow platform credits for this Session"
            )
        return await _resolve_platform(
            db,
            provider=provider,
            secret_name=secret_name,
            organization_id=organization_id,
        )

    if policy == "byok_only":
        return await _resolve_byok(
            db,
            model=model,
            runtime=runtime,
            vault_ids=vault_ids,
            organization_id=organization_id,
        )
    if policy == "platform_only":
        return await _resolve_platform(
            db,
            provider=provider,
            secret_name=secret_name,
            organization_id=organization_id,
        )

    if policy == "prefer_byok":
        first = "byok"
    elif policy == "prefer_platform":
        first = "platform"
    else:  # pragma: no cover - protected by database and query validation
        raise SessionFundingUnavailableError(
            "The Organization funding policy is invalid"
        )

    errors: list[Exception] = []
    for source in (first, "platform" if first == "byok" else "byok"):
        try:
            if source == "byok":
                return await _resolve_byok(
                    db,
                    model=model,
                    runtime=runtime,
                    vault_ids=vault_ids,
                    organization_id=organization_id,
                )
            return await _resolve_platform(
                db,
                provider=provider,
                secret_name=secret_name,
                organization_id=organization_id,
            )
        except (
            ModelCredentialRequiredError,
            organization_funding_q.OrganizationFundingUnavailableError,
        ) as exc:
            errors.append(exc)

    raise SessionFundingUnavailableError(
        f"No usable Organization funding source is available for model provider {provider}"
    ) from errors[-1]


async def _resolve_byok(
    db: AsyncSession,
    *,
    model: dict[str, Any],
    runtime: dict[str, Any] | None,
    vault_ids: list[str] | None,
    organization_id: str,
) -> dict[str, Any]:
    return await resolve_model_credential_binding(
        db,
        model=model,
        runtime=runtime,
        vault_ids=vault_ids,
        organization_id=organization_id,
    )


async def _resolve_platform(
    db: AsyncSession,
    *,
    provider: str,
    secret_name: str,
    organization_id: str,
) -> dict[str, Any]:
    try:
        key_binding = (
            await organization_funding_q.load_active_organization_provider_key_binding(
                db,
                provider=provider,
                organization_id=organization_id,
            )
        )
    except organization_funding_q.OrganizationFundingUnavailableError as exc:
        raise SessionFundingUnavailableError(str(exc)) from exc
    return {
        "version": MODEL_CREDENTIAL_BINDING_VERSION,
        "source": "platform",
        "credential_id": None,
        "vault_id": None,
        "model_provider": provider,
        "secret_name": secret_name,
        "organization_billing_account_id": (
            key_binding.organization_billing_account_id
        ),
        "organization_provider_key_binding_id": key_binding.id,
    }


def _keyless_binding(provider: str) -> dict[str, Any]:
    return {
        "version": MODEL_CREDENTIAL_BINDING_VERSION,
        "source": "none",
        "credential_id": None,
        "vault_id": None,
        "model_provider": provider,
        "secret_name": None,
        "organization_billing_account_id": None,
        "organization_provider_key_binding_id": None,
    }


def _normalize_funding_type(value: SessionFundingType | str | None) -> SessionFundingType:
    normalized = str(value or "organization_default").strip().lower()
    if normalized not in {"organization_default", "byok", "platform_credits"}:
        raise ValueError(
            "Session funding type must be organization_default, byok, or platform_credits"
        )
    return cast(SessionFundingType, normalized)
