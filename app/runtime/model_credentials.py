"""Create-time model credential binding for managed Sessions.

The public API stays compatible with Claude Managed Agents: callers provide an
ordered ``vault_ids`` list.  VMA resolves the first matching model credential
once and persists only its resource ID.  Runtime turns re-read that exact row so
rotation is immediate, while archive/delete revocation fails closed instead of
silently switching the payer. Providers that require an API key never fall back
to a service-owned credential.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import resources as res_q
from app.runtime.providers import runtime_provider_api_key_env, runtime_provider_id

MODEL_CREDENTIAL_BINDING_KEY = "model_credential_binding"
MODEL_CREDENTIAL_BINDING_VERSION = 1
MODEL_CREDENTIAL_KIND = "model_provider_api_key"


class ModelCredentialUnavailableError(RuntimeError):
    """A Session's fixed model Credential can no longer be used."""


class ModelCredentialRequiredError(ModelCredentialUnavailableError):
    """A key-requiring model has no matching Organization Vault credential."""


async def resolve_model_credential_binding(
    db: AsyncSession,
    *,
    model: dict[str, Any],
    runtime: dict[str, Any] | None,
    vault_ids: list[str] | None,
    organization_id: str,
) -> dict[str, Any]:
    """Resolve the first matching Vault credential and otherwise fail closed."""
    provider_id = runtime_provider_id(model, runtime=runtime)
    secret_name = runtime_provider_api_key_env(model, runtime=runtime)
    if not secret_name:
        return {
            "version": MODEL_CREDENTIAL_BINDING_VERSION,
            "source": "none",
            "credential_id": None,
            "vault_id": None,
            "model_provider": provider_id,
            "secret_name": None,
        }

    for raw_vault_id in vault_ids or []:
        vault_id = str(raw_vault_id or "")
        if not vault_id:
            continue
        vault = await res_q.get_resource(
            db,
            resource_id=vault_id,
            resource_type="vault",
            organization_id=organization_id,
        )
        if vault is None or vault.archived_at is not None:
            continue
        credentials = await res_q.list_resources(
            db,
            resource_type="credential",
            parent_id=vault_id,
            limit=1000,
            include_archived=False,
            organization_id=organization_id,
        )
        for credential in credentials:
            if _is_matching_environment_credential(
                credential,
                secret_name,
                provider_id=provider_id,
            ):
                return {
                    "version": MODEL_CREDENTIAL_BINDING_VERSION,
                    "source": "vault",
                    "credential_id": credential.id,
                    "vault_id": vault_id,
                    "model_provider": provider_id,
                    "secret_name": secret_name,
                }
    raise ModelCredentialRequiredError(
        f"Model provider {provider_id} requires a model Credential from one of the Session vault_ids"
    )


async def load_bound_model_credential(
    db: AsyncSession,
    *,
    binding: dict[str, Any],
    organization_id: str,
):
    """Load and validate the exact credential selected for a Session."""
    if binding.get("source") == "none":
        return None
    if binding.get("source") != "vault":
        raise ModelCredentialUnavailableError(
            "Server-provided model credentials are disabled; create a new Session with a Organization Vault credential"
        )

    credential_id = str(binding.get("credential_id") or "")
    vault_id = str(binding.get("vault_id") or "")
    secret_name = str(binding.get("secret_name") or "")
    provider_id = str(binding.get("model_provider") or "")
    if not credential_id or not vault_id or not secret_name or not provider_id:
        raise ModelCredentialUnavailableError("The Session model credential binding is invalid")

    vault = await res_q.get_resource(
        db,
        resource_id=vault_id,
        resource_type="vault",
        organization_id=organization_id,
    )
    if vault is None or vault.archived_at is not None:
        raise _unavailable(credential_id)

    credential = await res_q.get_resource(
        db,
        resource_id=credential_id,
        resource_type="credential",
        parent_id=vault_id,
        organization_id=organization_id,
    )
    if (
        credential is None
        or credential.archived_at is not None
        or not _is_matching_environment_credential(
            credential,
            secret_name,
            provider_id=provider_id,
        )
    ):
        raise _unavailable(credential_id)
    return credential


def binding_from_status_details(status_details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status_details, dict):
        return None
    binding = status_details.get(MODEL_CREDENTIAL_BINDING_KEY)
    if not isinstance(binding, dict):
        return None
    if binding.get("version") != MODEL_CREDENTIAL_BINDING_VERSION:
        return None
    return dict(binding)


def validate_binding_for_model(
    binding: dict[str, Any],
    *,
    model: dict[str, Any],
    runtime: dict[str, Any] | None,
) -> None:
    """Reject stale/corrupt bindings instead of silently changing auth source."""
    expected_provider = runtime_provider_id(model, runtime=runtime)
    expected_secret_name = runtime_provider_api_key_env(model, runtime=runtime)
    if binding.get("source") not in {"none", "vault"}:
        if binding.get("source") == "server":
            raise ModelCredentialUnavailableError(
                "Server-provided model credentials are disabled; create a new Session with a Organization Vault credential"
            )
        raise ModelCredentialUnavailableError("The Session model credential binding is invalid")
    if binding.get("secret_name") != expected_secret_name:
        raise ModelCredentialUnavailableError(
            "The Session model credential no longer matches its provider configuration; create a new Session"
        )
    bound_provider = binding.get("model_provider")
    if bound_provider is not None and bound_provider != expected_provider:
        raise ModelCredentialUnavailableError(
            "The Session model credential no longer matches its provider configuration; create a new Session"
        )
    if binding.get("source") == "none" and (
        binding.get("credential_id") is not None or binding.get("vault_id") is not None
    ):
        raise ModelCredentialUnavailableError("The Session model credential binding is invalid")


def status_details_with_binding(
    status_details: dict[str, Any] | None,
    binding: dict[str, Any],
) -> dict[str, Any]:
    details = dict(status_details or {})
    details[MODEL_CREDENTIAL_BINDING_KEY] = dict(binding)
    return details


def _is_matching_environment_credential(
    credential,
    secret_name: str,
    *,
    provider_id: str,
) -> bool:
    data = dict(credential.data or {})
    credential_kind = data.get("credential_kind")
    declared_provider = data.get("model_provider")

    # Native model Credentials are explicitly typed and provider-scoped. Rows
    # without a marker remain valid for compatibility with credentials created
    # through the original generic Vault API.
    if credential_kind is not None and credential_kind != MODEL_CREDENTIAL_KIND:
        return False
    if credential_kind == MODEL_CREDENTIAL_KIND and not declared_provider:
        return False
    if declared_provider is not None and declared_provider != provider_id:
        return False

    auth = dict(data.get("auth") or {})
    return (
        auth.get("type") == "environment_variable"
        and str(auth.get("secret_name") or "").strip() == secret_name
        and isinstance(auth.get("secret_value"), str)
        and bool(auth.get("secret_value"))
    )


def _unavailable(credential_id: str) -> ModelCredentialUnavailableError:
    return ModelCredentialUnavailableError(
        f"The Session model credential {credential_id} is unavailable; create a new Session"
    )
