from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_scope
from app.db.models import ManagedResource, ManagedSession
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.session_state import SESSION_RESCHEDULING, SESSION_TERMINATED
from app.workspace import workspace_id_or_default

logger = structlog.get_logger()

RUNNABLE_WORK_STATUSES = {"queued", "rescheduling"}
LEASED_WORK_STATUSES = {"leased", "running"}


class WorkLeaseError(RuntimeError):
    pass


async def enqueue_session_run(
    db: AsyncSession,
    session: ManagedSession,
    *,
    trigger: str,
    metadata: dict[str, Any] | None = None,
) -> ManagedResource:
    return await res_q.create_resource(
        db,
        resource_type="environment_work",
        parent_id=session.environment_id,
        name=f"session:{session.id}",
        status="queued",
        data={
            "session_id": session.id,
            "workspace_id": session.workspace_id,
            "trigger": trigger,
            "attempt": 0,
            "metadata": metadata or {},
            "queued_at": _utcnow_iso(),
        },
        workspace_id=session.workspace_id,
    )


async def execute_work_item(work_id: str, *, worker_id: str | None = None) -> None:
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        if work is None:
            return
        data = dict(work.data or {})
        if work.status == "running" and data.get("started_at"):
            return
        if work.status in LEASED_WORK_STATUSES:
            _require_lease_owner(work, worker_id=worker_id, action="execute")
        elif is_work_ready_for_execution(work):
            data["attempt"] = int(data.get("attempt") or 0) + 1
        else:
            return
        data["started_at"] = _utcnow_iso()
        if worker_id:
            data["started_by"] = worker_id
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()

    try:
        from app.runtime.runner import run_session_turn

        await run_session_turn(
            str(data["session_id"]),
            workspace_id=str(data.get("workspace_id") or work.workspace_id),
        )
    except Exception as exc:
        logger.exception("work_item_failed", work_id=work_id)
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            if work is not None:
                data = dict(work.data or {})
                data["error"] = {"type": exc.__class__.__name__, "message": str(exc)}
                data["finished_at"] = _utcnow_iso()
                await res_q.update_resource(db, work, data=data, status="error")
                await db.commit()
        return

    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        if work is None:
            return
        session = await sessions_q.get_session(
            db,
            str((work.data or {}).get("session_id")),
            workspace_id=work.workspace_id,
        )
        data = dict(work.data or {})
        data["finished_at"] = _utcnow_iso()
        data["session_status"] = session.status if session is not None else "missing"
        status = "completed"
        if session is not None and session.status == SESSION_RESCHEDULING:
            stop_reason = dict(session.stop_reason or {})
            status = "rescheduling"
            data["rescheduled_at"] = _utcnow_iso()
            data["retry_at"] = stop_reason.get("retry_at")
            data["retry_after_seconds"] = stop_reason.get("retry_after_seconds")
            data["stop_reason"] = stop_reason
        if session is not None and session.status == SESSION_TERMINATED and (session.stop_reason or {}).get("type") == "error":
            status = "error"
        if work.status == "stopped":
            status = "stopped"
        await res_q.update_resource(db, work, data=data, status=status)
        await db.commit()


def should_execute_inline(environment_config: dict[str, Any] | None) -> bool:
    return (environment_config or {}).get("type") != "self_hosted"


async def lease_next_work(
    db: AsyncSession,
    *,
    environment_id: str,
    worker_id: str,
    lease_seconds: int = 60,
    workspace_id: str | None = None,
) -> ManagedResource | None:
    return await _lease_next_work(
        db,
        environment_id=environment_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        workspace_id=workspace_id_or_default(workspace_id),
    )


async def lease_next_work_for_worker(
    db: AsyncSession,
    *,
    environment_id: str | None,
    worker_id: str,
    lease_seconds: int = 60,
) -> ManagedResource | None:
    """Atomically lease work across tenants for the trusted worker process."""
    return await _lease_next_work(
        db,
        environment_id=environment_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        workspace_id=None,
    )


