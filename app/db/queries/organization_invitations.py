"""Organization owner invitation persistence and state transitions."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationInvitation
from app.db.queries.organization_owners import add_owner, is_owner
from app.ids import new_id


class PendingInvitationExistsError(Exception):
    """The Organization already has a live invitation for this email."""


class InvitationUnavailableError(Exception):
    """The invitation is unknown, revoked, or expired."""


class InvitationEmailMismatchError(Exception):
    """The signed-in identity does not own the invited email address."""


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def generate_invitation_token() -> str:
    return secrets.token_urlsafe(32)


def hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def default_expires_at(ttl_days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=ttl_days)


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def is_expired(
    invitation: OrganizationInvitation, *, now: datetime | None = None
) -> bool:
    return _as_aware(invitation.expires_at) <= (now or datetime.now(UTC))


def invitation_status(invitation: OrganizationInvitation) -> str:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if is_expired(invitation):
        return "expired"
    return "pending"


async def get_pending_invitation_for_organization_email(
    db: AsyncSession, organization_id: str, email: str
) -> OrganizationInvitation | None:
    result = await db.execute(
        select(OrganizationInvitation).where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.email == normalize_email(email),
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_invitation(
    db: AsyncSession,
    *,
    organization_id: str,
    email: str,
    invited_by_user_id: str,
    token_hash: str,
    expires_at: datetime,
) -> OrganizationInvitation:
    normalized = normalize_email(email)
    now = datetime.now(UTC)
    existing = await get_pending_invitation_for_organization_email(
        db, organization_id, normalized
    )
    if existing is not None:
        if not is_expired(existing, now=now):
            raise PendingInvitationExistsError
        existing.revoked_at = now
        await db.flush()

    invitation = OrganizationInvitation(
        id=new_id("invite"),
        organization_id=organization_id,
        email=normalized,
        role="owner",
        token_hash=token_hash,
        invited_by_user_id=invited_by_user_id,
        expires_at=expires_at,
    )
    db.add(invitation)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise PendingInvitationExistsError from exc
    return invitation


async def list_organization_invitations(
    db: AsyncSession, organization_id: str
) -> list[OrganizationInvitation]:
    result = await db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.organization_id == organization_id)
        .order_by(OrganizationInvitation.created_at.desc())
    )
    return list(result.scalars().all())


async def get_organization_invitation(
    db: AsyncSession,
    organization_id: str,
    invitation_id: str,
    *,
    for_update: bool = False,
) -> OrganizationInvitation | None:
    statement = select(OrganizationInvitation).where(
        OrganizationInvitation.organization_id == organization_id,
        OrganizationInvitation.id == invitation_id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def revoke_invitation(
    db: AsyncSession, organization_id: str, invitation_id: str
) -> OrganizationInvitation | None:
    invitation = await get_organization_invitation(
        db,
        organization_id,
        invitation_id,
        for_update=True,
    )
    if invitation is None or invitation.accepted_at is not None:
        return None
    if invitation.revoked_at is None:
        invitation.revoked_at = datetime.now(UTC)
        await db.flush()
    return invitation


async def prepare_invitation_resend(
    db: AsyncSession,
    organization_id: str,
    invitation_id: str,
    *,
    token_hash: str,
    expires_at: datetime,
) -> OrganizationInvitation | None:
    invitation = await get_organization_invitation(
        db,
        organization_id,
        invitation_id,
        for_update=True,
    )
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
    ):
        return None
    invitation.token_hash = token_hash
    invitation.expires_at = expires_at
    await db.flush()
    return invitation


async def accept_invitation(
    db: AsyncSession,
    *,
    token_hash: str,
    user_id: str,
    user_email: str | None,
) -> OrganizationInvitation:
    result = await db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.token_hash == token_hash)
        .with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if invitation is None or invitation.revoked_at is not None or is_expired(invitation):
        raise InvitationUnavailableError
    if normalize_email(user_email) != invitation.email:
        raise InvitationEmailMismatchError

    already_owner = await is_owner(db, invitation.organization_id, user_id)
    if invitation.accepted_at is not None:
        if already_owner:
            return invitation
        raise InvitationUnavailableError

    if not already_owner:
        await add_owner(
            db,
            organization_id=invitation.organization_id,
            user_id=user_id,
            email=invitation.email,
            granted_by=invitation.invited_by_user_id,
        )
    invitation.accepted_at = datetime.now(UTC)
    await db.flush()
    return invitation
