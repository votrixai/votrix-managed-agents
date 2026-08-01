"""Memory Stores, path-addressed Memories, and immutable Versions."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, status

from app.db.models import Memory, MemoryStore, MemoryVersion
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.errors import InvalidRequest
from app.models.memory import (
    DeletedMemoryResponse,
    DeletedMemoryStoreResponse,
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryPrefixResponse,
    MemoryResponse,
    MemoryStoreCreateRequest,
    MemoryStoreListResponse,
    MemoryStoreResponse,
    MemoryStoreUpdateRequest,
    MemoryUpdateRequest,
    MemoryVersionListResponse,
    MemoryVersionResponse,
)
from app.routers.deps import ApiKeyId, Db, OrganizationId
from app.services import memory as service
from app.services import memory_records

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
        after_id = memory_records.decode_page_cursor(
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
            memory_records.encode_page_cursor(
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


@router.delete("/{memory_store_id}", response_model=DeletedMemoryStoreResponse)
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
    return DeletedMemoryStoreResponse(id=store.id)


# --- Memories -------------------------------------------------------------


@router.post(
    "/{memory_store_id}/memories",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_memory(
    memory_store_id: str,
    body: MemoryCreateRequest,
    db: Db,
    organization_id: OrganizationId,
    api_key_id: ApiKeyId,
    view: Literal["basic", "full"] = "full",
):
    memory = await memory_records.create_memory(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        path=body.path,
        content=body.content,
        created_by=memory_records.api_actor(api_key_id),
    )
    return to_memory(memory, view=view)


@router.get(
    "/{memory_store_id}/memories",
    response_model=MemoryListResponse,
)
async def list_memories(
    memory_store_id: str,
    db: Db,
    organization_id: OrganizationId,
    path_prefix: str | None = None,
    depth: int | None = None,
    view: Literal["basic", "full"] = "basic",
    limit: int = DEFAULT_PAGE_SIZE,
    page: str | None = None,
):
    found = await memory_records.list_memories(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        path_prefix=path_prefix,
        depth=depth,
        view=view,
        limit=limit,
        page=page,
    )
    data = []
    for item in found.items:
        if isinstance(item, memory_records.MemoryPrefix):
            data.append(MemoryPrefixResponse(path=item.path))
        else:
            data.append(to_memory(item, view=view))
    return MemoryListResponse(
        data=data,
        next_page=found.next_page,
        has_more=found.has_more,
    )


@router.get(
    "/{memory_store_id}/memories/{memory_id}",
    response_model=MemoryResponse,
)
async def retrieve_memory(
    memory_store_id: str,
    memory_id: str,
    db: Db,
    organization_id: OrganizationId,
    view: Literal["basic", "full"] = "full",
):
    memory = await memory_records.get_memory(
        db,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        organization_id=organization_id,
    )
    return to_memory(memory, view=view)


@router.post(
    "/{memory_store_id}/memories/{memory_id}",
    response_model=MemoryResponse,
)
async def update_memory(
    memory_store_id: str,
    memory_id: str,
    body: MemoryUpdateRequest,
    db: Db,
    organization_id: OrganizationId,
    api_key_id: ApiKeyId,
    view: Literal["basic", "full"] = "full",
):
    memory = await memory_records.update_memory(
        db,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        organization_id=organization_id,
        changes=body.model_dump(exclude_unset=True),
        created_by=memory_records.api_actor(api_key_id),
    )
    return to_memory(memory, view=view)


@router.delete(
    "/{memory_store_id}/memories/{memory_id}",
    response_model=DeletedMemoryResponse,
)
async def delete_memory(
    memory_store_id: str,
    memory_id: str,
    db: Db,
    organization_id: OrganizationId,
    api_key_id: ApiKeyId,
    expected_content_sha256: str | None = None,
):
    deleted_id = await memory_records.delete_memory(
        db,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        organization_id=organization_id,
        expected_content_sha256=expected_content_sha256,
        created_by=memory_records.api_actor(api_key_id),
    )
    return DeletedMemoryResponse(id=deleted_id)


# --- Memory Versions ------------------------------------------------------


@router.get(
    "/{memory_store_id}/memory_versions",
    response_model=MemoryVersionListResponse,
)
async def list_memory_versions(
    memory_store_id: str,
    db: Db,
    organization_id: OrganizationId,
    memory_id: str | None = None,
    operation: str | None = None,
    api_key_id: str | None = None,
    session_id: str | None = None,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    view: Literal["basic", "full"] = "basic",
    limit: int = DEFAULT_PAGE_SIZE,
    page: str | None = None,
):
    found = await memory_records.list_memory_versions(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        memory_id=memory_id,
        operation=operation,
        api_key_id=api_key_id,
        session_id=session_id,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        view=view,
        limit=limit,
        page=page,
    )
    return MemoryVersionListResponse(
        data=[to_memory_version(row, view=view) for row in found.page.items],
        next_page=found.next_page,
        has_more=found.page.has_more,
    )


@router.get(
    "/{memory_store_id}/memory_versions/{memory_version_id}",
    response_model=MemoryVersionResponse,
)
async def retrieve_memory_version(
    memory_store_id: str,
    memory_version_id: str,
    db: Db,
    organization_id: OrganizationId,
    view: Literal["basic", "full"] = "full",
):
    version = await memory_records.get_memory_version(
        db,
        memory_store_id=memory_store_id,
        memory_version_id=memory_version_id,
        organization_id=organization_id,
    )
    return to_memory_version(version, view=view)


@router.post(
    "/{memory_store_id}/memory_versions/{memory_version_id}/redact",
    response_model=MemoryVersionResponse,
)
async def redact_memory_version(
    memory_store_id: str,
    memory_version_id: str,
    db: Db,
    organization_id: OrganizationId,
    api_key_id: ApiKeyId,
):
    version = await memory_records.redact_memory_version(
        db,
        memory_store_id=memory_store_id,
        memory_version_id=memory_version_id,
        organization_id=organization_id,
        redacted_by=memory_records.api_actor(api_key_id),
    )
    return to_memory_version(version, view="full")


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


def to_memory(memory: Memory, *, view: Literal["basic", "full"]) -> MemoryResponse:
    return MemoryResponse(
        id=memory.id,
        memory_store_id=memory.memory_store_id,
        memory_version_id=memory.current_version_id,
        path=memory.path,
        content=memory.content if view == "full" else None,
        content_sha256=memory.content_sha256,
        content_size_bytes=memory.content_size_bytes,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def to_memory_version(
    version: MemoryVersion,
    *,
    view: Literal["basic", "full"],
) -> MemoryVersionResponse:
    content = version.content
    if view == "basic" or version.operation == "deleted" or version.redacted_at:
        content = None
    return MemoryVersionResponse(
        id=version.id,
        memory_store_id=version.memory_store_id,
        memory_id=version.memory_id,
        operation=version.operation,
        created_at=version.created_at,
        content=content,
        content_sha256=version.content_sha256,
        content_size_bytes=version.content_size_bytes,
        path=version.path,
        created_by=version.created_by,
        redacted_at=version.redacted_at,
        redacted_by=version.redacted_by,
    )
