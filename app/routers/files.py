from fastapi import APIRouter, File as UploadField, UploadFile, status
from fastapi.responses import RedirectResponse

from app.db.models import File
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.files import FileResponse, FileScope
from app.routers.deps import Db, OrganizationId
from app.services import files as service

router = APIRouter(prefix="/v1/files", tags=["files"])


@router.post("", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    db: Db,
    organization_id: OrganizationId,
    file: UploadFile = UploadField(...),
):
    """Upload a file, in one call.

    Filename and content type ride along in the multipart part rather than
    being declared separately, and the size is whatever arrived — so nothing
    about the file is a claim that has to be checked afterwards.
    """
    created = await service.upload_file(
        db,
        organization_id=organization_id,
        filename=file.filename or "upload",
        mime_type=file.content_type,
        content=await file.read(),
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
    """What the file is, without fetching it — name, size, where it came from."""
    file = await service.get_file(db, file_id=file_id, organization_id=organization_id)
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
async def download_file(file_id: str, db: Db, organization_id: OrganizationId):
    """Redirect to a short-lived signed URL rather than stream the bytes.

    307 rather than 302 so the method survives the redirect, and the URL names
    one object for a few minutes — it is not the bucket path.
    """
    url = await service.download_url(db, file_id=file_id, organization_id=organization_id)
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
        created_at=file.created_at,
        updated_at=file.updated_at,
    )
