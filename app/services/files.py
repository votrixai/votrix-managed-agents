"""File use cases.

Files are what a session works on and what it hands back. Uploads come in
through this service and go straight out to object storage; downloads never
come back through it at all, because a caller is handed a signed URL and
fetches the bytes itself.

A file record is created only once its bytes are stored, so there is no
half-uploaded state anywhere — every row here is a file that can be downloaded.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import files as files_q
from app.models.errors import Conflict, InvalidRequest, NotFound, PayloadTooLarge
from app.utils import storage

# Short-lived and single-object: long enough to start a download, worthless by
# the time it turns up in anyone's log.
DOWNLOAD_URL_TTL_SECONDS = 300

# The ceiling on a stored file. Reachable at last: while the only way in was a
# request body, the front end refused anything over 32 MiB before this was ever
# consulted, so the number here described nothing.
#
# What it costs at this size is the fetch, not the storage — every byte is
# pulled in and pushed back out through this process, so the limit is really a
# statement about how long one import may hold a concurrency slot.
MAX_FILE_BYTES = 500 * 1024 * 1024

STORAGE_CATEGORY = "files"

# How long the whole fetch may take. It has to outlive the largest file this
# will accept — at 500 MB that is only a few seconds between clouds, but a
# slower source must not be cut off mid-transfer — and stay well inside the
# platform's own request timeout so a hung source fails here, with a message,
# rather than there.
IMPORT_TIMEOUT_SECONDS = 1800.0


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


async def import_file(
    db: AsyncSession,
    *,
    organization_id: str,
    url: str,
    filename: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    scope_id: str | None = None,
) -> File:
    """Store a file by fetching it, rather than by being handed its bytes.

    Same contract as `upload_file` — the size and the hash are still measured
    here, not declared — but the bytes arrive over a URL the caller signed
    instead of in a request body. That is the whole point: a request body on
    Cloud Run stops at 32 MiB, before any of this runs, so `MAX_FILE_BYTES`
    was a limit no upload could ever reach.

    `size_bytes` and `sha256` are optional and are checks, never inputs. What
    is recorded is what arrived; if the caller said what to expect and the two
    disagree, the object is removed rather than left to be read as whole.
    """

    await _check_import_url(url)

    # Give this request's database connection back before the fetch starts.
    # Authenticating the request already opened a transaction on it, and the
    # pool it came from is deliberately tiny — five connections, ten with
    # overflow, against a pooler that refuses the sixteenth client. A fetch
    # runs for as long as the source needs it to, so holding a connection
    # across one would let a handful of concurrent imports starve every other
    # request in the process. One is taken again, briefly, for the insert at
    # the end.
    await db.commit()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(IMPORT_TIMEOUT_SECONDS, connect=15.0),
        # A redirect is a second URL nobody validated, and the obvious way to
        # walk this fetch somewhere it was not allowed to go.
        follow_redirects=False,
    ) as client:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise InvalidRequest(
                    f"The source answered {response.status_code} for that URL"
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                # Refused before a byte moves, which the request-body path
                # could never do.
                raise PayloadTooLarge(f"A file may be at most {MAX_FILE_BYTES} bytes")
            try:
                stored = await storage.save_stream(
                    response.aiter_bytes(),
                    organization_id=organization_id,
                    category=STORAGE_CATEGORY,
                    filename=filename,
                    mime_type=mime_type or response.headers.get("content-type"),
                    max_bytes=MAX_FILE_BYTES,
                )
            except storage.ObjectTooLarge as exc:
                raise PayloadTooLarge(
                    f"A file may be at most {MAX_FILE_BYTES} bytes"
                ) from exc

    if size_bytes is not None and stored.size_bytes != size_bytes:
        await storage.delete_object(stored.key)
        raise Conflict(
            f"That URL gave {stored.size_bytes} bytes, not the {size_bytes} declared"
        )
    if sha256 and stored.sha256 != sha256.strip().lower():
        await storage.delete_object(stored.key)
        raise Conflict("The fetched bytes do not match the declared sha256")

    file = await files_q.create_file(
        db,
        organization_id=organization_id,
        filename=filename,
        storage_key=stored.key,
        mime_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        scope_id=scope_id,
    )
    await db.commit()
    return file


async def _check_import_url(url: str) -> None:
    """Refuse anything that is not a public HTTPS address.

    This endpoint makes the service fetch a URL somebody else chose, which is
    the shape of every SSRF. HTTPS only, and every address the name resolves to
    has to be routable on the public internet — that is what keeps `10.x`, the
    metadata service and `localhost` out of reach.

    Resolution here and connection later are two separate lookups, so a name
    that answers differently each time can still slip past. Closing that means
    pinning the address into the connection, which is worth doing if this ever
    accepts URLs from outside our own services.
    """

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidRequest("An import URL must be https")
    if not parsed.hostname:
        raise InvalidRequest("An import URL must name a host")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise InvalidRequest(f"Could not resolve {parsed.hostname}") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise InvalidRequest(
                f"{parsed.hostname} resolves to a non-public address"
            )


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


async def download_url(
    db: AsyncSession,
    *,
    file_id: str,
    organization_id: str,
    inline: bool = False,
) -> str:
    """A signed URL for the caller to follow.

    Redirecting rather than streaming keeps the bytes out of this process. The
    URL is short-lived and names one object — it is not the bucket path, which
    stays internal.

    The file's own name is signed into it. A key starts with a content
    fingerprint, so without this a browser saves `a1b2c3d4_report.pdf`, and the
    row is the only place the name it was written under still exists.
    """
    file = await get_file(db, file_id=file_id, organization_id=organization_id)
    return await storage.presigned_download_url(
        file.storage_key,
        expires_in=DOWNLOAD_URL_TTL_SECONDS,
        filename=file.filename,
        inline=inline,
    )


async def delete_file(db: AsyncSession, *, file_id: str, organization_id: str) -> File:
    """Refuse if a session still holds it; otherwise the row, then the bytes.

    A file mounted into a session is referenced by `session_files`, and
    sessions are only soft-deleted, so that reference never goes away. The
    check is here to say so plainly. Without it the delete reached the database
    and came back as a foreign-key violation — a 500 for a request that was
    never going to work.

    The row goes before the bytes, which is the opposite of what this used to
    do and matters more than the reasoning it replaces. Removing the object
    first meant a failure on the *second* step destroyed the bytes and rolled
    the row back: the file stayed in every listing and every download of it
    404ed. Deleting the row first can at worst leave an unreferenced object in
    the bucket, which costs pennies and breaks nothing.
    """
    file = await get_file(db, file_id=file_id, organization_id=organization_id)
    holders = await files_q.sessions_holding(db, file)
    if holders:
        raise Conflict(
            f"File {file_id} is attached to {holders} session(s) and cannot be deleted"
        )

    key = file.storage_key
    await files_q.delete_file(db, file)
    await db.commit()
    await storage.delete_object(key)
    return file


__all__ = [
    "MAX_FILE_BYTES",
    "delete_file",
    "download_url",
    "get_file",
    "list_files",
    "upload_file",
]
