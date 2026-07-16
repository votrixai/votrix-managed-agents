"""Run-scoped model credential resolution for managed Sessions.

The broker is the only runtime boundary that turns a durable Session model
credential binding into plaintext provider secrets. The current implementation
supports Organization Vault BYOK and Organization platform funding, with no
process-environment fallback. Additional sources remain isolated from the Deep
Agents runtime contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import organization_funding as organization_funding_q
from app.db.queries import session_funding_bindings as funding_q
from app.db.queries import sessions as sessions_q
from app.runtime.contracts import EffectiveAgentVersion
from app.runtime.model_credentials import (
    MODEL_CREDENTIAL_BINDING_KEY,
    ModelCredentialUnavailableError,
    binding_from_status_details,
    load_bound_model_credential,
    resolve_model_credential_binding,
    status_details_with_binding,
    validate_binding_for_model,
)
from app.runtime.providers import (
    runtime_model_id,
    runtime_provider_api_key_env,
    runtime_provider_id,
)
from app.secret_cipher import decrypt_secret_values


class CredentialSourceResolver(Protocol):
    """Resolve one durable binding source into run-scoped provider secrets."""

    source: str

    async def resolve(
        self,
        db: AsyncSession,
        *,
        binding: dict[str, Any],
        organization_id: str,
    ) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class NoCredentialSourceResolver:
    """Resolve providers that do not require an API key."""

    source: str = "none"

    async def resolve(
        self,
        db: AsyncSession,
        *,
        binding: dict[str, Any],
        organization_id: str,
    ) -> dict[str, str]:
        del db, binding, organization_id
        return {}


@dataclass(frozen=True, slots=True)
class VaultCredentialSourceResolver:
    """Decrypt only the exact Vault credential fixed to the Session."""

    source: str = "vault"

    async def resolve(
        self,
        db: AsyncSession,
        *,
        binding: dict[str, Any],
        organization_id: str,
    ) -> dict[str, str]:
        credential = await load_bound_model_credential(
            db,
            binding=binding,
            organization_id=organization_id,
        )
        if credential is None:  # pragma: no cover - guarded by the source contract
            raise ModelCredentialUnavailableError(
                "The Session model credential binding is invalid"
            )

        auth = decrypt_secret_values(dict((credential.data or {}).get("auth") or {}))
        expected_secret_name = str(binding.get("secret_name") or "")
        secret_name = str(auth.get("secret_name") or "").strip()
        secret_value = auth.get("secret_value")
        if (
            auth.get("type") != "environment_variable"
            or secret_name != expected_secret_name
            or not isinstance(secret_value, str)
            or not secret_value
        ):
            raise ModelCredentialUnavailableError(
                f"The Session model credential {credential.id} is unavailable; create a new Session"
            )
        return {secret_name: secret_value}


@dataclass(frozen=True, slots=True)
class PlatformCredentialSourceResolver:
    """Decrypt the exact Organization provider key fixed to the Session."""

    source: str = "platform"

    async def resolve(
        self,
        db: AsyncSession,
        *,
        binding: dict[str, Any],
        organization_id: str,
    ) -> dict[str, str]:
        secret_name = str(binding.get("secret_name") or "")
        provider = str(binding.get("model_provider") or "")
        account_id = str(binding.get("organization_billing_account_id") or "")
        provider_key_binding_id = str(
            binding.get("organization_provider_key_binding_id") or ""
        )
        if not all((secret_name, provider, account_id, provider_key_binding_id)):
            raise ModelCredentialUnavailableError(
                "The Session model credential binding is invalid"
            )
        try:
            api_key = (
                await organization_funding_q._load_active_organization_provider_api_key(
                    db,
                    provider=provider,
                    provider_key_binding_id=provider_key_binding_id,
                    organization_billing_account_id=account_id,
                    organization_id=organization_id,
                )
            )
        except organization_funding_q.OrganizationFundingUnavailableError as exc:
            raise ModelCredentialUnavailableError(
                "The Session platform credential is unavailable; create a new Session"
            ) from exc
        if not api_key:
            raise ModelCredentialUnavailableError(
                "The Session platform credential is unavailable; create a new Session"
            )
        return {secret_name: api_key}


class SessionCredentialBroker:
    """Resolve the immutable credential choice for one Session turn.

    Source resolvers are constructor-injected so the model runtime and E2B do
    not need to know which Organization funding source owns a provider key.
    """

    def __init__(
        self,
        source_resolvers: tuple[CredentialSourceResolver, ...] | None = None,
    ) -> None:
        resolvers = source_resolvers or (
            NoCredentialSourceResolver(),
            VaultCredentialSourceResolver(),
            PlatformCredentialSourceResolver(),
        )
        self._source_resolvers = {resolver.source: resolver for resolver in resolvers}
        if len(self._source_resolvers) != len(resolvers):
            raise ValueError("Credential source resolver names must be unique")

    async def resolve_provider_secrets(
        self,
        db: AsyncSession,
        *,
        session: Any,
        version: EffectiveAgentVersion,
    ) -> dict[str, str]:
        """Return plaintext secrets scoped to the current model run only."""

        binding = await self._binding_for_session(db, session=session, version=version)
        validate_binding_for_model(
            binding,
            model=version.model,
            runtime=version.runtime,
        )
        source = str(binding.get("source") or "")
        resolver = self._source_resolvers.get(source)
        if resolver is None:
            raise ModelCredentialUnavailableError(
                "The Session model credential binding is invalid"
            )
        return await resolver.resolve(
            db,
            binding=binding,
            organization_id=session.organization_id,
        )

    @staticmethod
    async def _binding_for_session(
        db: AsyncSession,
        *,
        session: Any,
        version: EffectiveAgentVersion,
    ) -> dict[str, Any]:
        details = dict(session.status_details or {})
        legacy_binding = binding_from_status_details(details)
        if legacy_binding is None and MODEL_CREDENTIAL_BINDING_KEY in details:
            raise ModelCredentialUnavailableError(
                "The Session model credential binding is invalid"
            )

        durable_binding = await funding_q.get_session_funding_binding(
            db,
            session.id,
            organization_id=session.organization_id,
            for_update=True,
        )
        if durable_binding is not None:
            if legacy_binding is not None:
                validate_binding_for_model(
                    legacy_binding,
                    model=version.model,
                    runtime=version.runtime,
                )
            binding = SessionCredentialBroker._legacy_projection(
                durable_binding,
                version=version,
            )
            if legacy_binding is not None and not _same_binding(
                legacy_binding,
                binding,
            ):
                raise ModelCredentialUnavailableError(
                    "The Session model credential binding is invalid"
                )
            if legacy_binding is None:
                details = status_details_with_binding(details, binding)
                await sessions_q.update_session(db, session, status_details=details)
            return binding

        # Compatibility for Sessions created before durable bindings existed:
        # select once on their first runtime turn, then persist the same contract
        # used by newly created Sessions.
        binding = legacy_binding
        if binding is None:
            binding = await resolve_model_credential_binding(
                db,
                model=version.model,
                runtime=version.runtime,
                vault_ids=details.get("vault_ids") or [],
                organization_id=session.organization_id,
            )
            details = status_details_with_binding(details, binding)
            await sessions_q.update_session(db, session, status_details=details)

        validate_binding_for_model(
            binding,
            model=version.model,
            runtime=version.runtime,
        )
        try:
            await funding_q.create_session_funding_binding(
                db,
                session_id=session.id,
                source=str(binding.get("source") or ""),
                provider=runtime_provider_id(
                    version.model,
                    runtime=version.runtime,
                ),
                model_id=runtime_model_id(
                    version.model,
                    runtime=version.runtime,
                ),
                vault_id=binding.get("vault_id"),
                model_credential_id=binding.get("credential_id"),
                organization_billing_account_id=binding.get(
                    "organization_billing_account_id"
                ),
                organization_provider_key_binding_id=binding.get(
                    "organization_provider_key_binding_id"
                ),
                organization_id=session.organization_id,
            )
        except funding_q.SessionFundingBindingResourceError as exc:
            credential_id = str(binding.get("credential_id") or "")
            raise ModelCredentialUnavailableError(
                f"The Session model credential {credential_id} is unavailable; create a new Session"
            ) from exc
        return binding

    @staticmethod
    def _legacy_projection(durable_binding, *, version: EffectiveAgentVersion) -> dict[str, Any]:
        expected_provider = runtime_provider_id(
            version.model,
            runtime=version.runtime,
        )
        expected_model_id = runtime_model_id(
            version.model,
            runtime=version.runtime,
        )
        if (
            durable_binding.provider != expected_provider
            or durable_binding.model_id != expected_model_id
        ):
            raise ModelCredentialUnavailableError(
                "The Session funding binding no longer matches its model; create a new Session"
            )
        return {
            "version": 1,
            "source": durable_binding.source,
            "credential_id": durable_binding.model_credential_id,
            "vault_id": durable_binding.vault_id,
            "model_provider": durable_binding.provider,
            "secret_name": runtime_provider_api_key_env(
                version.model,
                runtime=version.runtime,
            ),
            "organization_billing_account_id": (
                durable_binding.organization_billing_account_id
            ),
            "organization_provider_key_binding_id": (
                durable_binding.organization_provider_key_binding_id
            ),
        }


def _same_binding(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare the private coordinates duplicated for wire compatibility."""

    keys = (
        "version",
        "source",
        "credential_id",
        "vault_id",
        "model_provider",
        "secret_name",
        "organization_billing_account_id",
        "organization_provider_key_binding_id",
    )
    return all(left.get(key) == right.get(key) for key in keys)


session_credential_broker = SessionCredentialBroker()
