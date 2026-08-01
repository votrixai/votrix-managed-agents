"""CMA-compatible Memory documents and immutable version history."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, MemoryStore, MemoryVersion
from app.db.models.memory import VOLUME_READY
from app.db.queries import Page
from app.db.queries import memory as memory_q
from app.db.queries import sessions as sessions_q
from app.models.errors import (
    Conflict,
    InvalidRequest,
    MemoryPreconditionFailed,
    MemoryStoreUnavailable,
    NotFound,
    PayloadTooLarge,
)
from app.models.memory import (
    MAX_MEMORIES_PER_STORE,
    MAX_MEMORY_CONTENT_BYTES,
    normalize_memory_path,
    normalize_memory_path_prefix,
)
from app.utils.volume import Volume

logger = structlog.get_logger(__name__)

MemoryView = Literal["basic", "full"]


@dataclass(frozen=True)
class MemoryPrefix:
    path: str


@dataclass(frozen=True)
class MemoryListPage:
    items: list[Memory | MemoryPrefix]
    next_page: str | None

    @property
    def has_more(self) -> bool:
        return self.next_page is not None


@dataclass(frozen=True)
class MemoryVersionPage:
    page: Page
    next_page: str | None


@dataclass(frozen=True)
class MemorySyncResult:
    changed: int
    issues: tuple[str, ...] = ()


def api_actor(api_key_id: str | None) -> dict[str, str] | None:
    if not api_key_id:
        return None
    return {"type": "api_actor", "api_key_id": api_key_id}


def session_actor(session_id: str) -> dict[str, str]:
    return {"type": "session_actor", "session_id": session_id}


async def create_memory(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path: str,
    content: str | None,
    created_by: dict[str, str] | None,
) -> Memory:
    path = normalize_memory_path(path)
    content = content or ""
    digest, size = _content_metadata(content)
    store = await _store_for_write(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if await memory_q.get_memory_by_path(
        db,
        memory_store_id=store.id,
        organization_id=organization_id,
        path=path,
    ):
        raise Conflict(f"A Memory already exists at {path}")
    if (
        await memory_q.count_memories(
            db,
            memory_store_id=store.id,
            organization_id=organization_id,
        )
        >= MAX_MEMORIES_PER_STORE
    ):
        raise Conflict("Memory Store has reached the 2000 Memory limit")

    await _provider_write(store, path, content)
    memory, _ = await memory_q.create_memory_with_version(
        db,
        organization_id=organization_id,
        memory_store_id=store.id,
        path=path,
        content=content,
        content_sha256=digest,
        content_size_bytes=size,
        created_by=created_by,
    )
    await db.commit()
    return memory


async def get_memory(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_id: str,
    organization_id: str,
) -> Memory:
    await _store_for_read(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    memory = await memory_q.get_memory(
        db,
        memory_id=memory_id,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if memory is None:
        raise NotFound(f"Memory {memory_id} not found")
    return memory


async def list_memories(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    path_prefix: str | None = None,
    depth: int | None = None,
    view: MemoryView = "basic",
    limit: int = 20,
    page: str | None = None,
) -> MemoryListPage:
    await _store_for_read(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if depth not in (None, 0, 1):
        raise InvalidRequest("depth must be 0, 1, or omitted")
    if not 1 <= limit <= 100:
        raise InvalidRequest("limit must be between 1 and 100")
    if view == "full" and limit > 20:
        raise InvalidRequest("limit may not exceed 20 when view=full")
    if path_prefix is not None:
        try:
            path_prefix = normalize_memory_path_prefix(path_prefix)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    memories = await memory_q.list_memories(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        path_prefix=path_prefix,
    )
    items: list[Memory | MemoryPrefix]
    if depth == 1:
        items = _roll_up_memories(memories, path_prefix=path_prefix or "/")
    else:
        # Cursor comparison below uses Python's Unicode ordering. Sort here as
        # well so database collation cannot make a page skip or repeat paths.
        items = sorted(memories, key=lambda memory: memory.path)

    cursor_kind = f"memory_path:{memory_store_id}"
    cursor_path = decode_page_cursor(page, kind=cursor_kind) if page else None
    if cursor_path is not None:
        items = [item for item in items if _item_path(item) > cursor_path]
    has_more = len(items) > limit
    selected = items[:limit]
    next_page = (
        encode_page_cursor(_item_path(selected[-1]), kind=cursor_kind)
        if has_more and selected
        else None
    )
    return MemoryListPage(items=selected, next_page=next_page)


async def update_memory(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_id: str,
    organization_id: str,
    changes: dict,
    created_by: dict[str, str] | None,
) -> Memory:
    store = await _store_for_write(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    memory = await memory_q.get_memory(
        db,
        memory_id=memory_id,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if memory is None:
        raise NotFound(f"Memory {memory_id} not found")

    requested_path = memory.path
    if changes.get("path") is not None:
        requested_path = normalize_memory_path(changes["path"])
    requested_content = memory.content
    if "content" in changes:
        requested_content = changes["content"] or ""
    requested_sha, requested_size = _content_metadata(requested_content)

    precondition = changes.get("precondition")
    if precondition is not None:
        expected_sha = (
            precondition.get("content_sha256")
            if isinstance(precondition, dict)
            else precondition.content_sha256
        )
        if expected_sha != memory.content_sha256:
            if requested_path == memory.path and requested_content == memory.content:
                return memory
            raise MemoryPreconditionFailed(
                "Memory content_sha256 precondition failed; retrieve and retry"
            )

    if requested_path == memory.path and requested_content == memory.content:
        return memory
    if requested_path != memory.path:
        collision = await memory_q.get_memory_by_path(
            db,
            memory_store_id=memory_store_id,
            organization_id=organization_id,
            path=requested_path,
        )
        if collision is not None and collision.id != memory.id:
            raise Conflict(f"A Memory already exists at {requested_path}")

    if requested_path == memory.path:
        await _provider_write(store, requested_path, requested_content)
    else:
        await _provider_write(store, requested_path, requested_content)
        try:
            await _provider_remove(store, memory.path)
        except Exception:
            # The database still names the old path. Remove the speculative new
            # copy when possible so the next Runtime reconciliation cannot
            # import a rename that the API reported as failed.
            try:
                await Volume.remove_file(store, requested_path)
            except Exception:
                pass
            raise

    await memory_q.update_memory_with_version(
        db,
        memory,
        path=requested_path,
        content=requested_content,
        content_sha256=requested_sha,
        content_size_bytes=requested_size,
        created_by=created_by,
    )
    await db.commit()
    return memory


async def delete_memory(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_id: str,
    organization_id: str,
    expected_content_sha256: str | None,
    created_by: dict[str, str] | None,
) -> str:
    store = await _store_for_write(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    memory = await memory_q.get_memory(
        db,
        memory_id=memory_id,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if memory is None:
        raise NotFound(f"Memory {memory_id} not found")
    if (
        expected_content_sha256 is not None
        and expected_content_sha256 != memory.content_sha256
    ):
        raise MemoryPreconditionFailed(
            "Memory content_sha256 precondition failed; retrieve and retry"
        )

    await _provider_remove(store, memory.path)
    deleted_id = memory.id
    await memory_q.delete_memory_with_version(
        db,
        memory,
        created_by=created_by,
    )
    await db.commit()
    return deleted_id


async def get_memory_version(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_version_id: str,
    organization_id: str,
) -> MemoryVersion:
    await _store_for_read(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    version = await memory_q.get_memory_version(
        db,
        memory_version_id=memory_version_id,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if version is None:
        raise NotFound(f"Memory Version {memory_version_id} not found")
    return version


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
    view: MemoryView = "basic",
    limit: int = 20,
    page: str | None = None,
) -> MemoryVersionPage:
    await _store_for_read(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    if operation not in (None, "created", "modified", "deleted"):
        raise InvalidRequest("operation must be created, modified, or deleted")
    if not 1 <= limit <= 100:
        raise InvalidRequest("limit must be between 1 and 100")
    if view == "full" and limit > 20:
        raise InvalidRequest("limit may not exceed 20 when view=full")
    cursor_kind = f"memory_version:{memory_store_id}"
    after_id = decode_page_cursor(page, kind=cursor_kind) if page else None
    found = await memory_q.list_memory_versions(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        memory_id=memory_id,
        operation=operation,
        api_key_id=api_key_id,
        session_id=session_id,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=limit,
        after_id=after_id,
    )
    next_page = (
        encode_page_cursor(found.last_id, kind=cursor_kind)
        if found.has_more and found.last_id
        else None
    )
    return MemoryVersionPage(page=found, next_page=next_page)


async def redact_memory_version(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_version_id: str,
    organization_id: str,
    redacted_by: dict[str, str] | None,
) -> MemoryVersion:
    await _store_for_read(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    version = await memory_q.get_memory_version(
        db,
        memory_version_id=memory_version_id,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if version is None:
        raise NotFound(f"Memory Version {memory_version_id} not found")
    current = await memory_q.current_memory_for_version(db, version=version)
    if current is not None and current.current_version_id == version.id:
        raise Conflict("The current live Memory Version cannot be redacted")
    await memory_q.redact_memory_version(
        db,
        version,
        redacted_by=redacted_by,
    )
    await db.commit()
    return version


async def reconcile_session_memory_stores(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    sandbox,
) -> MemorySyncResult:
    """Diff mounted files into CMA Memories after a Runtime filesystem tool.

    The mounted Volume is the agent's live filesystem. The relational rows are
    its audited API projection. Hashing first means unchanged files cost no
    transfer; only a new or changed document is read into this process.
    """
    attachments = await sessions_q.list_session_memory_stores(
        db,
        session_id=session_id,
    )
    total_changed = 0
    issues: list[str] = []
    for attachment in attachments:
        if attachment.organization_id != organization_id:
            continue
        if attachment.access != "read_write":
            continue
        changed, found_issues = await _reconcile_one_mounted_store(
            db,
            memory_store_id=attachment.memory_store_id,
            organization_id=organization_id,
            mount_path=attachment.mount_path,
            session_id=session_id,
            sandbox=sandbox,
        )
        total_changed += changed
        issues.extend(found_issues)
        # One Store is one reconciliation unit. Committing here releases its
        # row lock before a second attached Store incurs provider round trips.
        await db.commit()
    return MemorySyncResult(changed=total_changed, issues=tuple(issues))


async def _reconcile_one_mounted_store(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
    mount_path: str,
    session_id: str,
    sandbox,
) -> tuple[int, list[str]]:
    store = await memory_q.get_memory_store(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
        for_update=True,
    )
    if store is None or store.provisioning_status != VOLUME_READY:
        raise MemoryStoreUnavailable(
            f"Memory Store {memory_store_id} is no longer available for reconciliation"
        )

    files = await sandbox.list_files(
        mount_path,
        max_files=MAX_MEMORIES_PER_STORE + 1,
        include_oversized=True,
    )
    if len(files) > MAX_MEMORIES_PER_STORE:
        raise Conflict(
            f"Memory Store {memory_store_id} contains more than 2000 files; "
            "Runtime changes were not indexed"
        )

    current = await memory_q.list_memories(
        db,
        memory_store_id=memory_store_id,
        organization_id=organization_id,
    )
    by_path = {memory.path: memory for memory in current}
    seen_paths: set[str] = set()
    actor = session_actor(session_id)
    changed = 0
    issues: list[str] = []

    for file in files:
        path = "/" + file.path.lstrip("/")
        seen_paths.add(path)
        try:
            normalize_memory_path(path)
        except ValueError as exc:
            issues.append(f"{path}: {exc}")
            continue
        if file.size_bytes > MAX_MEMORY_CONTENT_BYTES:
            issues.append(
                f"{path}: {file.size_bytes} bytes exceeds the "
                f"{MAX_MEMORY_CONTENT_BYTES} byte Memory limit"
            )
            continue

        previous = by_path.get(path)
        if previous is not None and previous.content_sha256 == file.sha256:
            continue
        try:
            content_bytes = await sandbox.read_bytes(
                f"{mount_path}/{file.path}",
                max_bytes=MAX_MEMORY_CONTENT_BYTES,
            )
            content = content_bytes.decode("utf-8")
        except (FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
            issues.append(f"{path}: cannot index as UTF-8 Memory ({exc})")
            continue
        digest = hashlib.sha256(content_bytes).hexdigest()
        size = len(content_bytes)

        if previous is None:
            if len(by_path) >= MAX_MEMORIES_PER_STORE:
                issues.append(f"{path}: Memory Store has reached its 2000 Memory limit")
                continue
            memory, _ = await memory_q.create_memory_with_version(
                db,
                organization_id=organization_id,
                memory_store_id=memory_store_id,
                path=path,
                content=content,
                content_sha256=digest,
                content_size_bytes=size,
                created_by=actor,
            )
            by_path[path] = memory
        else:
            await memory_q.update_memory_with_version(
                db,
                previous,
                path=path,
                content=content,
                content_sha256=digest,
                content_size_bytes=size,
                created_by=actor,
            )
        changed += 1

    for path, memory in list(by_path.items()):
        if path in seen_paths:
            continue
        await memory_q.delete_memory_with_version(
            db,
            memory,
            created_by=actor,
        )
        changed += 1

    if store.archived_at is not None and changed:
        issues.append(
            f"Memory Store {store.id} was modified by an already-attached Session "
            "after it was archived"
        )
    return changed, issues


async def _store_for_read(
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


async def _store_for_write(
    db: AsyncSession,
    *,
    memory_store_id: str,
    organization_id: str,
) -> MemoryStore:
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
    return store


def _content_metadata(content: str) -> tuple[str, int]:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_MEMORY_CONTENT_BYTES:
        raise PayloadTooLarge(
            f"Memory content exceeds the {MAX_MEMORY_CONTENT_BYTES} byte limit"
        )
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


async def _provider_write(store: MemoryStore, path: str, content: str) -> None:
    try:
        await Volume.write_file(store, path, content)
    except Exception as exc:
        logger.exception(
            "memory_provider_write_failed",
            memory_store_id=store.id,
            path=path,
        )
        raise MemoryStoreUnavailable(
            f"Memory Store {store.id} could not persist {path}"
        ) from exc


async def _provider_remove(store: MemoryStore, path: str) -> None:
    try:
        await Volume.remove_file(store, path)
    except Exception as exc:
        logger.exception(
            "memory_provider_remove_failed",
            memory_store_id=store.id,
            path=path,
        )
        raise MemoryStoreUnavailable(
            f"Memory Store {store.id} could not remove {path}"
        ) from exc


def _roll_up_memories(
    memories: list[Memory],
    *,
    path_prefix: str,
) -> list[Memory | MemoryPrefix]:
    entries: dict[str, Memory | MemoryPrefix] = {}
    for memory in memories:
        relative = memory.path[len(path_prefix) :]
        if "/" not in relative:
            entries[memory.path] = memory
            continue
        directory = relative.split("/", 1)[0]
        prefix = f"{path_prefix}{directory}/"
        entries.setdefault(prefix, MemoryPrefix(path=prefix))
    return [entries[path] for path in sorted(entries)]


def _item_path(item: Memory | MemoryPrefix) -> str:
    return item.path


def encode_page_cursor(value: str, *, kind: str) -> str:
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


__all__ = [
    "MemoryListPage",
    "MemoryPrefix",
    "MemoryVersionPage",
    "MemorySyncResult",
    "api_actor",
    "create_memory",
    "decode_page_cursor",
    "delete_memory",
    "encode_page_cursor",
    "get_memory",
    "get_memory_version",
    "list_memories",
    "list_memory_versions",
    "redact_memory_version",
    "reconcile_session_memory_stores",
    "session_actor",
    "update_memory",
]
