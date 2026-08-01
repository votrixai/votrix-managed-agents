"""Tenant-scoped Memory Store persistence and provider lifecycle changes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryStore, SessionMemoryStore
from app.db.models.memory import (
    VOLUME_DELETED,
    VOLUME_PROVIDERS,
    VOLUME_PROVISIONING,
    VOLUME_PROVISIONING_STATES,
    VOLUME_PROVIDER_E2B,
)
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id

_UNSET = object()


async def create_memory_store(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    volume_provider: str = VOLUME_PROVIDER_E2B,
) -> MemoryStore:
    """Create the control-plane row before provisioning its external Volume."""
    if volume_provider not in VOLUME_PROVIDERS:
        raise ValueError(f"Unknown Memory Store Volume provider {volume_provider!r}")
    store = MemoryStore(
        id=new_id("memstore"),
        organization_id=organization_id,
        name=name,
        description=description,
        metadata_=dict(metadata or {}),
        volume_provider=volume_provider,
        provisioning_status=VOLUME_PROVISIONING,
    )
    db.add(store)
    await db.flush()
    return store


async def get_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    include_deleted: bool = False,
    for_update: bool = False,
) -> MemoryStore | None:
    stmt = select(MemoryStore).where(
        MemoryStore.id == memory_store_id,
        MemoryStore.organization_id == organization_id,
    )
    if not include_deleted:
        stmt = stmt.where(MemoryStore.deleted_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_memory_stores(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    include_deleted: bool = False,
    provisioning_status: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(MemoryStore).where(MemoryStore.organization_id == organization_id)
    if not include_archived:
        stmt = stmt.where(MemoryStore.archived_at.is_(None))
    if not include_deleted:
        stmt = stmt.where(MemoryStore.deleted_at.is_(None))
    if provisioning_status is not None:
        stmt = stmt.where(MemoryStore.provisioning_status == provisioning_status)
    return await fetch_page(
        db,
        stmt,
        sort=MemoryStore.created_at,
        id_column=MemoryStore.id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def update_memory_store(
    db: AsyncSession,
    store: MemoryStore,
    *,
    name: str | object = _UNSET,
    description: str | object = _UNSET,
    metadata: dict[str, Any] | object = _UNSET,
) -> None:
    if name is not _UNSET:
        store.name = str(name)
    if description is not _UNSET:
        store.description = str(description)
    if metadata is not _UNSET:
        store.metadata_ = dict(cast(dict[str, Any], metadata))
    store.lock_version += 1
    await db.flush()


async def set_volume_state(
    db: AsyncSession,
    store: MemoryStore,
    *,
    status: str,
    volume_locator: dict[str, Any] | None | object = _UNSET,
    error: str | None | object = _UNSET,
) -> None:
    """Record one idempotent provider transition after its side effect finishes."""
    if status not in VOLUME_PROVISIONING_STATES:
        raise ValueError(f"Unknown Memory Store provisioning status {status!r}")
    next_locator = store.volume_locator
    if volume_locator is not _UNSET:
        next_locator = (
            None
            if volume_locator is None
            else dict(cast(dict[str, Any], volume_locator))
        )
    next_error = store.provisioning_error
    if error is not _UNSET:
        next_error = None if error is None else str(error)
    if (
        store.provisioning_status == status
        and store.volume_locator == next_locator
        and store.provisioning_error == next_error
    ):
        return

    store.provisioning_status = status
    store.volume_locator = next_locator
    store.provisioning_error = next_error
    store.lock_version += 1
    await db.flush()


async def archive_memory_store(db: AsyncSession, store: MemoryStore) -> None:
    store.archived_at = datetime.now(timezone.utc)
    store.lock_version += 1
    await db.flush()


async def mark_memory_store_deleted(db: AsyncSession, store: MemoryStore) -> None:
    """Keep a tombstone after the provider resource has been destroyed."""
    store.provisioning_status = VOLUME_DELETED
    store.deleted_at = datetime.now(timezone.utc)
    store.lock_version += 1
    await db.flush()


async def count_memory_store_attachments(
    db: AsyncSession,
    *,
    memory_store_id: str,
) -> int:
    """How many Sessions have ever mounted this Store.

    A provider Volume can outlive Sessions, but destroying one while a paused
    Sandbox still has it mounted is unsafe. Like attached Files, used Stores
    are archived rather than deleted until Sandbox retention has a separate,
    explicit teardown contract.
    """
    result = await db.execute(
        select(func.count())
        .select_from(SessionMemoryStore)
        .where(SessionMemoryStore.memory_store_id == memory_store_id)
    )
    return int(result.scalar_one())
