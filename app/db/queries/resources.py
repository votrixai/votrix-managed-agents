from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import ManagedResource, ManagedSession
from app.ids import new_id
from app.organization import resolve_organization_id


PREFIXES = {
    "skill": "skill",
    "skill_version": "skver",
    "file": "file",
    "vault": "vault",
    "credential": "cred",
    "memory_store": "memstore",
    "memory": "mem",
    "memory_version": "memver",
    "deployment": "deploy",
    "deployment_run": "deprun",
    "user_profile": "uprof",
    "session_resource": "sesrsc",
    "session_thread": "thread",
    "environment_work": "work",
}


async def create_resource(
    db: AsyncSession,
    *,
    resource_type: str,
    data: dict[str, Any] | None = None,
    parent_id: str | None = None,
    version: int | None = None,
    name: str | None = None,
    status: str = "active",
    content: bytes | None = None,
    content_type: str | None = None,
    filename: str | None = None,
    storage_backend: str | None = None,
    storage_key: str | None = None,
    storage_url: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    organization_id: str | None = None,
) -> ManagedResource:
    resource = ManagedResource(
        id=new_id(PREFIXES.get(resource_type, "res")),
        organization_id=resolve_organization_id(organization_id),
        resource_type=resource_type,
        parent_id=parent_id,
        version=version,
        name=name,
        status=status,
        data=data or {},
        content=content,
        content_type=content_type,
        filename=filename,
        storage_backend=storage_backend,
        storage_key=storage_key,
        storage_url=storage_url,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    db.add(resource)
    await db.flush()
    return resource


async def get_resource(
    db: AsyncSession,
    *,
    resource_id: str,
    resource_type: str | None = None,
    parent_id: str | None = None,
    include_deleted: bool = False,
    organization_id: str | None = None,
    for_update: bool = False,
) -> ManagedResource | None:
    stmt = select(ManagedResource).where(
        ManagedResource.id == resource_id,
        ManagedResource.organization_id == resolve_organization_id(organization_id),
    )
    if resource_type is not None:
        stmt = stmt.where(ManagedResource.resource_type == resource_type)
    if parent_id is not None:
        stmt = stmt.where(ManagedResource.parent_id == parent_id)
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_resource_by_name(
    db: AsyncSession,
    *,
    resource_type: str,
    name: str,
    parent_id: str | None = None,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> ManagedResource | None:
    stmt = select(ManagedResource).where(
        ManagedResource.resource_type == resource_type,
        ManagedResource.name == name,
        ManagedResource.organization_id == resolve_organization_id(organization_id),
    )
    if parent_id is not None:
        stmt = stmt.where(ManagedResource.parent_id == parent_id)
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_resource_by_sha256(
    db: AsyncSession,
    *,
    resource_type: str,
    sha256: str,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> ManagedResource | None:
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == resource_type,
            ManagedResource.sha256 == sha256,
            ManagedResource.organization_id == resolve_organization_id(organization_id),
        )
        .order_by(ManagedResource.created_at.asc())
    )
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalars().first()


async def count_resources_by_storage_key(
    db: AsyncSession,
    *,
    resource_type: str,
    storage_backend: str,
    storage_key: str,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(ManagedResource).where(
        ManagedResource.resource_type == resource_type,
        ManagedResource.storage_backend == storage_backend,
        ManagedResource.storage_key == storage_key,
        ManagedResource.organization_id == resolve_organization_id(organization_id),
    )
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def list_resources_by_name_prefix(
    db: AsyncSession,
    *,
    resource_type: str,
    parent_id: str,
    name_prefix: str,
    limit: int = 1000,
    include_archived: bool = True,
    organization_id: str | None = None,
) -> list[ManagedResource]:
    escaped = _escape_like(name_prefix)
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == resource_type,
            ManagedResource.parent_id == parent_id,
            ManagedResource.organization_id == resolve_organization_id(organization_id),
            ManagedResource.deleted_at.is_(None),
            or_(
                ManagedResource.name == name_prefix,
                ManagedResource.name.like(f"{escaped}/%", escape="\\"),
            ),
        )
        .order_by(ManagedResource.created_at.desc(), ManagedResource.id.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(ManagedResource.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_resource_version(
    db: AsyncSession,
    *,
    resource_type: str,
    parent_id: str,
    version: int,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> ManagedResource | None:
    stmt = select(ManagedResource).where(
        ManagedResource.resource_type == resource_type,
        ManagedResource.parent_id == parent_id,
        ManagedResource.version == version,
        ManagedResource.organization_id == resolve_organization_id(organization_id),
    )
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_resources(
    db: AsyncSession,
    *,
    resource_type: str,
    parent_id: str | None = None,
    limit: int = 50,
    include_archived: bool = True,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> list[ManagedResource]:
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == resource_type,
            ManagedResource.organization_id == resolve_organization_id(organization_id),
        )
        .order_by(ManagedResource.created_at.desc(), ManagedResource.id.desc())
        .limit(limit)
    )
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    if parent_id is not None:
        stmt = stmt.where(ManagedResource.parent_id == parent_id)
    if not include_archived:
        stmt = stmt.where(ManagedResource.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_memory_versions_page(
    db: AsyncSession,
    *,
    memory_store_id: str,
    memory_id: str | None = None,
    operation: str | None = None,
    api_key_id: str | None = None,
    session_id: str | None = None,
    created_at_gte: datetime | None = None,
    created_at_lte: datetime | None = None,
    limit: int = 51,
    offset: int = 0,
    organization_id: str | None = None,
) -> list[ManagedResource]:
    """List one database page of store-scoped memory history.

    The parent-memory join deliberately includes soft-deleted memories so their
    immutable history remains visible from the store-wide endpoint.
    """

    memory = aliased(ManagedResource)
    operation_value = ManagedResource.data["operation"].as_string()
    created_by_api_key = ManagedResource.data["created_by"]["api_key_id"].as_string()
    actor = ManagedResource.data["actor"].as_string()
    direct_session = ManagedResource.data["session_id"].as_string()
    snapshot_session = ManagedResource.data["snapshot"]["session_id"].as_string()
    organization = resolve_organization_id(organization_id)

    stmt = (
        select(ManagedResource)
        .join(
            memory,
            and_(
                memory.id == ManagedResource.parent_id,
                memory.organization_id == ManagedResource.organization_id,
                memory.resource_type == "memory",
                memory.parent_id == memory_store_id,
            ),
        )
        .where(
            ManagedResource.resource_type == "memory_version",
            ManagedResource.organization_id == organization,
            ManagedResource.deleted_at.is_(None),
            memory.organization_id == organization,
        )
    )
    if memory_id is not None:
        stmt = stmt.where(memory.id == memory_id)
    if operation == "created":
        stmt = stmt.where(operation_value.in_(("created", "create")))
    elif operation == "deleted":
        stmt = stmt.where(operation_value.in_(("deleted", "delete")))
    elif operation == "modified":
        stmt = stmt.where(
            or_(
                operation_value.is_(None),
                operation_value.not_in(("created", "create", "deleted", "delete")),
            )
        )
    if api_key_id is not None:
        stmt = stmt.where(func.coalesce(created_by_api_key, actor) == api_key_id)
    if session_id is not None:
        stmt = stmt.where(func.coalesce(direct_session, snapshot_session) == session_id)
    if created_at_gte is not None:
        stmt = stmt.where(ManagedResource.created_at >= created_at_gte)
    if created_at_lte is not None:
        stmt = stmt.where(ManagedResource.created_at <= created_at_lte)

    result = await db.execute(
        stmt.order_by(ManagedResource.created_at.desc(), ManagedResource.id.desc())
        .offset(max(0, offset))
        .limit(max(1, limit))
    )
    return list(result.scalars().all())


async def list_child_resources_for_update(
    db: AsyncSession,
    *,
    resource_type: str,
    parent_id: str,
    include_archived: bool = True,
    include_deleted: bool = False,
    organization_id: str | None = None,
) -> list[ManagedResource]:
    """Lock every matching child row in a stable order, without pagination.

    Callers must lock the parent row before using this helper.  The parent-first
    contract serializes child mutations with parent cascades, while the stable
    child order prevents deadlocks if a cascade has more than one child.
    """

    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == resource_type,
            ManagedResource.parent_id == parent_id,
            ManagedResource.organization_id == resolve_organization_id(organization_id),
        )
        .order_by(ManagedResource.id.asc())
        .with_for_update()
    )
    if not include_deleted:
        stmt = stmt.where(ManagedResource.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(ManagedResource.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def find_session_resource_referencing_file(
    db: AsyncSession,
    *,
    file_id: str,
    organization_id: str | None = None,
) -> ManagedResource | None:
    """Find an active Session resource whose mounted copy is ``file_id``."""
    result = await db.execute(
        select(ManagedResource)
        .join(
            ManagedSession,
            and_(
                ManagedSession.id == ManagedResource.parent_id,
                ManagedSession.organization_id == ManagedResource.organization_id,
            ),
        )
        .where(
            ManagedResource.resource_type == "session_resource",
            ManagedResource.organization_id == resolve_organization_id(organization_id),
            ManagedResource.deleted_at.is_(None),
            ManagedResource.data["type"].as_string() == "file",
            ManagedResource.data["file_id"].as_string() == file_id,
            ManagedSession.deleted_at.is_(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_files_for_session_scope(
    db: AsyncSession,
    *,
    session_id: str,
    referenced_file_ids: set[str],
    limit: int = 1000,
    organization_id: str | None = None,
) -> list[ManagedResource]:
    """List files belonging to one Session without a organization-wide pre-limit."""
    ownership = [ManagedResource.parent_id == session_id]
    if referenced_file_ids:
        ownership.append(ManagedResource.id.in_(referenced_file_ids))
    ownership.append(ManagedResource.data["scope"]["id"].as_string() == session_id)
    result = await db.execute(
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == "file",
            ManagedResource.organization_id == resolve_organization_id(organization_id),
            ManagedResource.deleted_at.is_(None),
            or_(*ownership),
        )
        .order_by(ManagedResource.created_at.desc(), ManagedResource.id.desc())
        .limit(max(1, limit))
    )
    return list(result.scalars().all())


async def get_work_item_for_worker(db: AsyncSession, work_id: str) -> ManagedResource | None:
    """Resolve a queue item across organizations for the trusted worker process.

    This intentionally bypasses request organization context and is restricted to the
    internal ``environment_work`` resource type. Public routes must use
    :func:`get_resource` instead.
    """
    result = await db.execute(
        select(ManagedResource).where(
            ManagedResource.id == work_id,
            ManagedResource.resource_type == "environment_work",
            ManagedResource.deleted_at.is_(None),
        ).with_for_update()
    )
    return result.scalar_one_or_none()


async def list_work_items_for_worker(
    db: AsyncSession,
    *,
    environment_id: str | None = None,
    limit: int = 1000,
) -> list[ManagedResource]:
    """List queue items across tenants for the trusted worker process only."""
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == "environment_work",
            ManagedResource.deleted_at.is_(None),
        )
        .order_by(ManagedResource.created_at.desc(), ManagedResource.id.desc())
        .limit(limit)
    )
    if environment_id is not None:
        stmt = stmt.where(ManagedResource.parent_id == environment_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_resource(
    db: AsyncSession,
    resource: ManagedResource,
    *,
    data: dict[str, Any] | None = None,
    name: str | None = None,
    status: str | None = None,
    content: bytes | None = None,
    content_type: str | None = None,
    filename: str | None = None,
    storage_backend: str | None = None,
    storage_key: str | None = None,
    storage_url: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> ManagedResource:
    if data is not None:
        resource.data = data
    if name is not None:
        resource.name = name
    if status is not None:
        resource.status = status
    if content is not None:
        resource.content = content
    if content_type is not None:
        resource.content_type = content_type
    if filename is not None:
        resource.filename = filename
    if storage_backend is not None:
        resource.storage_backend = storage_backend
    if storage_key is not None:
        resource.storage_key = storage_key
    if storage_url is not None:
        resource.storage_url = storage_url
    if size_bytes is not None:
        resource.size_bytes = size_bytes
    if sha256 is not None:
        resource.sha256 = sha256
    await db.flush()
    return resource


async def archive_resource(db: AsyncSession, resource: ManagedResource) -> ManagedResource:
    resource.archived_at = datetime.now(timezone.utc)
    resource.status = "archived"
    await db.flush()
    return resource


async def delete_resource(db: AsyncSession, resource: ManagedResource) -> ManagedResource:
    resource.deleted_at = datetime.now(timezone.utc)
    resource.status = "deleted"
    await db.flush()
    return resource


async def next_version(
    db: AsyncSession,
    *,
    resource_type: str,
    parent_id: str,
    organization_id: str | None = None,
) -> int:
    result = await db.execute(
        select(func.max(ManagedResource.version)).where(
            ManagedResource.resource_type == resource_type,
            ManagedResource.parent_id == parent_id,
            ManagedResource.organization_id == resolve_organization_id(organization_id),
        )
    )
    return int(result.scalar_one_or_none() or 0) + 1


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
