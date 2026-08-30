from typing import Literal

from fastapi import APIRouter, File as UploadField, Form, UploadFile, status
from fastapi.responses import RedirectResponse

from app.db.models import File
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.files import FileResponse, FileScope
from app.models.errors import InvalidRequest
from app.routers.deps import Db, OrganizationId
from app.services import files as service

router = APIRouter(prefix="/v1/files", tags=["files"])


@router.post("", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    db: Db,
    organization_id: OrganizationId,
    file: UploadFile | None = UploadField(None),
    url: str | None = Form(None),
    filename: str | None = Form(None),
    mime_type: str | None = Form(None),
    size_bytes: int | None = Form(None),
    sha256: str | None = Form(None),
    idempotency_key: str | None = Form(None, min_length=1, max_length=255),
):
    """Store a file, either by sending its bytes or by naming where they are.

    `file` is the original form: filename and content type ride along in the
    multipart part rather than being declared separately, and the size is
    whatever arrived — so nothing about the file is a claim to be checked
    afterwards.

    `url` is the same thing for bytes too big to send. This service runs behind
    a front end that refuses a request body over 32 MiB, before any of this
    runs, which made `MAX_FILE_BYTES` a limit no upload could reach. Given a
    URL instead, the fetch happens here and streams straight into storage, so
    the declared limit is finally the real one. The response is identical
    either way, and so is the rule about claims: `size_bytes` and `sha256` are
    checked against what actually arrived, never recorded in its place.
    """

    if (file is None) == (url is None):
        raise InvalidRequest("Send either a file or a url, not both and not neither")

    if url is not None:
        if not filename:
            # A URL's path is not a name: it can be a signed key, a hash, or
            # nothing at all, and the row would be the only place the file's
            # real name should have been.
            raise InvalidRequest("filename is required when importing from a url")
        created = await service.import_file(
            db,
            organization_id=organization_id,
            url=url,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            idempotency_key=idempotency_key,
        )
        return to_file(created)

    created = await service.upload_file(
        db,
        organization_id=organization_id,
        filename=filename or file.filename or "upload",
        mime_type=mime_type or file.content_type,
        content=await file.read(),
        idempotency_key=idempotency_key,
    )
    return to_file(created)


@router.get("", response_model=ListResponse[FileResponse])
async def list_files(
    db: Db,
    organization_id: OrganizationId,
    scope_id: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    """Everything, or `?scope_id=<session id>` for one session's output."""
    found = await service.list_files(
        db,
        organization_id=organization_id,
        scope_id=scope_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(found, to_file)


@router.get("/{file_id}", response_model=FileResponse)
async def retrieve_file(file_id: str, db: Db, organization_id: OrganizationId):
    """What the file is, without fetching it — name, size, where it came from.

    Answers for an archived file too, with `archived: true`. Something holding
    an id needs to be able to ask what became of it, and a 404 says only that
    the id was never real.
    """
    file = await service.get_readable_file(
        db, file_id=file_id, organization_id=organization_id
    )
    return to_file(file)


@router.get(
    "/{file_id}/content",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    responses={
        307: {
            "description": "Redirect to a short-lived URL for the File bytes.",
            "headers": {
                "Location": {
                    "description": "Short-lived URL for downloading the File.",
                    "schema": {"type": "string", "format": "uri"},
                }
            },
        }
    },
)
async def download_file(
    file_id: str,
    db: Db,
    organization_id: OrganizationId,
    disposition: Literal["attachment", "inline"] = "attachment",
):
    """Redirect to a short-lived signed URL rather than stream the bytes.

    307 rather than 302 so the method survives the redirect, and the URL names
    one object for a few minutes — it is not the bucket path.

    `disposition` is the caller's to choose because only the caller knows what
    it is doing with the file: a download button wants the browser to save it,
    an `<img>` or a preview pane wants it shown. Either way the name signed
    into the URL is the one the file was written under.
    """
    url = await service.download_url(
        db,
        file_id=file_id,
        organization_id=organization_id,
        inline=disposition == "inline",
    )
    return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.delete("/{file_id}", response_model=DeletedResponse)
async def delete_file(file_id: str, db: Db, organization_id: OrganizationId):
    file = await service.delete_file(db, file_id=file_id, organization_id=organization_id)
    return DeletedResponse(id=file.id)


def to_file(file: File) -> FileResponse:
    """`storage_key` is not here and never will be. Where an object lives in the
    bucket is ours; callers reach it through a signed URL or not at all."""
    return FileResponse(
        id=file.id,
        filename=file.filename,
        mime_type=file.mime_type,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        scope=FileScope.for_id(file.scope_id) if file.scope_id else None,
        archived=file.archived_at is not None,
        created_at=file.created_at,
        updated_at=file.updated_at,
    )
