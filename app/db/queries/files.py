from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File, SessionFile
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


async def get_live_scoped_file(
    db: AsyncSession,
    *,
    scope_id: str,
    filename: str,
    organization_id: str,
) -> File | None:
    """The file this session currently has at this path, if any.

    There is at most one — a partial unique index says so — because a
    session's outputs are a directory and a directory holds one file per path.
    Comparing its hash is how a file that has not changed since the last
    capture is left alone; a hash that differs means this same row takes on the
    new contents.

    Archived rows are not candidates. Those are paths that no longer exist,
    kept only so ids already handed out keep resolving.
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


async def list_live_scoped_files(
    db: AsyncSession, *, scope_id: str, organization_id: str
) -> list[File]:
    """Every path this session currently has, as rows.

    What a capture pass compares the container's own listing against, to find
    the rows whose files are gone.
    """
    result = await db.execute(
        select(File).where(
            File.scope_id == scope_id,
            File.organization_id == organization_id,
            File.archived_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def replace_file_content(
    db: AsyncSession,
    file: File,
    *,
    storage_key: str,
    mime_type: str | None,
    size_bytes: int,
    sha256: str | None,
) -> File:
    """Point an existing row at new bytes, keeping its identity.

    A path the agent rewrote is the same file, so it keeps the same id. That
    is what lets a link handed to someone an hour ago still resolve, and
    resolve to what is there now rather than to what used to be — the two
    things a file path means.

    `created_at` is deliberately untouched: it records when this path first
    appeared, which is a different question from when its contents last
    changed. `updated_at` answers that one.

    The old object stays in the bucket. Removing it here would mean destroying
    bytes a still-live row points at if this transaction then rolled back, and
    an unreferenced object costs pennies where that costs the file.
    """
    file.storage_key = storage_key
    file.mime_type = mime_type
    file.size_bytes = size_bytes
    file.sha256 = sha256
    await db.flush()
    return file


async def archive_file(db: AsyncSession, file: File) -> File:
    """Mark a row as no longer being a file this session has.

    Not a delete. The bytes stay, and so does the id: something quoted it to a
    user, and a link that stops working is worse than one that hands back the
    last thing that was there. What changes is that it stops being listed.
    """
    if file.archived_at is None:
        file.archived_at = datetime.now(timezone.utc)
        await db.flush()
    return file


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


async def sessions_holding(db: AsyncSession, file: File) -> int:
    """How many sessions were given this file.

    An uploaded file is mounted into a session by a row in `session_files`, and
    sessions are only ever soft-deleted — so that row outlives the session and
    keeps referencing the file for good. Asking first is what turns "the
    foreign key will refuse this" into something the caller can be told.
    """
    return (
        await db.execute(
            select(func.count())
            .select_from(SessionFile)
            .where(SessionFile.file_id == file.id)
        )
    ).scalar_one()


async def delete_file(db: AsyncSession, file: File) -> None:
    await db.delete(file)
    await db.flush()
