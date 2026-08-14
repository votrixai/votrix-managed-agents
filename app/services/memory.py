"""Memory Store control-plane lifecycle backed by provider Volumes."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryStore, SessionMemoryStore, SessionSandbox
from app.db.models.memory import (
    VOLUME_DELETING,
    VOLUME_FAILED,
    VOLUME_PROVIDER_E2B,
    VOLUME_READY,
)
from app.db.models.sessions import (
    IDLE,
    SANDBOX_PAUSED,
    SANDBOX_RUNNING,
    TERMINATED,
    Session,
)
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import memory as memory_q
from app.models.errors import (
    Conflict,
    InvalidRequest,
    MemoryStoreUnavailable,
    NotFound,
    PayloadTooLarge,
)
from app.utils.sandbox import Sandbox
from app.utils.volume import Volume

logger = structlog.get_logger(__name__)

MAX_MEMORY_STORE_FILE_BYTES = 100 * 1024 * 1024
MAX_MEMORY_STORE_FILE_PATH_BYTES = 1024


@dataclass(frozen=True)
class MemoryStoreFile:
    memory_store_id: str
    path: str
    size_bytes: int
    sha256: str


def encode_page_cursor(value: str, *, kind: str) -> str:
    """An opaque cursor. Callers page by handing this back, not by building one.

    ``kind`` is carried inside so a cursor from one collection cannot be
    replayed against another, where the same id would mean something else.
    """

    payload = json.dumps(
        {"kind": kind, "value": value},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"page_{encoded}"


def decode_page_cursor(token: str, *, kind: str) -> str:
    if not token.startswith("page_"):
        raise InvalidRequest("page is not a valid Memory cursor")
    raw = token.removeprefix("page_")
    raw += "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw).decode())
    except Exception as exc:
        raise InvalidRequest("page is not a valid Memory cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != kind
        or not isinstance(payload.get("value"), str)
    ):
        raise InvalidRequest("page is not valid for this Memory collection")
    return payload["value"]


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


async def put_memory_store_file(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path: str,
    content: bytes,
) -> MemoryStoreFile:
    """Create or replace one file in the Store's provider filesystem."""

    normalized_path = normalize_memory_store_file_path(path)
    if len(content) > MAX_MEMORY_STORE_FILE_BYTES:
        raise PayloadTooLarge(
            f"Memory Store files may be at most {MAX_MEMORY_STORE_FILE_BYTES} bytes"
        )
    store, mounted = await _file_mutation_target(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    provider_path = f"/{normalized_path}"
    try:
        if mounted is None:
            await Volume.write_file(store, provider_path, content)
        else:
            attachment, sandbox = mounted
            container = Sandbox.from_id(
                sandbox.external_sandbox_id,
                attachment.session_id,
                organization_id,
            )
            await container.write_bytes(
                f"{attachment.mount_path}{provider_path}",
                content,
            )
    except Exception as exc:
        logger.exception(
            "memory_store_file_write_failed",
            memory_store_id=store.id,
            path=normalized_path,
        )
        raise MemoryStoreUnavailable(
            f"Memory Store {store.id} could not persist {normalized_path}"
        ) from exc

    # This also releases the Store and Session row locks. There is no file
    # projection to update: the provider filesystem is the source of truth.
    await db.commit()
    return MemoryStoreFile(
        memory_store_id=store.id,
        path=normalized_path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def delete_memory_store_file(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path: str,
) -> None:
    """Delete one file, treating an already absent path as success."""

    normalized_path = normalize_memory_store_file_path(path)
    store, mounted = await _file_mutation_target(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    provider_path = f"/{normalized_path}"
    try:
        if mounted is None:
            await Volume.remove_file(store, provider_path)
        else:
            attachment, sandbox = mounted
            container = Sandbox.from_id(
                sandbox.external_sandbox_id,
                attachment.session_id,
                organization_id,
            )
            await container.remove_file(f"{attachment.mount_path}{provider_path}")
    except Exception as exc:
        logger.exception(
            "memory_store_file_remove_failed",
            memory_store_id=store.id,
            path=normalized_path,
        )
        raise MemoryStoreUnavailable(
            f"Memory Store {store.id} could not remove {normalized_path}"
        ) from exc

    await db.commit()


def normalize_memory_store_file_path(value: str) -> str:
    """Validate the relative path exposed after ``/files/`` in the URL."""

    if not isinstance(value, str) or not value:
        raise InvalidRequest("Memory Store file path must not be empty")
    if value.startswith("/"):
        raise InvalidRequest("Memory Store file path must be relative")
    if len(value.encode("utf-8")) > MAX_MEMORY_STORE_FILE_PATH_BYTES:
        raise InvalidRequest(
            f"Memory Store file path must be at most {MAX_MEMORY_STORE_FILE_PATH_BYTES} bytes"
        )
    if unicodedata.normalize("NFC", value) != value:
        raise InvalidRequest("Memory Store file path must be NFC-normalized")

    parts = value.split("/")
    if any(part == "" for part in parts):
        raise InvalidRequest("Memory Store file path must not contain empty segments")
    if any(part in {".", ".."} for part in parts):
        raise InvalidRequest("Memory Store file path must not contain '.' or '..' segments")
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for part in parts
        for character in part
    ):
        raise InvalidRequest(
            "Memory Store file path must not contain control or format characters"
        )
    return value


async def _file_mutation_target(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> tuple[
    MemoryStore,
    tuple[SessionMemoryStore, SessionSandbox] | None,
]:
    """Choose the provider API that is valid for the Store's mount state.

    E2B's standalone Volume content API is for unmounted Volumes. If a usable
    mount exists, mutate through that Sandbox instead. Session rows stay locked
    through the provider call so an idle Session cannot begin a turn midway
    through the external write.
    """

    store = await memory_q.get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if store is None or store.provisioning_status != VOLUME_READY:
        raise NotFound(f"Memory Store {memory_store_id} not found")
    if store.archived_at is not None:
        raise Conflict("Archived Memory Stores are read-only")

    mounts = await memory_q.list_memory_store_mounts_for_update(
        db,
        memory_store_id=store.id,
        organization_id=organization_id,
    )
    busy = [
        session.id
        for _, session, _ in mounts
        if session.status not in (IDLE, TERMINATED)
    ]
    if busy:
        raise Conflict(
            f"Memory Store {store.id} is mounted by a busy Session; retry when it is idle"
        )

    for attachment, _, sandbox in mounts:
        if (
            sandbox is not None
            and sandbox.external_sandbox_id
            and sandbox.state in (SANDBOX_RUNNING, SANDBOX_PAUSED)
        ):
            return store, (attachment, sandbox)
    return store, None


async def archive_memory_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    """Retire a Store from an operator workflow; not a public API operation."""

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
    """Destroy an unused Store from an operator workflow, never public HTTP."""

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

    # Nothing left to purge on this side: the contents are the Volume, and
    # destroying it above took them with it.
    await memory_q.mark_memory_store_deleted(db, store)
    await db.commit()
    return store


async def require_attachable(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
    # This lock serializes first attachment with standalone Volume file
    # mutations. Once the attachment row commits, later mutations go through
    # the mounted Sandbox instead.
    store = await memory_q.get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if store is None or store.provisioning_status != VOLUME_READY:
        raise NotFound(f"Memory Store {memory_store_id} not found")
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
    "MAX_MEMORY_STORE_FILE_BYTES",
    "MAX_MEMORY_STORE_FILE_PATH_BYTES",
    "MemoryStoreFile",
    "archive_memory_store",
    "create_memory_store",
    "delete_memory_store_file",
    "delete_memory_store",
    "get_memory_store",
    "list_memory_stores",
    "normalize_memory_store_file_path",
    "put_memory_store_file",
    "require_attachable",
    "update_memory_store",
]