async def _lease_next_work(
    db: AsyncSession,
    *,
    environment_id: str | None,
    worker_id: str,
    lease_seconds: int,
    workspace_id: str | None,
) -> ManagedResource | None:
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.resource_type == "environment_work",
            ManagedResource.deleted_at.is_(None),
            ManagedResource.status.in_(RUNNABLE_WORK_STATUSES | LEASED_WORK_STATUSES),
        )
        .order_by(ManagedResource.created_at.asc(), ManagedResource.id.asc())
        .limit(1000)
        .with_for_update(skip_locked=True)
    )
    if environment_id is not None:
        stmt = stmt.where(ManagedResource.parent_id == environment_id)
    if workspace_id is not None:
        stmt = stmt.where(ManagedResource.workspace_id == workspace_id)
    result = await db.execute(stmt)
    candidates = list(result.scalars().all())
    now = datetime.now(timezone.utc)
    for work in candidates:
        if _work_available_for_lease(work, now):
            return await lease_work(db, work, worker_id=worker_id, lease_seconds=lease_seconds, now=now)
    return None


async def lease_work(
    db: AsyncSession,
    work: ManagedResource,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> ManagedResource:
    now = now or datetime.now(timezone.utc)
    data = dict(work.data or {})
    data["attempt"] = int(data.get("attempt") or 0) + 1
    data["lease"] = {
        "worker_id": worker_id,
        "leased_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
    }
    await res_q.update_resource(db, work, data=data, status="leased")
    await db.flush()
    return work


async def ack_work(
    db: AsyncSession,
    work: ManagedResource,
    *,
    worker_id: str | None = None,
) -> ManagedResource:
    _require_lease_owner(work, worker_id=worker_id, action="ack")
    data = dict(work.data or {})
    data["acked_at"] = _utcnow_iso()
    if worker_id:
        data["acked_by"] = worker_id
    await res_q.update_resource(db, work, data=data, status="running")
    return work


async def heartbeat_work(
    db: AsyncSession,
    work: ManagedResource,
    *,
    worker_id: str | None,
    lease_seconds: int = 60,
    payload: dict[str, Any] | None = None,
) -> ManagedResource:
    _require_lease_owner(work, worker_id=worker_id, action="heartbeat")
    data = dict(work.data or {})
    now = datetime.now(timezone.utc)
    data["last_heartbeat_at"] = now.isoformat()
    data["last_heartbeat"] = payload or {}
    lease = dict(data.get("lease") or {})
    if worker_id:
        lease["worker_id"] = worker_id
    lease["expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
    data["lease"] = lease
    await res_q.update_resource(db, work, data=data, status="running")
    return work


async def stop_work(db: AsyncSession, work: ManagedResource, *, payload: dict[str, Any]) -> ManagedResource:
    data = dict(work.data or {})
    data["stop"] = payload
    data["stopped_at"] = _utcnow_iso()
    await res_q.update_resource(db, work, data=data, status="stopped")
    return work


def _work_ready_for_execution(work: ManagedResource, now: datetime) -> bool:
    if work.status == "queued":
        return True
    if work.status == "rescheduling":
        return _retry_at_due(work, now)
    return False


def is_work_ready_for_execution(work: ManagedResource, now: datetime | None = None) -> bool:
    return _work_ready_for_execution(work, now or datetime.now(timezone.utc))


def is_work_available_for_lease(work: ManagedResource, now: datetime | None = None) -> bool:
    return _work_available_for_lease(work, now or datetime.now(timezone.utc))


def _work_available_for_lease(work: ManagedResource, now: datetime) -> bool:
    return _work_ready_for_execution(work, now) or _lease_expired(work, now)


def _retry_at_due(work: ManagedResource, now: datetime) -> bool:
    retry_at = (work.data or {}).get("retry_at")
    if not isinstance(retry_at, str) or not retry_at:
        return True
    try:
        return datetime.fromisoformat(retry_at) <= now
    except ValueError:
        return True


def _lease_expired(work: ManagedResource, now: datetime) -> bool:
    if work.status not in LEASED_WORK_STATUSES:
        return False
    expires_at = ((work.data or {}).get("lease") or {}).get("expires_at")
    if not isinstance(expires_at, str):
        return True
    try:
        return datetime.fromisoformat(expires_at) <= now
    except ValueError:
        return True


def _require_lease_owner(work: ManagedResource, *, worker_id: str | None, action: str) -> None:
    if work.status not in LEASED_WORK_STATUSES:
        raise WorkLeaseError(f"Cannot {action} work item while status is {work.status}")
    lease = (work.data or {}).get("lease") or {}
    lease_worker_id = lease.get("worker_id")
    if not lease_worker_id:
        return
    if not worker_id:
        raise WorkLeaseError(f"worker_id is required to {action} this leased work item")
    if str(lease_worker_id) != str(worker_id):
        raise WorkLeaseError(f"Worker {worker_id} does not own this work lease")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
