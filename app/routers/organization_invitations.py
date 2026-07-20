"""Hosted owner invitations for the VMA Developer Console."""

from datetime import datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_session
from app.db.models import Organization
from app.db.queries import organization_invitations as invitations_q
from app.human_auth import AuthenticatedUser, require_super_admin, require_user
from app.invitation_email import (
    InvitationEmailDeliveryError,
    send_organization_invitation_email,
)
from app.organization import MissingOrganizationContextError, resolve_organization_id
from app.routers.organizations import OrganizationResponse


class OrganizationInvitationCreateRequest(BaseModel):
    email: EmailStr


class OrganizationInvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class OrganizationInvitationResponse(BaseModel):
    id: str
    organization_id: str
    email: str
    role: Literal["owner"]
    invited_by_user_id: str
    status: Literal["pending", "accepted", "revoked", "expired"]
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


management_router = APIRouter(
    prefix="/v1/me/organizations",
    tags=["hosted organization invitations"],
    dependencies=[Depends(require_super_admin)],
    include_in_schema=False,
)
acceptance_router = APIRouter(
    prefix="/v1/me/invitations",
    tags=["hosted organization invitations"],
    include_in_schema=False,
)


def _invitation_response(invitation) -> OrganizationInvitationResponse:
    return OrganizationInvitationResponse(
        id=invitation.id,
        organization_id=invitation.organization_id,
        email=invitation.email,
        role="owner",
        invited_by_user_id=invitation.invited_by_user_id,
        status=invitations_q.invitation_status(invitation),
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        created_at=invitation.created_at,
        updated_at=invitation.updated_at,
    )


def _organization_response(organization: Organization) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
        metadata=organization.metadata_,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


async def _active_organization(db: AsyncSession, organization_id: str) -> Organization:
    try:
        resolved_organization_id = resolve_organization_id(organization_id)
    except (MissingOrganizationContextError, ValueError):
        raise HTTPException(status_code=404, detail="Organization not found") from None
    organization = await db.get(Organization, resolved_organization_id)
    if organization is None or organization.archived_at is not None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


def _invite_url(token: str) -> str:
    base_url = get_settings().vma_console_base_url.rstrip("/")
    # Keep the bearer token in the URL fragment so browsers do not send it to
    # the Console, reverse proxy, or OAuth provider in an HTTP request.
    return f"{base_url}/invite#token={quote(token, safe='')}"


@management_router.post(
    "/{organization_id}/invitations",
    response_model=OrganizationInvitationResponse,
    status_code=201,
)
async def create_invitation(
    organization_id: str,
    body: OrganizationInvitationCreateRequest,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    organization = await _active_organization(db, organization_id)
    token = invitations_q.generate_invitation_token()
    try:
        invitation = await invitations_q.create_invitation(
            db,
            organization_id=organization.id,
            email=str(body.email),
            invited_by_user_id=super_admin.id,
            token_hash=invitations_q.hash_invitation_token(token),
            expires_at=invitations_q.default_expires_at(
                get_settings().vma_organization_invite_ttl_days
            ),
        )
        await db.commit()
        await db.refresh(invitation)
    except invitations_q.PendingInvitationExistsError:
        raise HTTPException(
            status_code=409,
            detail="That email already has a pending invitation",
        )

    try:
        await send_organization_invitation_email(
            to_email=invitation.email,
            organization_name=organization.name,
            invite_url=_invite_url(token),
            invited_by_email=super_admin.email,
        )
    except InvitationEmailDeliveryError as exc:
        await db.delete(invitation)
        await db.commit()
        raise HTTPException(status_code=503, detail=str(exc))
    return _invitation_response(invitation)


@management_router.get(
    "/{organization_id}/invitations",
    response_model=list[OrganizationInvitationResponse],
)
async def list_invitations(
    organization_id: str,
    db: AsyncSession = Depends(get_session),
):
    organization = await _active_organization(db, organization_id)
    return [
        _invitation_response(invitation)
        for invitation in await invitations_q.list_organization_invitations(
            db, organization.id
        )
    ]


@management_router.delete(
    "/{organization_id}/invitations/{invitation_id}",
    response_model=OrganizationInvitationResponse,
)
async def revoke_invitation(
    organization_id: str,
    invitation_id: str,
    db: AsyncSession = Depends(get_session),
):
    organization = await _active_organization(db, organization_id)
    invitation = await invitations_q.revoke_invitation(
        db, organization.id, invitation_id
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    await db.commit()
    await db.refresh(invitation)
    return _invitation_response(invitation)


@management_router.post(
    "/{organization_id}/invitations/{invitation_id}/resend",
    response_model=OrganizationInvitationResponse,
)
async def resend_invitation(
    organization_id: str,
    invitation_id: str,
    super_admin: AuthenticatedUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_session),
):
    organization = await _active_organization(db, organization_id)
    token = invitations_q.generate_invitation_token()
    invitation = await invitations_q.prepare_invitation_resend(
        db,
        organization.id,
        invitation_id,
        token_hash=invitations_q.hash_invitation_token(token),
        expires_at=invitations_q.default_expires_at(
            get_settings().vma_organization_invite_ttl_days
        ),
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    try:
        await send_organization_invitation_email(
            to_email=invitation.email,
            organization_name=organization.name,
            invite_url=_invite_url(token),
            invited_by_email=super_admin.email,
        )
    except InvitationEmailDeliveryError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail=str(exc))
    await db.commit()
    await db.refresh(invitation)
    return _invitation_response(invitation)


@acceptance_router.post(
    "/accept",
    response_model=OrganizationResponse,
)
async def accept_invitation(
    body: OrganizationInvitationAcceptRequest,
    user: AuthenticatedUser = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    if not user.email or not user.email_verified:
        raise HTTPException(status_code=403, detail="A verified email is required")
    try:
        invitation = await invitations_q.accept_invitation(
            db,
            token_hash=invitations_q.hash_invitation_token(body.token),
            user_id=user.id,
            user_email=user.email,
        )
    except invitations_q.InvitationEmailMismatchError:
        raise HTTPException(
            status_code=403,
            detail="This invitation was sent to a different email",
        )
    except invitations_q.InvitationUnavailableError:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or expired",
        )
    organization = await _active_organization(db, invitation.organization_id)
    await db.commit()
    return _organization_response(organization)
