from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SessionEventIdempotency
from app.ids import new_id
from app.workspace import workspace_id_or_default


async def get_submission(
    db: AsyncSession,
    *,
    session_id: str,
    key_hash: str,
    workspace_id: str | None = None,
) -> SessionEventIdempotency | None:
    result = await db.execute(
        select(SessionEventIdempotency).where(
            SessionEventIdempotency.workspace_id == workspace_id_or_default(workspace_id),
            SessionEventIdempotency.session_id == session_id,
            SessionEventIdempotency.key_hash == key_hash,
        )
    )
    return result.scalar_one_or_none()


async def create_submission(
    db: AsyncSession,
    *,
    session_id: str,
    key_hash: str,
    request_sha256: str,
    work_id: str | None,
    response_status: int,
    response_body: dict,
    workspace_id: str | None = None,
) -> SessionEventIdempotency:
    submission = SessionEventIdempotency(
        id=new_id("idem"),
        workspace_id=workspace_id_or_default(workspace_id),
        session_id=session_id,
        key_hash=key_hash,
        request_sha256=request_sha256,
        work_id=work_id,
        response_status=response_status,
        response_body=response_body,
    )
    db.add(submission)
    await db.flush()
    return submission
