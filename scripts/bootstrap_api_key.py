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
from app.db.models import Workspace
from app.db.queries import api_keys as api_keys_q


class BootstrapConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    key_id: str
    workspace_id: str
    prefix: str
    secret: str
    workspace_created: bool


async def bootstrap_api_key(
    *,
    workspace_id: str,
    workspace_slug: str | None = None,
    workspace_name: str | None = None,
    key_name: str = "Bootstrap admin",
    allow_additional_admin_key: bool = False,
) -> BootstrapResult:
    workspace_id = _required_text(workspace_id, "workspace_id", max_length=64)
    workspace_slug = _required_text(workspace_slug or workspace_id, "workspace_slug", max_length=255)
    workspace_name = _required_text(workspace_name or workspace_id, "workspace_name", max_length=255)
    key_name = _required_text(key_name, "key_name", max_length=255)

    async with session_scope() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        workspace = result.scalar_one_or_none()
        workspace_created = workspace is None
        if workspace is None:
            workspace = Workspace(
                id=workspace_id,
                slug=workspace_slug,
                name=workspace_name,
                metadata_={"provisioned_by": "bootstrap_api_key"},
            )
            db.add(workspace)
            await db.flush()
        elif workspace.archived_at is not None:
            raise BootstrapConflict(f"Workspace {workspace_id} is archived")

        existing = await api_keys_q.list_api_keys(
            db,
            workspace_id=workspace_id,
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
                f"Workspace {workspace_id} already has an active management key ({prefixes}); "
                "use the authenticated API rotation flow or pass "
                "--allow-additional-admin-key deliberately"
            )

        api_key, secret = await api_keys_q.create_api_key(
            db,
            workspace_id=workspace_id,
            name=key_name,
            scopes=[api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE],
            created_by="bootstrap_api_key",
            metadata={"provisioned_by": "bootstrap_api_key"},
        )
        await db.commit()
        return BootstrapResult(
            key_id=api_key.id,
            workspace_id=workspace_id,
            prefix=api_key.prefix,
            secret=secret,
            workspace_created=workspace_created,
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
        description="Create the first database-backed administrator API key for one workspace."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--workspace-slug")
    parser.add_argument("--workspace-name")
    parser.add_argument("--key-name", default="Bootstrap admin")
    parser.add_argument(
        "--allow-additional-admin-key",
        action="store_true",
        help="Explicitly allow another active management key in the same workspace.",
    )
    return parser


async def _run(args: argparse.Namespace) -> BootstrapResult:
    return await bootstrap_api_key(
        workspace_id=args.workspace_id,
        workspace_slug=args.workspace_slug,
        workspace_name=args.workspace_name,
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
                "workspace_id": result.workspace_id,
                "prefix": result.prefix,
                "secret": result.secret,
                "workspace_created": result.workspace_created,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
