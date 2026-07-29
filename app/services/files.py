"""File use cases.

Files are what a session works on and what it hands back. Uploads come in
through this service and go straight out to object storage; downloads never
come back through it at all, because a caller is handed a signed URL and
fetches the bytes itself.

A file record is created only once its bytes are stored, so there is no
half-uploaded state anywhere — every row here is a file that can be downloaded.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import files as files_q
from app.models.errors import Conflict, NotFound
from app.utils import storage

# Short-lived and single-object: long enough to start a download, worthless by
# the time it turns up in anyone's log.
DOWNLOAD_URL_TTL_SECONDS = 300

MAX_FILE_BYTES = 100 * 1024 * 1024

STORAGE_CATEGORY = "files"


async def upload_file(
    db: AsyncSession,
    *,
    organization_id: str,
    filename: str,
    mime_type: str | None,
    content: bytes,
) -> File:
    """Store an uploaded file.

    Everything recorded about it is read off the bytes rather than declared by
    the client — the size and the hash are measured here, so there is nothing
    to take on trust and nothing to go back and verify.
    """
    if len(content) > MAX_FILE_BYTES:
        raise Conflict(f"A file may be at most {MAX_FILE_BYTES} bytes")

    stored = await storage.save_bytes(
        content,
        organization_id=organization_id,
        category=STORAGE_CATEGORY,
        filename=filename,
        mime_type=mime_type,
    )
    file = await files_q.create_file(
        db,
        organization_id=organization_id,
        filename=filename,
        storage_key=stored.key,
        mime_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    await db.commit()
    return file


async def get_file(db: AsyncSession, *, file_id: str, organization_id: str) -> File:
    file = await files_q.get_file(db, file_id=file_id, organization_id=organization_id)
    if file is None or file.archived_at is not None:
        raise NotFound(f"File {file_id} not found")
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
    """`scope_id` narrows this to what one session produced.

    Without it a caller sees everything the organization has, uploads and
    outputs alike — which is what a file browser wants, and not what someone
    collecting the results of one run does.
    """
    return await files_q.list_files(
        db,
        organization_id=organization_id,
        scope_id=scope_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def download_url(db: AsyncSession, *, file_id: str, organization_id: str) -> str:
    """A signed URL for the caller to follow.

    Redirecting rather than streaming keeps the bytes out of this process. The
    URL is short-lived and names one object — it is not the bucket path, which
    stays internal.
    """
    file = await get_file(db, file_id=file_id, organization_id=organization_id)
    return await storage.presigned_download_url(
        file.storage_key,
        expires_in=DOWNLOAD_URL_TTL_SECONDS,
    )


async def delete_file(db: AsyncSession, *, file_id: str, organization_id: str) -> File:
    """Remove the bytes, then the row.

    Failing between the two leaves a row pointing at a deleted object, which
    shows up as a broken download. The other order loses the key and leaks the
    bytes forever, which nobody would ever notice.
    """
    file = await get_file(db, file_id=file_id, organization_id=organization_id)
    await storage.delete_object(file.storage_key)
    await files_q.delete_file(db, file)
    await db.commit()
    return file


__all__ = [
    "MAX_FILE_BYTES",
    "delete_file",
    "download_url",
    "get_file",
    "list_files",
    "upload_file",
]
