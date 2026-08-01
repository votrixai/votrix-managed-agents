"""Memory Store control-plane lifecycle backed by provider Volumes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryStore
from app.db.models.memory import (
    VOLUME_DELETING,
    VOLUME_FAILED,
    VOLUME_PROVIDER_E2B,
    VOLUME_READY,
)
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import memory as memory_q
from app.models.errors import Conflict, MemoryStoreUnavailable, NotFound
from app.utils.volume import Volume

logger = structlog.get_logger(__name__)


async def create_memory_store(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    description: str = "",
    metadata: dict[str, str] | None = None,
) -> MemoryStore:
    """Create the row first, then complete the external side of the saga."""
    store = await memory_q.create_memory_store(
        db,
        organization_id=organization_id,
        name=name,
        description=description,
        metadata=metadata,
        volume_provider=VOLUME_PROVIDER_E2B,
    )
    # The id determines the provider name. Commit it before the network call so
    # a provider timeout never leaves an anonymous resource we cannot reconcile.
    await db.commit()

    try:
        locator = await Volume.provision(store)
    except Exception as exc:
        logger.exception("memory_store_volume_provision_failed", memory_store_id=store.id)
        await memory_q.set_volume_state(
            db,
            store,
            status=VOLUME_FAILED,
            error=_provider_error(exc),
        )
        await db.commit()
        raise MemoryStoreUnavailable("The Memory Store Volume could not be created") from exc

    await memory_q.set_volume_state(
        db,
        store,
        status=VOLUME_READY,
        volume_locator=locator,
        error=None,
    )
    await db.commit()
    return store


async def get_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    store = await memory_q.get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if store is None or store.provisioning_status != VOLUME_READY:
        raise NotFound(f"Memory Store {memory_store_id} not found")
    return store


async def list_memory_stores(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    created_at_gte: datetime | None = None,
    created_at_lte: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    return await memory_q.list_memory_stores(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        provisioning_status=VOLUME_READY,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def update_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    changes: dict[str, Any],
) -> MemoryStore:
    store = await get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if store.archived_at is not None:
        raise Conflict("Archived Memory Stores are read-only")

    fields: dict[str, Any] = {}
    if "name" in changes:
        fields["name"] = changes["name"]
    if "description" in changes:
        fields["description"] = changes["description"] or ""
    if "metadata" in changes:
        patched = dict(store.metadata_ or {})
        for key, value in (changes["metadata"] or {}).items():
            if value is None or value == "":
                patched.pop(key, None)
            else:
                patched[key] = value
        if len(patched) > 16:
            raise Conflict("metadata may contain at most 16 keys")
        fields["metadata"] = patched

    if fields:
        await memory_q.update_memory_store(db, store, **fields)
        await db.commit()
    return store


async def archive_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    store = await get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if store.archived_at is None:
        await memory_q.archive_memory_store(db, store)
        await db.commit()
    return store


async def delete_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    store = await get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    holders = await memory_q.count_memory_store_attachments(
        db, memory_store_id=store.id
    )
    if holders:
        raise Conflict(
            f"Memory Store {store.id} is attached to {holders} session(s); archive it instead"
        )

    await memory_q.set_volume_state(db, store, status=VOLUME_DELETING, error=None)
    await db.commit()
    try:
        await Volume.destroy(store)
    except Exception as exc:
        logger.exception("memory_store_volume_delete_failed", memory_store_id=store.id)
        await memory_q.set_volume_state(
            db,
            store,
            status=VOLUME_FAILED,
            error=_provider_error(exc),
        )
        await db.commit()
        raise MemoryStoreUnavailable("The Memory Store Volume could not be deleted") from exc

    await memory_q.purge_memory_store_contents(
        db,
        memory_store_id=store.id,
        organization_id=store.organization_id,
    )
    await memory_q.mark_memory_store_deleted(db, store)
    await db.commit()
    return store


async def require_attachable(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    store = await get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if store.archived_at is not None:
        raise Conflict(f"Memory Store {store.id} is archived")
    if store.volume_provider != VOLUME_PROVIDER_E2B:
        raise Conflict(
            f"Memory Store provider {store.volume_provider!r} cannot mount in an E2B Session"
        )
    return store


def _provider_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2048]


__all__ = [
    "archive_memory_store",
    "create_memory_store",
    "delete_memory_store",
    "get_memory_store",
    "list_memory_stores",
    "require_attachable",
    "update_memory_store",
]
