"""Memory Store metadata and provider Volume lifecycle routes."""

from fastapi import APIRouter, status

from app.db.models import MemoryStore
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.memory import (
    MemoryStoreCreateRequest,
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


@router.get("", response_model=ListResponse[MemoryStoreResponse])
async def list_memory_stores(
    db: Db,
    organization_id: OrganizationId,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    found = await service.list_memory_stores(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(found, to_memory_store)


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


@router.post("/{memory_store_id}/archive", response_model=MemoryStoreResponse)
async def archive_memory_store(
    memory_store_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    store = await service.archive_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    return to_memory_store(store)


@router.delete("/{memory_store_id}", response_model=DeletedResponse)
async def delete_memory_store(
    memory_store_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    store = await service.delete_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    return DeletedResponse(id=store.id)


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
