"""Provision Organization funding from a trusted service-operator environment.

This module writes directly to the control-plane database. It deliberately has
no HTTP route and never accepts a provider API key as a command-line value.
Run it only after Alembic migrations have reached the current head.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Callable, Mapping

from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries import organization_funding as funding_q
from app.organization import resolve_organization_id


DEFAULT_API_KEY_ENV = "VMA_FUNDING_PROVIDER_API_KEY"
_UNSET = object()


class OrganizationFundingProvisionError(RuntimeError):
    """Raised when trusted provisioning cannot safely mutate funding state."""


@dataclass(frozen=True)
class OrganizationFundingProvisionResult:
    organization_id: str
    account_id: str
    account_status: str
    policy: str
    currency: str
    trial_expires_at: datetime | None
    provider_key_binding_id: str
    provider: str
    provider_key_status: str
    upstream_key_id: str | None
    spending_limit_usd_micros: int | None
    provider_key_expires_at: datetime | None
    provider_key_action: str

    def to_dict(self) -> dict[str, Any]:
        """Return operator-safe metadata without secret or ciphertext fields."""

        return {
            "organization_id": self.organization_id,
            "billing_account": {
                "id": self.account_id,
                "status": self.account_status,
                "policy": self.policy,
                "currency": self.currency,
                "trial_expires_at": _format_datetime(self.trial_expires_at),
            },
            "provider_key_binding": {
                "id": self.provider_key_binding_id,
                "provider": self.provider,
                "status": self.provider_key_status,
                "upstream_key_id": self.upstream_key_id,
                "spending_limit_usd_micros": self.spending_limit_usd_micros,
                "expires_at": _format_datetime(self.provider_key_expires_at),
                "action": self.provider_key_action,
            },
        }


async def provision_organization_funding(
    *,
    organization_id: str,
    provider: str,
    api_key: str,
    policy: str | None = None,
    account_status: str | None = None,
    trial_expires_at: datetime | None | object = _UNSET,
    upstream_key_id: str | None | object = _UNSET,
    spending_limit_usd_micros: int | None | object = _UNSET,
    provider_key_expires_at: datetime | None | object = _UNSET,
) -> OrganizationFundingProvisionResult:
    """Create or update one account and create or rotate one provider key row."""

    scoped_organization_id = resolve_organization_id(organization_id)
    _require_secret(api_key)

    async with session_scope() as db:
        organization = await db.get(Organization, scoped_organization_id)
        if organization is None:
            raise OrganizationFundingProvisionError(
                f"Organization {scoped_organization_id} does not exist"
            )
        if organization.archived_at is not None:
            raise OrganizationFundingProvisionError(
                f"Organization {scoped_organization_id} is archived"
            )

        account = await funding_q.load_organization_billing_account(
            db,
            organization_id=scoped_organization_id,
            for_update=True,
        )
        if account is None:
            account = await funding_q.create_organization_billing_account(
                db,
                organization_id=scoped_organization_id,
                policy=policy or "byok_only",
                status=account_status or "active",
                trial_expires_at=(
                    None if trial_expires_at is _UNSET else trial_expires_at
                ),
            )
        else:
            account_updates: dict[str, Any] = {}
            if policy is not None:
                account_updates["policy"] = policy
            if account_status is not None:
                account_updates["status"] = account_status
            if trial_expires_at is not _UNSET:
                account_updates["trial_expires_at"] = trial_expires_at
            if account_updates:
                account = await funding_q.update_organization_billing_account(
                    db,
                    organization_id=scoped_organization_id,
                    **account_updates,
                )

        binding = await funding_q.load_organization_provider_key_binding(
            db,
            organization_id=scoped_organization_id,
            provider=provider,
            for_update=True,
        )
        if binding is None:
            binding = await funding_q.create_organization_provider_key_binding(
                db,
                organization_id=scoped_organization_id,
                organization_billing_account_id=account.id,
                provider=provider,
                api_key=api_key,
                upstream_key_id=(
                    None if upstream_key_id is _UNSET else upstream_key_id
                ),
                spending_limit_usd_micros=(
                    None
                    if spending_limit_usd_micros is _UNSET
                    else spending_limit_usd_micros
                ),
                expires_at=(
                    None
                    if provider_key_expires_at is _UNSET
                    else provider_key_expires_at
                ),
            )
            action = "created"
        else:
            rotate_kwargs: dict[str, Any] = {}
            if upstream_key_id is not _UNSET:
                rotate_kwargs["upstream_key_id"] = upstream_key_id
            if spending_limit_usd_micros is not _UNSET:
                rotate_kwargs["spending_limit_usd_micros"] = (
                    spending_limit_usd_micros
                )
            if provider_key_expires_at is not _UNSET:
                rotate_kwargs["expires_at"] = provider_key_expires_at
            binding = await funding_q.rotate_organization_provider_key_binding(
                db,
                organization_id=scoped_organization_id,
                provider=provider,
                api_key=api_key,
                **rotate_kwargs,
            )
            action = "rotated"

        result = OrganizationFundingProvisionResult(
            organization_id=scoped_organization_id,
            account_id=account.id,
            account_status=account.status,
            policy=account.policy,
            currency=account.currency,
            trial_expires_at=account.trial_expires_at,
            provider_key_binding_id=binding.id,
            provider=binding.provider,
            provider_key_status=binding.status,
            upstream_key_id=binding.upstream_key_id,
            spending_limit_usd_micros=binding.spending_limit_usd_micros,
            provider_key_expires_at=binding.expires_at,
            provider_key_action=action,
        )
        await db.commit()
        return result


def read_provider_api_key(
    *,
    environment_variable: str = DEFAULT_API_KEY_ENV,
    read_stdin: bool = False,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    prompt: Callable[[str], str] | None = None,
) -> str:
    """Read a secret from an environment variable, stdin, or hidden prompt."""

    if read_stdin:
        stream = stdin or sys.stdin
        return _require_secret(stream.readline().rstrip("\r\n"))

    source = environ if environ is not None else os.environ
    api_key = source.get(environment_variable)
    if api_key is not None:
        return _require_secret(api_key)

    hidden_prompt = prompt or getpass.getpass
    return _require_secret(hidden_prompt("Provider API key: "))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update Organization funding and create or rotate one "
            "platform-provider key binding."
        ),
        epilog=(
            f"The provider API key is read from {DEFAULT_API_KEY_ENV} by default. "
            "Use --api-key-env for another environment variable, "
            "--api-key-stdin for stdin, or omit the environment value for a hidden prompt."
        ),
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--policy", choices=sorted(funding_q.FUNDING_POLICIES))
    parser.add_argument(
        "--account-status",
        choices=sorted(funding_q.BILLING_ACCOUNT_STATUSES),
    )

    trial_group = parser.add_mutually_exclusive_group()
    trial_group.add_argument(
        "--trial-expires-at",
        type=_parse_datetime,
        default=_UNSET,
        metavar="ISO8601",
    )
    trial_group.add_argument(
        "--clear-trial-expiry",
        dest="trial_expires_at",
        action="store_const",
        const=None,
    )

    upstream_group = parser.add_mutually_exclusive_group()
    upstream_group.add_argument("--upstream-key-id", default=_UNSET)
    upstream_group.add_argument(
        "--clear-upstream-key-id",
        dest="upstream_key_id",
        action="store_const",
        const=None,
    )

    spending_group = parser.add_mutually_exclusive_group()
    spending_group.add_argument(
        "--spending-limit-usd-micros",
        type=_nonnegative_integer,
        default=_UNSET,
    )
    spending_group.add_argument(
        "--clear-spending-limit",
        dest="spending_limit_usd_micros",
        action="store_const",
        const=None,
    )

    expiry_group = parser.add_mutually_exclusive_group()
    expiry_group.add_argument(
        "--provider-key-expires-at",
        type=_parse_datetime,
        default=_UNSET,
        metavar="ISO8601",
    )
    expiry_group.add_argument(
        "--clear-provider-key-expiry",
        dest="provider_key_expires_at",
        action="store_const",
        const=None,
    )

    secret_group = parser.add_mutually_exclusive_group()
    secret_group.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        metavar="ENVIRONMENT_VARIABLE",
    )
    secret_group.add_argument("--api-key-stdin", action="store_true")
    return parser


async def _run(
    args: argparse.Namespace,
    *,
    api_key: str,
) -> OrganizationFundingProvisionResult:
    return await provision_organization_funding(
        organization_id=args.organization_id,
        provider=args.provider,
        api_key=api_key,
        policy=args.policy,
        account_status=args.account_status,
        trial_expires_at=args.trial_expires_at,
        upstream_key_id=args.upstream_key_id,
        spending_limit_usd_micros=args.spending_limit_usd_micros,
        provider_key_expires_at=args.provider_key_expires_at,
    )


def _parse_datetime(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timestamp must be ISO 8601 with a timezone"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return parsed


def _require_secret(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrganizationFundingProvisionError("Provider API key is required")
    return value


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        api_key = read_provider_api_key(
            environment_variable=args.api_key_env,
            read_stdin=args.api_key_stdin,
        )
        result = asyncio.run(_run(args, api_key=api_key))
    except (
        OrganizationFundingProvisionError,
        funding_q.OrganizationFundingConflictError,
        funding_q.OrganizationFundingUnavailableError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(result.to_dict(), separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
