import re
from dataclasses import dataclass, replace
from typing import Annotated, Protocol, runtime_checkable

from fastapi import Header, HTTPException, Request

from app.config import get_settings
from app.db.queries.api_keys import API_KEYS_MANAGE_SCOPE, API_SCOPE, WORKER_SCOPE
from app.organization import (
    CurrentOrganization,
    MissingOrganizationContextError,
    current_organization,
    reset_current_organization,
    resolve_organization_id,
    set_current_organization,
)

CMA_MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
VOTRIX_MANAGED_AGENTS_BETA = "votrix-managed-agents-2026-04-01"
ANTHROPIC_SKILLS_BETA = "skills-2025-10-02"
ANTHROPIC_USER_PROFILES_BETA = "user-profiles-2026-03-24"
ANTHROPIC_AGENT_MEMORY_BETA = "agent-memory-2026-07-22"
ACCEPTED_MANAGED_AGENTS_BETAS = {
    CMA_MANAGED_AGENTS_BETA,
    VOTRIX_MANAGED_AGENTS_BETA,
    ANTHROPIC_SKILLS_BETA,
    ANTHROPIC_USER_PROFILES_BETA,
    ANTHROPIC_AGENT_MEMORY_BETA,
}
ANTHROPIC_API_VERSION = "2023-06-01"


@dataclass(frozen=True)
class RequestCredentials:
    x_api_key: str | None
    authorization: str | None


@runtime_checkable
class AuthProvider(Protocol):
    async def authenticate(self, request: Request, credentials: RequestCredentials) -> CurrentOrganization:
        ...


class DatabaseApiKeyAuthProvider:
    async def authenticate(self, request: Request, credentials: RequestCredentials) -> CurrentOrganization:
        from app.db.engine import session_scope
        from app.db.models import Organization
        from app.db.queries import api_keys as api_keys_q

        token = credentials.x_api_key or _bearer_token(credentials.authorization)
        if not token:
            raise HTTPException(status_code=401, detail="Missing API key")

        async with session_scope() as db:
            api_key = await api_keys_q.get_api_key_by_token(db, token)
            if api_key is None or api_keys_q.api_key_is_expired(api_key):
                raise HTTPException(status_code=401, detail="Invalid API key")
            try:
                organization_id = resolve_organization_id(api_key.organization_id)
            except (MissingOrganizationContextError, ValueError) as exc:
                raise HTTPException(status_code=401, detail="Invalid API key") from exc
            organization = await db.get(Organization, organization_id)
            if organization is None or organization.archived_at is not None:
                raise HTTPException(status_code=401, detail="Invalid API key")
            await api_keys_q.touch_api_key(db, api_key)
            await db.commit()
            return CurrentOrganization(
                id=organization_id,
                slug=organization.slug,
                source="database_api_key",
                api_key_id=api_key.id,
                scopes=frozenset(api_key.scopes or ()),
            )


def default_auth_provider() -> AuthProvider:
    """Use the same fail-closed tenant-key authentication in every environment."""
    return DatabaseApiKeyAuthProvider()


async def require_api_access(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
    authorization: Annotated[str | None, Header(alias="authorization")] = None,
    anthropic_beta: Annotated[
        str | None,
        Header(alias="anthropic-beta", include_in_schema=False),
    ] = None,
    votrix_managed_agents_beta: Annotated[
        str | None,
        Header(
            alias="votrix-managed-agents-beta",
            description=(
                "Votrix Managed Agents preview selector. "
                f"Use `{VOTRIX_MANAGED_AGENTS_BETA}`."
            ),
        ),
    ] = None,
    anthropic_version: Annotated[
        str | None,
        Header(alias="anthropic-version", include_in_schema=False),
    ] = None,
):
    settings = get_settings()

    anthropic_beta_values = _split_header_values(anthropic_beta)
    native_beta_values = _split_header_values(votrix_managed_agents_beta)
    beta_values = anthropic_beta_values | native_beta_values

    if settings.vma_require_beta_header:
        if not beta_values.intersection(ACCEPTED_MANAGED_AGENTS_BETAS):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Missing required beta header: "
                    f"{CMA_MANAGED_AGENTS_BETA}, {VOTRIX_MANAGED_AGENTS_BETA}, "
                    f"{ANTHROPIC_SKILLS_BETA}, {ANTHROPIC_USER_PROFILES_BETA}, "
                    f"or {ANTHROPIC_AGENT_MEMORY_BETA}"
                ),
            )

    compatibility_mode = bool(anthropic_beta_values.intersection(ACCEPTED_MANAGED_AGENTS_BETAS))
    if settings.vma_require_anthropic_version_header and compatibility_mode and not anthropic_version:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required Anthropic API version header: {ANTHROPIC_API_VERSION}",
        )

    provider: AuthProvider = getattr(
        request.app.state,
        "auth_provider",
        DatabaseApiKeyAuthProvider(),
    )
    organization = await provider.authenticate(
        request,
        RequestCredentials(x_api_key=x_api_key, authorization=authorization),
    )
    organization = await _require_active_organization(organization)
    # Keep the authenticated actor on the request even when a later scope or
    # quota check rejects it. The HTTP audit middleware can then attribute the
    # denial without relying on a context variable that is only installed for
    # successful dependencies.
    request.state.current_organization = organization
    required_scope = required_scope_for_request(request)
    if required_scope not in organization.scopes:
        raise HTTPException(
            status_code=403,
            detail=f"API key is missing required scope: {required_scope}",
        )
    if settings.vma_governance_enabled:
        from app.governance_runtime import governance_service, rate_limit_headers

        decision = await governance_service().authorize_request(
            organization.id,
            actor_type=organization.source,
            actor_id=organization.api_key_id,
            request_id=getattr(request.state, "request_id", None),
            method=request.method,
            path=request.url.path,
        )
        request.state.rate_limit_decision = decision
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "code": "request_quota_exceeded",
                        "message": "Organization request rate limit exceeded",
                    },
                },
                headers=rate_limit_headers(decision),
            )
    token = set_current_organization(organization)
    try:
        yield organization
    finally:
        reset_current_organization(token)


async def get_current_organization(request: Request) -> CurrentOrganization:
    organization = getattr(request.state, "current_organization", None)
    if organization is not None:
        return organization
    return current_organization()


async def _require_active_organization(
    organization: CurrentOrganization,
) -> CurrentOrganization:
    """Bind every authentication provider to a real, active Organization row."""
    from app.db.engine import session_scope
    from app.db.models import Organization

    try:
        organization_id = resolve_organization_id(organization.id)
    except (MissingOrganizationContextError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid API key") from exc
    async with session_scope() as db:
        stored = await db.get(Organization, organization_id)
    if stored is None or stored.archived_at is not None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return replace(organization, id=organization_id, slug=stored.slug)


def _split_header_values(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None


def required_scope_for_request(request: Request) -> str:
    path = request.url.path.rstrip("/")
    if path == "/v1/api_keys" or path.startswith("/v1/api_keys/"):
        return API_KEYS_MANAGE_SCOPE
    if re.fullmatch(r"/v1/environments/[^/]+/work(?:/.*)?", path):
        return WORKER_SCOPE
    return API_SCOPE
