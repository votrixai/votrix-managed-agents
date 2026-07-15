from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_workspace, require_api_access
from app.db.engine import get_session
from app.db.queries import api_keys as api_keys_q
from app.metadata import normalize_metadata
from app.models.api_keys import (
    ApiKeyCreateRequest,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeyRevokeRequest,
    ApiKeyRotateRequest,
    api_key_to_created_response,
    api_key_to_response,
)
from app.models.common import ListResponse
from app.pagination import paginate
from app.workspace import CurrentWorkspace

router = APIRouter(
    prefix="/v1/api_keys",
    tags=["api keys"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest,
    workspace: CurrentWorkspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_session),
):
    _validate_future_expiration(body.expires_at)
    api_key, secret = await api_keys_q.create_api_key(
        db,
        name=body.name,
        workspace_id=workspace.id,
        scopes=body.scopes,
        expires_at=body.expires_at,
        created_by=_actor_id(workspace),
        metadata=normalize_metadata(body.metadata),
    )
    await db.commit()
    return api_key_to_created_response(api_key, secret)


@router.get("", response_model=ListResponse[ApiKeyResponse])
async def list_api_keys(
    limit: int = Query(default=50, ge=1, le=100),
    page: str | None = None,
    include_revoked: bool = True,
    db: AsyncSession = Depends(get_session),
):
    api_keys = await api_keys_q.list_api_keys(db, include_revoked=include_revoked)
    return paginate([api_key_to_response(item) for item in api_keys], limit=limit, page=page, max_limit=100)


@router.get("/{key_id}", response_model=ApiKeyResponse)
async def retrieve_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_session),
):
    api_key = await api_keys_q.get_api_key(db, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return api_key_to_response(api_key)


@router.post("/{key_id}/revoke", response_model=ApiKeyResponse)
async def revoke_api_key(
    key_id: str,
    body: ApiKeyRevokeRequest | None = None,
    workspace: CurrentWorkspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_session),
):
    api_key = await api_keys_q.get_api_key(db, key_id, for_update=True)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await api_keys_q.revoke_api_key(
        db,
        api_key,
        revoked_by=_actor_id(workspace),
        reason=body.reason if body is not None else None,
    )
    await db.commit()
    return api_key_to_response(api_key)


@router.post("/{key_id}/rotate", response_model=ApiKeyCreatedResponse, status_code=201)
async def rotate_api_key(
    key_id: str,
    body: ApiKeyRotateRequest | None = None,
    workspace: CurrentWorkspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_session),
):
    api_key = await api_keys_q.get_api_key(db, key_id, for_update=True)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is not None or api_key.archived_at is not None:
        raise HTTPException(status_code=409, detail="Revoked API keys cannot be rotated")

    request = body or ApiKeyRotateRequest()
    expires_at = request.expires_at if "expires_at" in request.model_fields_set else api_key.expires_at
    _validate_future_expiration(expires_at)
    replacement, secret = await api_keys_q.create_api_key(
        db,
        name=api_key.name,
        workspace_id=api_key.workspace_id,
        scopes=api_key.scopes,
        expires_at=expires_at,
        created_by=_actor_id(workspace),
        replaces_key_id=api_key.id,
        metadata=dict(api_key.metadata_ or {}),
    )
    await api_keys_q.revoke_api_key(
        db,
        api_key,
        revoked_by=_actor_id(workspace),
        reason=request.reason,
        replaced_by_key_id=replacement.id,
    )
    await db.commit()
    return api_key_to_created_response(replacement, secret)


def _actor_id(workspace: CurrentWorkspace) -> str:
    return workspace.api_key_id or workspace.source


def _validate_future_expiration(expires_at: datetime | None) -> None:
    if expires_at is None:
        return
    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
    if normalized <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
