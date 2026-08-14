"""Memory Store lifecycle and path-addressed provider filesystem writes."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Response, status

from app.db.models import MemoryStore
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.errors import InvalidRequest
from app.models.memory import (
    MemoryStoreCreateRequest,
    MemoryStoreFileResponse,
    MemoryStoreListResponse,
    MemoryStoreResponse,
    MemoryStoreUpdateRequest,
)
from app.routers.deps import Db, OrganizationId
from app.services import memory as service

router = APIRouter(prefix="/v1/memory_stores", tags=["memory-stores"])


@router.post("", response_model=MemoryStoreResponse, status_code=status.HTTP_201_CREATED)
async def create_memory_store(
    body: MemoryStoreCreateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    store = await service.create_memory_store(
        db,
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        metadata=body.metadata,
    )
    return to_memory_store(store)


@router.get("", response_model=MemoryStoreListResponse)
async def list_memory_stores(
    db: Db,
    organization_id: OrganizationId,
    include_archived: bool = False,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    limit: int = DEFAULT_PAGE_SIZE,
    page: str | None = None,
    before_id: str | None = None,
    after_id: str | None = None,
):
    if not 1 <= limit <= 100:
        raise InvalidRequest("limit must be between 1 and 100")
    if page is not None:
        if before_id is not None or after_id is not None:
            raise InvalidRequest("page cannot be combined with before_id or after_id")
        after_id = service.decode_page_cursor(
            page,
            kind=f"memory_store:{organization_id}",
        )
    found = await service.list_memory_stores(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return MemoryStoreListResponse(
        data=[to_memory_store(row) for row in found.items],
        next_page=(
            service.encode_page_cursor(
                found.last_id,
                kind=f"memory_store:{organization_id}",
            )
            if found.has_more and found.last_id
            else None
        ),
        has_more=found.has_more,
        first_id=found.first_id,
        last_id=found.last_id,
    )


@router.get("/{memory_store_id}", response_model=MemoryStoreResponse)
async def retrieve_memory_store(
    memory_store_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    store = await service.get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    return to_memory_store(store)


@router.post("/{memory_store_id}", response_model=MemoryStoreResponse)
@router.patch("/{memory_store_id}", response_model=MemoryStoreResponse)
async def update_memory_store(
    memory_store_id: str,
    body: MemoryStoreUpdateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    store = await service.update_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        changes=body.model_dump(exclude_unset=True),
    )
    return to_memory_store(store)


@router.put(
    "/{memory_store_id}/files/{file_path:path}",
    response_model=MemoryStoreFileResponse,
)
async def put_memory_store_file(
    memory_store_id: str,
    file_path: Annotated[
        str,
        Path(
            min_length=1,
            description="Relative path of the file inside the Memory Store.",
        ),
    ],
    content: Annotated[
        bytes,
        Body(
            media_type="application/octet-stream",
            description="Raw bytes that create or replace the file.",
        ),
    ],
    db: Db,
    organization_id: OrganizationId,
):
    written = await service.put_memory_store_file(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        path=file_path,
        content=content,
    )
    return MemoryStoreFileResponse(
        memory_store_id=written.memory_store_id,
        path=written.path,
        size_bytes=written.size_bytes,
        sha256=written.sha256,
    )


@router.delete(
    "/{memory_store_id}/files/{file_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_memory_store_file(
    memory_store_id: str,
    file_path: Annotated[
        str,
        Path(
            min_length=1,
            description="Relative path of the file inside the Memory Store.",
        ),
    ],
    db: Db,
    organization_id: OrganizationId,
) -> Response:
    await service.delete_memory_store_file(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        path=file_path,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def to_memory_store(store: MemoryStore) -> MemoryStoreResponse:
    """Provider ids and provisioning failures stay inside the control plane."""
    return MemoryStoreResponse(
        id=store.id,
        name=store.name,
        description=store.description,
        metadata=store.metadata_,
        created_at=store.created_at,
        updated_at=store.updated_at,
        archived_at=store.archived_at,
    )
