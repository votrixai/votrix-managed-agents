"""Direct access to a container, for callers that are not a conversation.

Named as verbs rather than shaped as a REST collection. What a caller does
here is create a container, run things in it and delete it; `exec` is not a
POST to a collection of execs under any reading that helps anyone.
"""

from fastapi import APIRouter

from app.db.models import File, Sandbox
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.files import FileResponse, FileScope
from app.models.sandbox import (
    ExecResponse,
    SandboxCreateRequest,
    SandboxDeleteRequest,
    SandboxDownloadRequest,
    SandboxExecRequest,
    SandboxExecResultRequest,
    SandboxGetRequest,
    SandboxListRequest,
    SandboxResponse,
    SandboxUploadRequest,
)
from app.routers.deps import Db, OrganizationId
from app.services import sandboxes as service

router = APIRouter(prefix="/v1/sandbox", tags=["sandbox"])


def to_sandbox(row: Sandbox) -> SandboxResponse:
    return SandboxResponse(
        id=row.id,
        environment_id=row.environment_id,
        state=row.state,
        session_id=row.session_id,
        ttl_seconds=row.ttl_seconds,
        network_access=row.network_access,
        expires_at=row.expires_at,
        last_active_at=row.last_active_at,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_file(file: File) -> FileResponse:
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


@router.post("/create", response_model=SandboxResponse)
async def create_sandbox(
    body: SandboxCreateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Start a container and return the id that addresses it.

    It dies on its own after `ttl_seconds`, and every later call pushes that
    out again. Deleting it is the cheap path, not the safe one: the TTL is
    what makes a container nobody deletes cost a bounded amount.
    """
    row = await service.create_sandbox(
        db,
        organization_id=organization_id,
        environment_id=body.environment_id,
        ttl_seconds=body.ttl_seconds,
        network_access=body.network_access,
    )
    return to_sandbox(row)


@router.post("/get", response_model=SandboxResponse)
async def get_sandbox(
    body: SandboxGetRequest,
    db: Db,
    organization_id: OrganizationId,
):
    return to_sandbox(
        await service.get_sandbox(
            db, sandbox_id=body.sandbox_id, organization_id=organization_id
        )
    )


@router.post("/list", response_model=ListResponse[SandboxResponse])
async def list_sandboxes(
    body: SandboxListRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Every container this organization has made, so a leak is visible."""
    found = await service.list_sandboxes(
        db,
        organization_id=organization_id,
        state=body.state,
        include_session_owned=body.include_session_owned,
        limit=body.limit,
        before_id=body.before_id,
        after_id=body.after_id,
    )
    return page_of(found, to_sandbox)


@router.post("/delete", response_model=DeletedResponse)
async def delete_sandbox(
    body: SandboxDeleteRequest,
    db: Db,
    organization_id: OrganizationId,
):
    row = await service.delete_sandbox(
        db, sandbox_id=body.sandbox_id, organization_id=organization_id
    )
    return DeletedResponse(id=row.id)


@router.post("/exec", response_model=ExecResponse)
async def exec_command(
    body: SandboxExecRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Run one command, and wait up to `wait_seconds` for it to finish.

    A command that outlives the wait comes back as `running` with an id; ask
    `get_result` for it afterwards. The command itself is unaffected either
    way — it runs in the container, detached from this request.
    """
    return ExecResponse(
        **await service.exec_command(
            db,
            sandbox_id=body.sandbox_id,
            organization_id=organization_id,
            command=body.command,
            cwd=body.cwd,
            timeout_seconds=body.timeout_seconds,
            wait_seconds=body.wait_seconds,
        )
    )


@router.post("/get_result", response_model=ExecResponse)
async def get_result(
    body: SandboxExecResultRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Where a command got to. Answers immediately, whatever the answer is."""
    return ExecResponse(
        **await service.exec_result(
            db,
            sandbox_id=body.sandbox_id,
            organization_id=organization_id,
            exec_id=body.exec_id,
        )
    )


@router.post("/upload", response_model=SandboxResponse)
async def upload_file(
    body: SandboxUploadRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Put a stored File into the container.

    The bytes go from object storage to the container over a signed URL that
    is good for one object and a few minutes. They do not pass through this
    service, which is what lets a large input cost the same as a small one
    here.
    """
    row = await service.upload_file(
        db,
        sandbox_id=body.sandbox_id,
        organization_id=organization_id,
        file_id=body.file_id,
        path=body.path,
    )
    return to_sandbox(row)


@router.post("/download", response_model=FileResponse)
async def download_file(
    body: SandboxDownloadRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Take one file out of the container and make it addressable."""
    file = await service.download_file(
        db,
        sandbox_id=body.sandbox_id,
        organization_id=organization_id,
        path=body.path,
        filename=body.filename,
    )
    return to_file(file)
