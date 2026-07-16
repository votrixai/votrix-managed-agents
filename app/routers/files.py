import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_access
from app.config import get_settings
from app.content_scan import UnsafeContentError, validate_upload_content
from app.db.engine import get_session
from app.db.queries import resources as res_q
from app.governance_runtime import governance_service
from app.models.common import FlexibleApiModel, ListResponse
from app.models.files import (
    FileDeletedResponse,
    FileResponse,
    PresignedFileUploadResponse,
)
from app.models.resources import deleted_response, resource_to_response
from app.pagination import paginate_by_id, sort_by_created_at
from app.storage import (
    StorageConfigurationError,
    copy_file,
    create_presigned_upload_url,
    delete_file as delete_stored_file,
    download_file_with_type,
    get_file_info,
    is_object_storage_backend,
    object_key,
    object_storage_backend_label,
    save_file_bytes,
    should_store_in_object_storage,
)
from app.organization import resolve_organization_id

UPLOAD_READ_CHUNK_BYTES = 64 * 1024

router = APIRouter(
    prefix="/v1/files",
    tags=["files"],
    dependencies=[Depends(require_api_access)],
)


class PresignFileBody(FlexibleApiModel):
    filename: str
    mime_type: str = "application/octet-stream"
    namespace: str = "vma"
    expires_in: int = 900


class CompleteFileBody(FlexibleApiModel):
    key: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


@router.post("", response_model=FileResponse, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
):
    content = await _read_upload_file_bounded(
        file,
        max_bytes=get_settings().vma_max_file_upload_bytes,
    )
    _scan_upload_content(content, label="File upload")
    mime_type = file.content_type or "application/octet-stream"
    sha256 = hashlib.sha256(content).hexdigest()
    existing = await _find_deduplicated_file(db, sha256=sha256)
    await _enforce_organization_storage_quota(db, incoming_bytes=len(content))
    if existing is None:
        try:
            should_store_in_object_storage()
            stored = await save_file_bytes(
                content,
                mime_type,
                namespace="vma",
                filename=file.filename or "upload",
                category="files",
                organization_id=resolve_organization_id(),
            )
        except StorageConfigurationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        storage_backend = stored.backend
        storage_key = stored.key
        storage_url = None
        stored_size_bytes = stored.size_bytes
        stored_sha256 = stored.sha256 or sha256
        data = {
            "filename": file.filename,
            "mime_type": mime_type,
        }
    else:
        storage_backend = existing.storage_backend
        storage_key = existing.storage_key
        storage_url = None
        stored_size_bytes = existing.size_bytes
        stored_sha256 = existing.sha256 or sha256
        data = {
            "filename": file.filename,
            "mime_type": mime_type,
            "deduplicated_from_file_id": existing.id,
        }

    resource = await res_q.create_resource(
        db,
        resource_type="file",
        name=file.filename,
        filename=file.filename,
        content=None,
        content_type=mime_type,
        data=data,
        storage_backend=storage_backend,
        storage_key=storage_key,
        storage_url=storage_url,
        size_bytes=stored_size_bytes,
        sha256=stored_sha256,
    )
    await db.commit()
    return resource_to_response(resource, public_type="file")


@router.post("/presign", response_model=PresignedFileUploadResponse)
async def presign_file_upload(body: PresignFileBody):
    try:
        if not should_store_in_object_storage():
            raise HTTPException(status_code=503, detail="Object storage is not configured")
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    key = object_key(
        namespace=body.namespace,
        category="staged-uploads",
        filename=body.filename,
        organization_id=resolve_organization_id(),
    )
    upload_url = await create_presigned_upload_url(
        key,
        body.mime_type,
        expires_in=body.expires_in,
    )
    return {
        "type": "file_upload_url",
        "key": key,
        "upload_url": upload_url,
        "method": "PUT",
        "headers": {"content-type": body.mime_type},
        "expires_in": body.expires_in,
    }


