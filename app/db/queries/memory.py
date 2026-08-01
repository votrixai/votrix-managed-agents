"""Tenant-scoped Memory Store persistence and provider lifecycle changes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, MemoryStore, MemoryVersion, SessionMemoryStore
from app.db.models.memory import (
    MEMORY_VERSION_OPERATIONS,
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
    created_at_gte: datetime | None = None,
    created_at_lte: datetime | None = None,
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
    if created_at_gte is not None:
        stmt = stmt.where(MemoryStore.created_at >= created_at_gte)
    if created_at_lte is not None:
        stmt = stmt.where(MemoryStore.created_at <= created_at_lte)
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


async def purge_memory_store_contents(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> None:
    """Permanently remove live heads and audit history with their Store."""
    await db.execute(
        delete(MemoryVersion).where(
            MemoryVersion.memory_store_id == memory_store_id,
            MemoryVersion.organization_id == organization_id,
        )
    )
    await db.execute(
        delete(Memory).where(
            Memory.memory_store_id == memory_store_id,
            Memory.organization_id == organization_id,
        )
    )
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


# --- Memories -------------------------------------------------------------


async def get_memory(
    db: AsyncSession,
    *,
    memory_id: str,
    memory_store_id: str,
    organization_id: str,
    for_update: bool = False,
) -> Memory | None:
    stmt = select(Memory).where(
        Memory.id == memory_id,
        Memory.memory_store_id == memory_store_id,
        Memory.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_memory_by_path(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path: str,
    for_update: bool = False,
) -> Memory | None:
    stmt = select(Memory).where(
        Memory.memory_store_id == memory_store_id,
        Memory.organization_id == organization_id,
        Memory.path == path,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_memories(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path_prefix: str | None = None,
) -> list[Memory]:
    stmt = select(Memory).where(
        Memory.memory_store_id == memory_store_id,
        Memory.organization_id == organization_id,
    )
    if path_prefix and path_prefix != "/":
        escaped = (
            path_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        stmt = stmt.where(Memory.path.like(f"{escaped}%", escape="\\"))
    result = await db.execute(stmt.order_by(Memory.path.asc(), Memory.id.asc()))
    return list(result.scalars().all())


async def count_memories(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(
            Memory.memory_store_id == memory_store_id,
            Memory.organization_id == organization_id,
        )
    )
    return int(result.scalar_one())


async def create_memory_with_version(
    db: AsyncSession,
    *,
    organization_id: str,
    memory_store_id: str,
    path: str,
    content: str,
    content_sha256: str,
    content_size_bytes: int,
    created_by: dict[str, str] | None,
) -> tuple[Memory, MemoryVersion]:
    memory_id = new_id("mem")
    version_id = new_id("memver")
    memory = Memory(
        id=memory_id,
        organization_id=organization_id,
        memory_store_id=memory_store_id,
        path=path,
        content=content,
        content_sha256=content_sha256,
        content_size_bytes=content_size_bytes,
        current_version_id=version_id,
    )
    version = _new_memory_version(
        version_id=version_id,
        organization_id=organization_id,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        operation="created",
        path=path,
        content=content,
        content_sha256=content_sha256,
        content_size_bytes=content_size_bytes,
        created_by=created_by,
    )
    db.add_all((memory, version))
    await db.flush()
    return memory, version


async def update_memory_with_version(
    db: AsyncSession,
    memory: Memory,
    *,
    path: str,
    content: str,
    content_sha256: str,
    content_size_bytes: int,
    created_by: dict[str, str] | None,
) -> MemoryVersion:
    version_id = new_id("memver")
    memory.path = path
    memory.content = content
    memory.content_sha256 = content_sha256
    memory.content_size_bytes = content_size_bytes
    memory.current_version_id = version_id
    memory.lock_version += 1
    version = _new_memory_version(
        version_id=version_id,
        organization_id=memory.organization_id,
        memory_store_id=memory.memory_store_id,
        memory_id=memory.id,
        operation="modified",
        path=path,
        content=content,
        content_sha256=content_sha256,
        content_size_bytes=content_size_bytes,
        created_by=created_by,
    )
    db.add(version)
    await db.flush()
    return version


async def delete_memory_with_version(
    db: AsyncSession,
    memory: Memory,
    *,
    created_by: dict[str, str] | None,
) -> MemoryVersion:
    version = _new_memory_version(
        version_id=new_id("memver"),
        organization_id=memory.organization_id,
        memory_store_id=memory.memory_store_id,
        memory_id=memory.id,
        operation="deleted",
        path=memory.path,
        content=None,
        content_sha256=None,
        content_size_bytes=None,
        created_by=created_by,
    )
    db.add(version)
    await db.delete(memory)
    await db.flush()
    return version


# --- Memory Versions ------------------------------------------------------


async def get_memory_version(
    db: AsyncSession,
    *,
    memory_version_id: str,
    memory_store_id: str,
    organization_id: str,
    for_update: bool = False,
) -> MemoryVersion | None:
    stmt = select(MemoryVersion).where(
        MemoryVersion.id == memory_version_id,
        MemoryVersion.memory_store_id == memory_store_id,
        MemoryVersion.organization_id == organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_memory_versions(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    memory_id: str | None = None,
    operation: str | None = None,
    api_key_id: str | None = None,
    session_id: str | None = None,
    created_at_gte: datetime | None = None,
    created_at_lte: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    if operation is not None and operation not in MEMORY_VERSION_OPERATIONS:
        raise ValueError(f"Unknown Memory Version operation {operation!r}")
    stmt = select(MemoryVersion).where(
        MemoryVersion.memory_store_id == memory_store_id,
        MemoryVersion.organization_id == organization_id,
    )
    if memory_id is not None:
        stmt = stmt.where(MemoryVersion.memory_id == memory_id)
    if operation is not None:
        stmt = stmt.where(MemoryVersion.operation == operation)
    if api_key_id is not None:
        stmt = stmt.where(MemoryVersion.api_key_id == api_key_id)
    if session_id is not None:
        stmt = stmt.where(MemoryVersion.session_id == session_id)
    if created_at_gte is not None:
        stmt = stmt.where(MemoryVersion.created_at >= created_at_gte)
    if created_at_lte is not None:
        stmt = stmt.where(MemoryVersion.created_at <= created_at_lte)
    return await fetch_page(
        db,
        stmt,
        sort=MemoryVersion.created_at,
        id_column=MemoryVersion.id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def current_memory_for_version(
    db: AsyncSession,
    *,
    version: MemoryVersion,
) -> Memory | None:
    result = await db.execute(
        select(Memory).where(
            Memory.id == version.memory_id,
            Memory.memory_store_id == version.memory_store_id,
        )
    )
    return result.scalar_one_or_none()


async def redact_memory_version(
    db: AsyncSession,
    version: MemoryVersion,
    *,
    redacted_by: dict[str, str] | None,
) -> None:
    if version.redacted_at is not None:
        return
    version.path = None
    version.content = None
    version.content_sha256 = None
    version.content_size_bytes = None
    version.redacted_at = datetime.now(timezone.utc)
    version.redacted_by = redacted_by
    await db.flush()


def _new_memory_version(
    *,
    version_id: str,
    organization_id: str,
    memory_store_id: str,
    memory_id: str,
    operation: str,
    path: str | None,
    content: str | None,
    content_sha256: str | None,
    content_size_bytes: int | None,
    created_by: dict[str, str] | None,
) -> MemoryVersion:
    actor_type = created_by.get("type") if created_by else None
    return MemoryVersion(
        id=version_id,
        organization_id=organization_id,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        operation=operation,
        path=path,
        content=content,
        content_sha256=content_sha256,
        content_size_bytes=content_size_bytes,
        created_by=dict(created_by) if created_by else None,
        actor_type=actor_type,
        api_key_id=(
            created_by.get("api_key_id")
            if actor_type == "api_actor" and created_by
            else None
        ),
        session_id=(
            created_by.get("session_id")
            if actor_type == "session_actor" and created_by
            else None
        ),
        user_id=(
            created_by.get("user_id")
            if actor_type == "user_actor" and created_by
            else None
        ),
    )
