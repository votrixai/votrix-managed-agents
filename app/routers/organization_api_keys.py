"""Organization API-key management for the first-party Console."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.db.models import VMA_API_SCOPE, VmaApiKey
from app.db.queries import vma_api_keys as api_keys_q
from app.models.organization_api_keys import (
    OrganizationApiKeyCreated,
    OrganizationApiKeyCreateRequest,
    OrganizationApiKeyCreateResponse,
    OrganizationApiKeyListResponse,
    OrganizationApiKeyMetadata,
    OrganizationApiKeyRevokedResponse,
)
from app.routers.deps import ConsoleIdentity, ConsolePrincipal, Db

router = APIRouter(
    prefix="/v1/me/api-keys",
    tags=["developer-console"],
    include_in_schema=False,
)

PRIVATE_RESPONSE_HEADERS = {
    "cache-control": "private, no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}


def _mark_private(response: Response) -> None:
    response.headers.update(PRIVATE_RESPONSE_HEADERS)


def _is_active_key(api_key: VmaApiKey) -> bool:
    return (
        api_key.revoked_at is None
        and not api_keys_q.vma_api_key_is_expired(api_key)
    )


def _can_revoke(api_key: VmaApiKey, principal: ConsolePrincipal) -> bool:
    return principal.can_manage_all_api_keys or api_key.created_by == principal.user_id


def _metadata(
    api_key: VmaApiKey,
    principal: ConsolePrincipal,
) -> OrganizationApiKeyMetadata:
    return OrganizationApiKeyMetadata(
        organization_id=api_key.organization_id,
        id=api_key.id,
        name=api_key.name,
        prefix=api_key.prefix,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        can_revoke=_can_revoke(api_key, principal),
    )


@router.get("", response_model=OrganizationApiKeyListResponse)
async def list_organization_api_keys(
    response: Response,
    db: Db,
    principal: ConsoleIdentity,
) -> OrganizationApiKeyListResponse:
    _mark_private(response)
    keys = await api_keys_q.list_vma_api_keys(
        db,
        organization_id=principal.organization_id,
        include_revoked=False,
    )
    return OrganizationApiKeyListResponse(
        data=[
            _metadata(api_key, principal)
            for api_key in keys
            if _is_active_key(api_key)
        ]
    )


@router.post(
    "",
    response_model=OrganizationApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_api_key(
    body: OrganizationApiKeyCreateRequest,
    response: Response,
    db: Db,
    principal: ConsoleIdentity,
) -> OrganizationApiKeyCreateResponse:
    _mark_private(response)
    api_key, plaintext = await api_keys_q.create_vma_api_key(
        db,
        organization_id=principal.organization_id,
        name=body.name,
        scopes=[VMA_API_SCOPE],
        created_by=principal.user_id,
        metadata={"created_via": "developer_console"},
    )
    await db.commit()
    return OrganizationApiKeyCreateResponse(
        data=OrganizationApiKeyCreated(
            **_metadata(api_key, principal).model_dump(),
            api_key=plaintext,
        )
    )


@router.delete(
    "/{api_key_id}",
    response_model=OrganizationApiKeyRevokedResponse,
)
async def revoke_organization_api_key(
    api_key_id: str,
    response: Response,
    db: Db,
    principal: ConsoleIdentity,
) -> OrganizationApiKeyRevokedResponse:
    _mark_private(response)
    api_key = await api_keys_q.get_vma_api_key(
        db,
        organization_id=principal.organization_id,
        key_id=api_key_id,
    )
    if api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if not _can_revoke(api_key, principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API key access denied")
    await api_keys_q.revoke_vma_api_key(
        db,
        api_key,
        revoked_by=principal.user_id,
        reason="Revoked from Developer Console",
    )
    await db.commit()
    return OrganizationApiKeyRevokedResponse(id=api_key.id)
