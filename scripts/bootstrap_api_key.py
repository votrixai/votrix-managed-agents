"""Create the first tenant administrator API key.

Run this only from a trusted migration/admin environment after `alembic upgrade
head`. By default the generated plaintext is written once to stdout; the GCP
operator flow instead supplies a pre-provisioned key on stdin and redacts it
from stdout. VMA stores only its SHA-256 digest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
from dataclasses import dataclass

from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries import vma_api_keys as api_keys_q


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
    allow_legacy_import: bool = False,
    api_key: str | None = None,
) -> BootstrapResult:
    organization_id = _organization_id(organization_id)
    organization_slug = _required_text(
        organization_slug or organization_id,
        "organization_slug",
        max_length=64,
    )
    organization_name = _required_text(
        organization_name or organization_id,
        "organization_name",
        max_length=255,
    )
    key_name = _required_text(key_name, "key_name", max_length=255)
    legacy_api_key = False
    if api_key is not None:
        api_key = _required_text(api_key, "api_key", max_length=512)
        legacy_api_key = api_keys_q.is_legacy_vma_api_key(api_key)
        if not legacy_api_key:
            api_keys_q.validate_vma_api_key_prefix(api_key)

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
            )
            db.add(organization)
            await db.flush()
        elif organization.archived_at is not None:
            raise BootstrapConflict(f"Organization {organization_id} is archived")

        existing = await api_keys_q.list_vma_api_keys(
            db,
            organization_id=organization_id,
            include_revoked=True,
        )
        active_admins = [
            item
            for item in existing
            if item.revoked_at is None
            and api_keys_q.VMA_API_KEYS_MANAGE_SCOPE in (item.scopes or [])
            and not api_keys_q.vma_api_key_is_expired(item)
        ]
        if api_key is not None:
            supplied_hash = api_keys_q.hash_vma_api_key(api_key)
            matching_admin = next(
                (
                    item
                    for item in active_admins
                    if secrets.compare_digest(item.key_hash, supplied_hash)
                ),
                None,
            )
            if matching_admin is not None:
                return BootstrapResult(
                    key_id=matching_admin.id,
                    organization_id=organization_id,
                    prefix=matching_admin.prefix,
                    secret=api_key,
                    organization_created=False,
                )
        if legacy_api_key:
            if not allow_legacy_import:
                raise ValueError(
                    "legacy environment-prefixed API key may only reuse an existing "
                    "active management key unless --allow-legacy-import is explicitly set"
                )
            if existing:
                raise BootstrapConflict(
                    "legacy environment-prefixed VMA API keys can only be imported "
                    "before the Organization has any key rows"
                )
        if active_admins and not allow_additional_admin_key:
            prefixes = ", ".join(item.prefix for item in active_admins)
            raise BootstrapConflict(
                f"Organization {organization_id} already has an active management key ({prefixes}); "
                "use the authenticated API rotation flow or pass "
                "--allow-additional-admin-key deliberately"
            )

        api_key, secret = await api_keys_q.create_vma_api_key(
            db,
            organization_id=organization_id,
            name=key_name,
            token=api_key,
            allow_legacy_token=legacy_api_key and allow_legacy_import,
            scopes=[
                api_keys_q.VMA_API_SCOPE,
                api_keys_q.VMA_API_KEYS_MANAGE_SCOPE,
            ],
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


def _organization_id(value: str) -> str:
    normalized = _required_text(value, "organization_id", max_length=64)
    if re.fullmatch(r"org_[A-Za-z0-9][A-Za-z0-9._=-]{0,59}", normalized) is None:
        raise ValueError("organization_id must be an explicit org_* identifier")
    if normalized == "org_default":
        raise ValueError("organization_id is reserved and cannot be used")
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
    parser.add_argument(
        "--allow-legacy-import",
        action="store_true",
        help=(
            "Allow a trusted, existing vma_live_* or vma_test_* secret to seed an "
            "Organization that has no API-key rows. This is a one-time migration "
            "escape hatch."
        ),
    )
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help=(
            "Read a pre-generated VMA API key from stdin. "
            "This makes bootstrap idempotent when an operator secret is provisioned "
            "before the database row."
        ),
    )
    parser.add_argument(
        "--redact-secret",
        action="store_true",
        help="Omit the plaintext key from stdout; requires --api-key-stdin.",
    )
    return parser


async def _run(args: argparse.Namespace) -> BootstrapResult:
    api_key = None
    if args.api_key_stdin:
        api_key = sys.stdin.read().strip()
    return await bootstrap_api_key(
        organization_id=args.organization_id,
        organization_slug=args.organization_slug,
        organization_name=args.organization_name,
        key_name=args.key_name,
        allow_additional_admin_key=args.allow_additional_admin_key,
        allow_legacy_import=args.allow_legacy_import,
        api_key=api_key,
    )


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.redact_secret and not args.api_key_stdin:
        parser.error("--redact-secret requires --api-key-stdin")
    try:
        result = asyncio.run(_run(args))
    except (BootstrapConflict, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    # This is the sole optional plaintext emission. Redirect stdout directly
    # into the intended secret sink and do not persist it in shell history or
    # logs. The GCP flow uses --redact-secret because its key already lives in
    # Secret Manager.
    payload = {
        "id": result.key_id,
        "organization_id": result.organization_id,
        "prefix": result.prefix,
        "organization_created": result.organization_created,
    }
    if not args.redact_secret:
        payload["secret"] = result.secret
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