@router.post("/complete", response_model=FileResponse, status_code=201)
async def complete_file_upload(body: CompleteFileBody, db: AsyncSession = Depends(get_session)):
    try:
        if not should_store_in_object_storage():
            raise HTTPException(status_code=503, detail="Object storage is not configured")
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _validate_staged_upload_key(body.key)
    info = await get_file_info(body.key)
    mime_type = body.mime_type or info.get("ContentType") or "application/octet-stream"
    head_size = info.get("ContentLength")
    for declared_size in (body.size_bytes, head_size):
        if declared_size is None:
            continue
        _enforce_size_limit(
            int(declared_size),
            get_settings().vma_max_file_upload_bytes,
            label="File upload",
        )
    staged_content, _stored_content_type = await download_file_with_type(body.key)
    actual_size = len(staged_content)
    _enforce_size_limit(
        actual_size,
        get_settings().vma_max_file_upload_bytes,
        label="File upload",
    )
    if body.size_bytes is not None and int(body.size_bytes) != actual_size:
        raise HTTPException(status_code=422, detail="File upload size_bytes does not match staged object")
    if head_size is not None and int(head_size) != actual_size:
        raise HTTPException(status_code=422, detail="File upload ContentLength does not match staged object")
    _scan_upload_content(staged_content, label="File upload")
    staged_sha256 = hashlib.sha256(staged_content).hexdigest()
    if body.sha256 and body.sha256 != staged_sha256:
        raise HTTPException(status_code=422, detail="File upload sha256 does not match staged object")
    sha256 = body.sha256 or staged_sha256
    filename = body.filename or body.key.split("/")[-1]
    existing = await _find_deduplicated_file(db, sha256=sha256)
    await _enforce_organization_storage_quota(db, incoming_bytes=actual_size)
    if existing is None:
        permanent_key = object_key(
            namespace="vma",
            category="files",
            filename=filename,
            content_sha256=sha256,
            organization_id=resolve_organization_id(),
        )
        await copy_file(body.key, permanent_key, content_type=mime_type)
        storage_key = permanent_key
        storage_url = None
        storage_backend = object_storage_backend_label()
        data = {
            "filename": filename,
            "mime_type": mime_type,
        }
    else:
        storage_key = existing.storage_key
        storage_url = None
        storage_backend = existing.storage_backend
        data = {
            "filename": filename,
            "mime_type": mime_type,
            "deduplicated_from_file_id": existing.id,
        }
    await delete_stored_file(body.key)
    resource = await res_q.create_resource(
        db,
        resource_type="file",
        name=filename,
        filename=filename,
        content_type=mime_type,
        data=data,
        storage_backend=storage_backend,
        storage_key=storage_key,
        storage_url=storage_url,
        size_bytes=actual_size,
        sha256=sha256,
    )
    await db.commit()
    return resource_to_response(resource, public_type="file")


