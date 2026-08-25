"""Private Organization onboarding for the Developer Console control plane.

This router is mounted only by ``app.control_plane``. The public VMA ASGI app
does not import or include it, and the Cloud Run service hosting it keeps the
Invoker IAM check enabled.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app import human_auth
from app.human_auth import AuthenticatedUser
from app.models.organizations import OrganizationCreateRequest, OrganizationResponse
from app.routers.deps import Db
from app.services import organizations as organizations_service


router = APIRouter(
    prefix="/internal/organizations",
    tags=["private organization onboarding"],
    include_in_schema=False,
)

PRIVATE_RESPONSE_HEADERS = {
    "cache-control": "private, no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}


async def authenticate_onboarding_user(
    authorization: Annotated[
        str | None,
        Header(include_in_schema=False),
    ] = None,
) -> AuthenticatedUser:
    """Verify the end user independently of the calling Vercel service.

    Cloud Run consumes ``x-serverless-authorization`` for service IAM. The
    normal ``authorization`` header therefore remains available for this
    Supabase bearer token and is verified again at VMA's trust boundary.
    """
    scheme, _, access_token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not access_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return await human_auth.authenticate_user(access_token)


OnboardingUser = Annotated[AuthenticatedUser, Depends(authenticate_onboarding_user)]


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_initial_organization(
    body: OrganizationCreateRequest,
    response: Response,
    db: Db,
    user: OnboardingUser,
) -> OrganizationResponse:
    response.headers.update(PRIVATE_RESPONSE_HEADERS)
    organization = await organizations_service.provision_initial_organization(
        db,
        requester_user_id=user.id,
        requester_email=user.email,
        name=body.name,
    )
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
        archived_at=organization.archived_at,
    )


__all__ = ["authenticate_onboarding_user", "router"]
