from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


class MissingOrganizationContextError(RuntimeError):
    """Raised when tenant-scoped work has no explicitly installed Organization."""


_current_organization: ContextVar["CurrentOrganization | None"] = ContextVar(
    "current_organization",
    default=None,
)


@dataclass(frozen=True)
class CurrentOrganization:
    id: str
    slug: str = ""
    source: str = "explicit"
    api_key_id: str | None = None
    # Hosted/custom auth providers pre-date database API-key scopes. Keep their
    # existing behavior unless they deliberately return a narrower set.
    scopes: frozenset[str] = field(
        default_factory=lambda: frozenset({"api", "api_keys:manage", "worker"})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validate_organization_id(self.id))


def resolve_organization_id(value: str | None = None) -> str:
    """Resolve an explicit ID or the explicitly installed request/worker context."""
    if value is not None:
        return _validate_organization_id(value)
    return current_organization().id


def set_current_organization(
    organization: CurrentOrganization,
) -> Token[CurrentOrganization | None]:
    return _current_organization.set(organization)


def reset_current_organization(token: Token[CurrentOrganization | None]) -> None:
    _current_organization.reset(token)


def current_organization() -> CurrentOrganization:
    organization = _current_organization.get()
    if organization is None:
        raise MissingOrganizationContextError(
            "Organization context is required for tenant-scoped work"
        )
    return organization


def _validate_organization_id(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise MissingOrganizationContextError("organization_id is required")
    if re.fullmatch(r"org_[A-Za-z0-9][A-Za-z0-9._=-]{0,59}", normalized) is None:
        raise ValueError("organization_id must be an explicit org_* identifier")
    if normalized.removeprefix("org_") == "default":
        raise ValueError("organization_id is reserved and cannot be used")
    return normalized