@router.get("", response_model=ListResponse[FileResponse])
async def list_files(
    limit: int = 50,
    after_id: str | None = None,
    before_id: str | None = None,
    scope_id: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    scope_file_ids: set[str] = set()
    if scope_id is not None:
        scope_file_ids = await _file_ids_for_scope(db, scope_id)
        files = await res_q.list_files_for_session_scope(
            db,
            session_id=scope_id,
            referenced_file_ids=scope_file_ids,
            limit=1000,
        )
    else:
        files = await res_q.list_resources(db, resource_type="file", limit=1000)
    files = sort_by_created_at(files, order="desc")
    return paginate_by_id(
        [_file_response(f, scope_id=scope_id if f.id in scope_file_ids else None) for f in files],
        limit=limit,
        after_id=after_id,
        before_id=before_id,
    )


@router.get("/{file_id}", response_model=FileResponse)
async def retrieve_file_metadata(file_id: str, db: AsyncSession = Depends(get_session)):
    file = await res_q.get_resource(db, resource_id=file_id, resource_type="file")
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    return resource_to_response(file, public_type="file")


@router.get("/{file_id}/content")
async def download_file(file_id: str, db: AsyncSession = Depends(get_session)):
    file = await res_q.get_resource(db, resource_id=file_id, resource_type="file")
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not (is_object_storage_backend(file.storage_backend) and file.storage_key):
        raise HTTPException(status_code=500, detail="File object is not stored in object storage")
    content, stored_content_type = await download_file_with_type(file.storage_key)
    content_type = stored_content_type or file.content_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{file.filename or file.id}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{file_id}", response_model=FileDeletedResponse)
async def delete_file(file_id: str, db: AsyncSession = Depends(get_session)):
    file = await res_q.get_resource(db, resource_id=file_id, resource_type="file")
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    mounted = await res_q.find_session_resource_referencing_file(
        db,
        file_id=file.id,
    )
    if mounted is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "File is mounted by an active Session resource; delete it through the "
                "Session resources API when that runtime permits removal"
            ),
        )
    if is_object_storage_backend(file.storage_backend) and file.storage_key:
        active_references = await res_q.count_resources_by_storage_key(
            db,
            resource_type="file",
            storage_backend=file.storage_backend,
            storage_key=file.storage_key,
        )
        if active_references <= 1:
            await delete_stored_file(file.storage_key)
    await res_q.delete_resource(db, file)
    await db.commit()
    return deleted_response(file, public_type="file_deleted")


def _enforce_size_limit(size_bytes: int, max_bytes: int, *, label: str) -> None:
    if max_bytes > 0 and size_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds maximum size of {max_bytes} bytes",
        )


async def _read_upload_file_bounded(file: UploadFile, *, max_bytes: int) -> bytes:
    content = bytearray()
    while True:
        read_size = UPLOAD_READ_CHUNK_BYTES
        if max_bytes > 0:
            read_size = min(read_size, max_bytes + 1 - len(content))
        chunk = await file.read(read_size)
        if not chunk:
            break
        content.extend(chunk)
        _enforce_size_limit(len(content), max_bytes, label="File upload")
    return bytes(content)


async def _enforce_organization_storage_quota(
    db: AsyncSession,
    *,
    incoming_bytes: int,
) -> None:
    if not get_settings().vma_governance_enabled:
        return
    await governance_service().enforce_storage_quota(
        db,
        resolve_organization_id(),
        incoming_bytes,
    )


def _scan_upload_content(content: bytes, *, label: str) -> None:
    try:
        validate_upload_content(content, label=label)
    except UnsafeContentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _file_response(file, *, scope_id: str | None = None) -> dict:
    response = resource_to_response(file, public_type="file")
    if scope_id is not None:
        response.setdefault("scope", {"type": "session", "id": scope_id})
    return response


async def _file_ids_for_scope(db: AsyncSession, scope_id: str) -> set[str]:
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=scope_id,
        limit=1000,
    )
    file_ids: set[str] = set()
    for resource in resources:
        data = resource.data or {}
        if data.get("type") != "file":
            continue
        file_id = data.get("file_id")
        if isinstance(file_id, str) and file_id:
            file_ids.add(file_id)
    return file_ids


async def _find_deduplicated_file(db: AsyncSession, *, sha256: str | None):
    if not sha256:
        return None
    existing = await res_q.get_resource_by_sha256(db, resource_type="file", sha256=sha256)
    if existing is None:
        return None
    if not (is_object_storage_backend(existing.storage_backend) and existing.storage_key):
        return None
    return existing


def _validate_staged_upload_key(key: str) -> None:
    organization_prefix = f"organizations/{resolve_organization_id()}/"
    if not key.startswith(organization_prefix) or "/staged-uploads/" not in key:
        raise HTTPException(
            status_code=422,
            detail="Only staged upload keys for the current organization can be completed",
        )
