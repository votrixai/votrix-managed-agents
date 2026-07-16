"""Create the first tenant administrator API key.

Run this only from a trusted migration/admin environment after `alembic upgrade
head`. The generated plaintext is written once to stdout; VMA stores only its
SHA-256 digest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass

from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries import api_keys as api_keys_q
from app.organization import resolve_organization_id


class BootstrapConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    key_id: str
    organization_id: str
    prefix: str
    secret: str
    organization_created: bool


async def bootstrap_api_key(
    *,
    organization_id: str,
    organization_slug: str | None = None,
    organization_name: str | None = None,
    key_name: str = "Bootstrap admin",
    allow_additional_admin_key: bool = False,
) -> BootstrapResult:
    organization_id = resolve_organization_id(
        _required_text(organization_id, "organization_id", max_length=64)
    )
    organization_slug = _required_text(
        organization_slug or organization_id,
        "organization_slug",
        max_length=255,
    )
    organization_name = _required_text(
        organization_name or organization_id,
        "organization_name",
        max_length=255,
    )
    key_name = _required_text(key_name, "key_name", max_length=255)

    async with session_scope() as db:
        result = await db.execute(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        organization = result.scalar_one_or_none()
        organization_created = organization is None
        if organization is None:
            organization = Organization(
                id=organization_id,
                slug=organization_slug,
                name=organization_name,
                metadata_={"provisioned_by": "bootstrap_api_key"},
            )
            db.add(organization)
            await db.flush()
        elif organization.archived_at is not None:
            raise BootstrapConflict(f"Organization {organization_id} is archived")

        existing = await api_keys_q.list_api_keys(
            db,
            organization_id=organization_id,
            include_revoked=False,
        )
        active_admins = [
            item
            for item in existing
            if api_keys_q.API_KEYS_MANAGE_SCOPE in (item.scopes or [])
            and not api_keys_q.api_key_is_expired(item)
        ]
        if active_admins and not allow_additional_admin_key:
            prefixes = ", ".join(item.prefix for item in active_admins)
            raise BootstrapConflict(
                f"Organization {organization_id} already has an active management key ({prefixes}); "
                "use the authenticated API rotation flow or pass "
                "--allow-additional-admin-key deliberately"
            )

        api_key, secret = await api_keys_q.create_api_key(
            db,
            organization_id=organization_id,
            name=key_name,
            scopes=[api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE],
            created_by="bootstrap_api_key",
            metadata={"provisioned_by": "bootstrap_api_key"},
        )
        await db.commit()
        return BootstrapResult(
            key_id=api_key.id,
            organization_id=organization_id,
            prefix=api_key.prefix,
            secret=secret,
            organization_created=organization_created,
        )


def _required_text(value: str, field: str, *, max_length: int) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first database-backed administrator API key for one Organization."
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--organization-slug")
    parser.add_argument("--organization-name")
    parser.add_argument("--key-name", default="Bootstrap admin")
    parser.add_argument(
        "--allow-additional-admin-key",
        action="store_true",
        help="Explicitly allow another active management key in the same Organization.",
    )
    return parser


async def _run(args: argparse.Namespace) -> BootstrapResult:
    return await bootstrap_api_key(
        organization_id=args.organization_id,
        organization_slug=args.organization_slug,
        organization_name=args.organization_name,
        key_name=args.key_name,
        allow_additional_admin_key=args.allow_additional_admin_key,
    )


def main() -> None:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (BootstrapConflict, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    # This is the sole plaintext emission. Redirect stdout directly into the
    # intended secret manager and do not persist it in shell history or logs.
    print(
        json.dumps(
            {
                "id": result.key_id,
                "organization_id": result.organization_id,
                "prefix": result.prefix,
                "secret": result.secret,
                "organization_created": result.organization_created,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
