"""Give every Organization the default Account it is supposed to have.

Organizations created before Accounts existed have none, and a Session cannot
open without one — a request that names no Account resolves to the default, so
an Organization missing it cannot run anything at all.

Also finishes the ones that stopped halfway. Creating an Account is a row here
and a key at the provider, which cannot be one transaction, so an interrupted
attempt leaves an Account with nothing to spend through.

Idempotent. An Organization that already has a usable default is left alone,
and no key is minted for it.

Run from a trusted admin environment after `alembic upgrade head`, with
DATABASE_URL, OPENROUTER_MANAGEMENT_KEY and VMA_ENCRYPTION_KEY set:

    python -m scripts.backfill_default_accounts --dry-run
    python -m scripts.backfill_default_accounts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries import accounts as accounts_q
from app.services import accounts as accounts_service


@dataclass
class Result:
    examined: int = 0
    created: int = 0
    already_had: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)


async def _run(*, dry_run: bool, limit: int | None) -> Result:
    result = Result()

    async with session_scope() as db:
        query = select(Organization.id).order_by(Organization.created_at)
        if limit is not None:
            query = query.limit(limit)
        organization_ids = list((await db.execute(query)).scalars().all())

    for organization_id in organization_ids:
        result.examined += 1
        async with session_scope() as db:
            existing = await accounts_q.get_default_account(
                db, organization_id=organization_id
            )
            # Already usable, so there is nothing to mint and nothing to fix.
            if existing is not None and existing.credential is not None:
                result.already_had += 1
                continue

            if dry_run:
                result.created += 1
                continue

            try:
                await accounts_service.ensure_default_account(
                    db, organization_id=organization_id
                )
            except Exception as exc:
                # One Organization's failure is not the others'. A provider key
                # left over from an interrupted attempt stops that Organization
                # and needs a human, and the rest should not wait for one.
                await db.rollback()
                result.failed.append(
                    {
                        "organization_id": organization_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            result.created += 1

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be created without minting anything.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only look at the first N Organizations, oldest first.",
    )
    args = parser.parse_args()

    result = asyncio.run(_run(dry_run=args.dry_run, limit=args.limit))
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "examined": result.examined,
                "created": result.created,
                "already_had": result.already_had,
                "failed": result.failed,
            },
            separators=(",", ":"),
        )
    )
    # Non-zero so a deploy step or a retry loop notices, since a failure here
    # is an Organization that cannot open a Session.
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
