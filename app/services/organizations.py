"""Organizations, and the one invariant they are created with.

An Organization always has a default Account. A request that names no Account
resolves to it, so an Organization without one has nothing to spend through —
which makes creating the two separately a window where the first is useless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ACCOUNT_ACTIVE,
    MEMBER_ROLE_OWNER,
    Organization,
    OrganizationOnboardingRequest,
)
from app.db.queries import accounts as accounts_q
from app.db.queries import organizations as organizations_q
from app.models.errors import Conflict, InvalidRequest
from app.services import accounts as accounts_service
from app.utils.id_generator import new_id
from app.utils.openrouter_management import OpenRouterKeyAdmin


ONBOARDING_LEASE_DURATION = timedelta(minutes=5)


async def create_organization(
    db: AsyncSession,
    *,
    name: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> Organization:
    """Create an Organization and the Account it spends through.

    The Account is minted here rather than on first use: provisioning calls
    another service, and doing that lazily would put a provider round-trip in
    front of somebody's first session instead of in front of a setup step.
    """
    organization = await organizations_q.create_organization(db, name=name)
    await accounts_service.create_default_account(
        db,
        organization_id=organization.id,
        keys=keys,
    )
    return organization


async def provision_initial_organization(
    db: AsyncSession,
    *,
    requester_user_id: str,
    requester_email: str | None,
    name: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> Organization:
    """Provision the one self-service Organization a new user can create.

    This is deliberately resumable by Supabase user rather than merely by an
    HTTP request id. A Vercel function can time out after OpenRouter has minted
    a key, and the browser can be reloaded before retrying. Neither event may
    create a second Organization or credential.

    The owner membership is the final write. Until the default Account is
    active, normal Organization listing cannot reveal or enter the incomplete
    tenant.
    """
    user_id = requester_user_id.strip()
    display_name = name.strip()
    if not user_id or len(user_id) > 64:
        raise InvalidRequest("Invalid user identity")
    if not display_name or len(display_name) > 255:
        raise InvalidRequest("Organization name must be between 1 and 255 characters")
    if any(ord(character) < 32 for character in display_name):
        raise InvalidRequest("Organization name cannot contain control characters")

    email = _member_email(requester_email)
    request = await organizations_q.get_onboarding_request_for_user(
        db,
        requester_user_id=user_id,
    )
    if request is None:
        if await organizations_q.user_has_active_membership(db, user_id=user_id):
            raise Conflict(
                "Self-service onboarding is only available before joining an Organization"
            )
        try:
            request = await organizations_q.create_onboarding_request(
                db,
                requester_user_id=user_id,
                requester_email=email,
                requested_name=display_name,
            )
            # The request is the durable retry boundary. It must exist before
            # either the tenant row or a provider credential can be created.
            await db.commit()
        except IntegrityError:
            # A duplicate browser submission won the unique user constraint.
            await db.rollback()
            request = await organizations_q.get_onboarding_request_for_user(
                db,
                requester_user_id=user_id,
            )
            if request is None:  # pragma: no cover - defensive database failure
                raise

    if request.requested_name != display_name:
        raise Conflict(
            "Organization onboarding already started with a different name"
        )

    if request.completed_at is not None:
        return await _completed_organization(
            db,
            organization_id=request.organization_id,
            requester_user_id=user_id,
        )

    lease_token = new_id("lease")
    lease_started_at = datetime.now(timezone.utc)
    acquired = await organizations_q.acquire_onboarding_lease(
        db,
        request_id=request.id,
        lease_token=lease_token,
        now=lease_started_at,
        expires_at=lease_started_at + ONBOARDING_LEASE_DURATION,
    )
    if not acquired:
        await db.rollback()
        raise Conflict("Organization provisioning is already in progress; retry shortly")
    await db.commit()
    await db.refresh(request)
    request = await organizations_q.get_onboarding_request_for_user(
        db,
        requester_user_id=user_id,
    )
    if request is None:  # pragma: no cover - defensive database failure
        raise Conflict("The original Organization onboarding record is incomplete")

    try:
        return await _provision_leased_organization(
            db,
            request=request,
            requester_user_id=user_id,
            requester_email=email,
            display_name=display_name,
            lease_token=lease_token,
            keys=keys,
        )
    except BaseException:
        await db.rollback()
        try:
            await organizations_q.release_onboarding_lease(
                db,
                request_id=request.id,
                lease_token=lease_token,
            )
            await db.commit()
        except BaseException:
            # Preserve the provisioning error. A dead worker's lease expires,
            # so failure to clear it cannot permanently strand onboarding.
            await db.rollback()
        raise


async def _provision_leased_organization(
    db: AsyncSession,
    *,
    request: OrganizationOnboardingRequest,
    requester_user_id: str,
    requester_email: str | None,
    display_name: str,
    lease_token: str,
    keys: OpenRouterKeyAdmin | None,
) -> Organization:
    """Finish provisioning while this caller owns the durable user lease."""
    user_id = requester_user_id
    email = requester_email

    if request.organization_id is None:
        # Check again after winning the durable request in case an invitation
        # was accepted concurrently with the first onboarding submission.
        if await organizations_q.user_has_active_membership(db, user_id=user_id):
            raise Conflict(
                "Self-service onboarding is only available before joining an Organization"
            )
        organization = await organizations_q.create_organization(
            db,
            name=display_name,
        )
        request.organization_id = organization.id
        await db.commit()
    else:
        organization = await organizations_q.get_organization(
            db,
            organization_id=request.organization_id,
        )
        if organization is None:
            raise Conflict("The original Organization onboarding record is incomplete")

    try:
        account = await accounts_service.ensure_default_account(
            db,
            organization_id=organization.id,
            keys=keys,
        )
    except IntegrityError as exc:
        # Two identical submissions can both observe the Organization before
        # either sees its default Account. The database prevents two defaults;
        # the later request should retry the same durable onboarding record.
        await db.rollback()
        existing = await accounts_q.get_default_account(
            db,
            organization_id=organization.id,
        )
        if existing is None:
            raise
        raise Conflict(
            "Organization provisioning is already in progress; retry shortly"
        ) from exc

    if account.status != ACCOUNT_ACTIVE or account.credential is None:
        raise Conflict("The Organization default Account is not active")

    # An invitation might have been accepted while the provider Account was
    # being created. Initial onboarding must not turn that user into an owner
    # of a second tenant merely because the slower request started first.
    if await organizations_q.user_has_active_membership(db, user_id=user_id):
        raise Conflict(
            "Self-service onboarding is only available before joining an Organization"
        )

    member = await organizations_q.get_member(
        db,
        organization_id=organization.id,
        user_id=user_id,
    )
    if member is None:
        await organizations_q.add_member(
            db,
            organization_id=organization.id,
            user_id=user_id,
            email=email,
            role=MEMBER_ROLE_OWNER,
        )
    request.completed_at = datetime.now(timezone.utc)
    request.provisioning_lease_token = None
    request.provisioning_lease_expires_at = None
    await db.commit()
    await db.refresh(organization)
    return organization


async def _completed_organization(
    db: AsyncSession,
    *,
    organization_id: str | None,
    requester_user_id: str,
) -> Organization:
    if organization_id is None:
        raise Conflict("The original Organization onboarding record is incomplete")
    organization = await organizations_q.get_organization(
        db,
        organization_id=organization_id,
    )
    membership = await organizations_q.get_member(
        db,
        organization_id=organization_id,
        user_id=requester_user_id,
    )
    if (
        organization is None
        or organization.archived_at is not None
        or membership is None
    ):
        # A removed owner must not be able to restore access by replaying their
        # original onboarding call.
        raise Conflict("Organization onboarding has already been completed")
    return organization


def _member_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized if normalized and len(normalized) <= 255 else None


__all__ = ["create_organization", "provision_initial_organization"]
