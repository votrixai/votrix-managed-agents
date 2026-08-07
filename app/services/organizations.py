"""Organizations, and the one invariant they are created with.

An Organization always has a default Account. A request that names no Account
resolves to it, so an Organization without one has nothing to spend through —
which makes creating the two separately a window where the first is useless.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.db.queries import organizations as organizations_q
from app.utils.openrouter_management import OpenRouterKeyAdmin
from app.services import accounts as accounts_service


async def create_organization(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> Organization:
    """Create an Organization and the Account it spends through.

    The Account is minted here rather than on first use: provisioning calls
    another service, and doing that lazily would put a provider round-trip in
    front of somebody's first session instead of in front of a setup step.
    """
    organization = await organizations_q.create_organization(db, slug=slug, name=name)
    await accounts_service.create_default_account(
        db,
        organization_id=organization.id,
        keys=keys,
    )
    return organization


__all__ = ["create_organization"]
