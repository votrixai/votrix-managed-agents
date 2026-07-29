from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.utils.id_generator import new_id


async def create_file(
    db: AsyncSession,
    *,
    organization_id: str,
    filename: str,
    storage_key: str,
    mime_type: str | None = None,
    size_bytes: int = 0,
    sha256: str | None = None,
    scope_id: str | None = None,
) -> File:
    """Record a file whose bytes are already stored.

    Created last, on purpose: nothing here describes a file that might not be
    there, so every row can be downloaded and there is no state to check.

    `scope_id` names the session that produced this file. It is null for
    anything a user uploaded, which is what separates the two on a list.
    """
    file = File(
        id=new_id("file"),
        organization_id=organization_id,
        scope_id=scope_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_key=storage_key,
    )
    db.add(file)
    await db.flush()
    return file


async def get_file(db: AsyncSession, *, file_id: str, organization_id: str) -> File | None:
    result = await db.execute(
        select(File).where(File.id == file_id, File.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def get_latest_scoped_file(
    db: AsyncSession,
    *,
    scope_id: str,
    filename: str,
    organization_id: str,
) -> File | None:
    """The most recent capture of this path by this session.

    Captures append rather than replace, so a path the agent rewrote has
    several rows. This is the current one — and comparing its hash is how a
    file that has not changed since the last capture is left alone.
    """
    result = await db.execute(
        select(File)
        .where(
            File.scope_id == scope_id,
            File.filename == filename,
            File.organization_id == organization_id,
            File.archived_at.is_(None),
        )
        .order_by(File.created_at.desc(), File.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def list_files(
    db: AsyncSession,
    *,
    organization_id: str,
    scope_id: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(File).where(
        File.organization_id == organization_id,
        File.archived_at.is_(None),
    )
    if scope_id is not None:
        stmt = stmt.where(File.scope_id == scope_id)
    return await fetch_page(
        db, stmt, sort=File.created_at, id_column=File.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )


async def total_size_bytes(db: AsyncSession, *, organization_id: str) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(File.size_bytes), 0)).where(
            File.organization_id == organization_id,
            File.archived_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def archive_file(db: AsyncSession, file: File) -> None:
    file.archived_at = datetime.now(timezone.utc)
    await db.flush()


async def delete_file(db: AsyncSession, file: File) -> None:
    await db.delete(file)
    await db.flush()
