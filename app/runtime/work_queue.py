from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import ManagedResource, ManagedSession
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.governance import QuotaExceededError
from app.governance_runtime import governance_service
from app.organization import current_organization, resolve_organization_id
from app.session_state import SESSION_RESCHEDULING, SESSION_TERMINATED

logger = structlog.get_logger()

RUNNABLE_WORK_STATUSES = {"queued", "rescheduling"}
LEASED_WORK_STATUSES = {"leased", "running"}
TERMINAL_WORK_OUTCOMES = {"completed", "error", "stopped", "rescheduling"}


class WorkLeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkExecutionLease:
    work_id: str
    worker_id: str
    lease_id: str
    generation: int
    attempt: int = 0


class WorkAttemptsExhausted(RuntimeError):
    def __init__(
        self,
        *,
        session_id: str,
        organization_id: str,
        work_id: str,
        attempt: int,
        max_attempts: int,
    ) -> None:
        super().__init__(f"Work item {work_id} exceeded {max_attempts} execution attempts")
        self.session_id = session_id
        self.organization_id = organization_id
        self.work_id = work_id
        self.attempt = attempt
        self.max_attempts = max_attempts


async def get_active_session_work(
    db: AsyncSession,
    session: ManagedSession,
    *,
    for_update: bool = False,
) -> ManagedResource | None:
    stmt = (
        select(ManagedResource)
        .where(
            ManagedResource.organization_id == session.organization_id,
            ManagedResource.resource_type == "environment_work",
            ManagedResource.name == f"session:{session.id}",
            ManagedResource.deleted_at.is_(None),
            ManagedResource.status.in_(RUNNABLE_WORK_STATUSES | LEASED_WORK_STATUSES),
        )
        .order_by(ManagedResource.created_at.asc(), ManagedResource.id.asc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def enqueue_session_run(
    db: AsyncSession,
    session: ManagedSession,
    *,
    trigger: str,
    metadata: dict[str, Any] | None = None,
) -> ManagedResource:
    from app.config import get_settings

    service = governance_service()
    if get_settings().vma_governance_enabled:
        token_decision = await service.preflight_model_tokens_in_session(
            db,
            organization_id=session.organization_id,
            source_type="session",
            source_id=session.id,
        )
        if not token_decision.allowed:
            raise QuotaExceededError(token_decision)

    work = await res_q.create_resource(
        db,
        resource_type="environment_work",
        parent_id=session.environment_id,
        name=f"session:{session.id}",
        status="queued",
        data={
            "session_id": session.id,
            "trigger": trigger,
            "attempt": 0,
            "metadata": metadata or {},
            "queued_at": _utcnow_iso(),
        },
        organization_id=session.organization_id,
    )
    if get_settings().vma_governance_enabled:
        actor = current_organization()
        active_decision = await service.acquire_active_work_in_session(
            db,
            organization_id=session.organization_id,
            reference_id=work.id,
            actor_id=actor.api_key_id,
            metadata={"session_id": session.id, "trigger": trigger},
        )
        if not active_decision.allowed:
            raise QuotaExceededError(active_decision)
    return work


async def execute_work_item(
    work_id: str,
    *,
    worker_id: str | None = None,
    lease_id: str | None = None,
    lease_generation: int | None = None,
    lease_seconds: int = 60,
) -> str:
    effective_worker_id = worker_id or f"inline-{uuid4().hex}"
    claimed_lease = lease_id is not None or lease_generation is not None
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        if work is None:
            return "missing"
        data = dict(work.data or {})
        if work.status in LEASED_WORK_STATUSES:
            if not claimed_lease:
                if not _lease_expired(work, datetime.now(timezone.utc)):
                    return "already_running"
                await lease_work(
                    db,
                    work,
                    worker_id=effective_worker_id,
                    lease_seconds=lease_seconds,
                )
                data = dict(work.data or {})
            else:
                _require_lease_owner(
                    work,
                    worker_id=effective_worker_id,
                    lease_id=lease_id,
                    action="execute",
                )
                current_generation = int(
                    ((work.data or {}).get("lease") or {}).get("generation") or 0
                )
                if lease_generation is not None and current_generation != lease_generation:
                    raise WorkLeaseError(
                        f"Worker {effective_worker_id} does not own the current work lease generation"
                    )
        elif is_work_ready_for_execution(work):
            await lease_work(
                db,
                work,
                worker_id=effective_worker_id,
                lease_seconds=lease_seconds,
            )
            data = dict(work.data or {})
        else:
            return "not_runnable"
        execution_lease = _execution_lease(work)
        await db.commit()

    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_execution_lease(
            execution_lease,
            stop=heartbeat_stop,
            lease_seconds=lease_seconds,
        ),
        name=f"vma-work-heartbeat-{work_id}",
    )
    executed = False
    try:
        from app.runtime.runner import run_session_turn

        executed = await run_session_turn(
            str(data["session_id"]),
            organization_id=resolve_organization_id(work.organization_id),
            work_lease=execution_lease,
        )
    except WorkAttemptsExhausted as exc:
        logger.warning(
            "work_item_attempts_exhausted",
            work_id=exc.work_id,
            attempt=exc.attempt,
            max_attempts=exc.max_attempts,
        )
        return "exhausted"
    except Exception as exc:
        logger.exception("work_item_failed", work_id=work_id)
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            if (
                work is not None
                and work.status in LEASED_WORK_STATUSES
                and _lease_matches(work, execution_lease)
            ):
                data = dict(work.data or {})
                data["error"] = {"type": exc.__class__.__name__, "message": str(exc)}
                data["finished_at"] = _utcnow_iso()
                await res_q.update_resource(db, work, data=data, status="error")
                await _release_work_quota(
                    db,
                    work,
                    actor_id=effective_worker_id,
                )
                await db.commit()
        return "error"
    finally:
        heartbeat_stop.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=5)
        except TimeoutError:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        except Exception:
            logger.exception(
                "work_lease_heartbeat_failed",
                work_id=execution_lease.work_id,
                lease_id=execution_lease.lease_id,
            )

    if not executed:
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            if work is None:
                return "superseded"
            if work.status in TERMINAL_WORK_OUTCOMES:
                return work.status
            if work.status not in LEASED_WORK_STATUSES or not _lease_matches(
                work,
                execution_lease,
            ):
                return "superseded"
            data = dict(work.data or {})
            data.pop("started_at", None)
            data.pop("started_by", None)
            data.pop("lease", None)
            data["deferred_at"] = _utcnow_iso()
            await res_q.update_resource(db, work, data=data, status="queued")
            await db.commit()
        return "deferred"

    follow_up_dispatch: tuple[int, datetime | None] | None = None
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        if work is None:
            return "superseded"
        if work.status in TERMINAL_WORK_OUTCOMES:
            return work.status
        if work.status not in LEASED_WORK_STATUSES or not _lease_matches(
            work,
            execution_lease,
        ):
            return "superseded"
        session = await sessions_q.get_session(
            db,
            str((work.data or {}).get("session_id")),
            organization_id=work.organization_id,
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
        if status != "rescheduling":
            data.pop("turn_journal", None)
        await res_q.update_resource(db, work, data=data, status=status)
        if status in {"completed", "error", "stopped"}:
            await _release_work_quota(
                db,
                work,
                actor_id=effective_worker_id,
            )
        await db.commit()
        if status == "rescheduling":
            follow_up_dispatch = (
                int(data.get("attempt") or 0),
                _parse_datetime(data.get("retry_at")),
            )
    if (
        follow_up_dispatch is not None
        and get_settings().vma_work_dispatch_mode == "hybrid"
    ):
        from app.runtime.dispatch import dispatch_work

        attempt, schedule_at = follow_up_dispatch
        await dispatch_work(work_id, attempt=attempt, schedule_at=schedule_at)
    return status


def should_execute_inline(environment_config: dict[str, Any] | None) -> bool:
    from app.config import get_settings

    settings = get_settings()
    return (
        settings.app_env.lower() in {"local", "test"}
        and (environment_config or {}).get("type") != "self_hosted"
    )


async def lease_next_work(
    db: AsyncSession,
    *,
    environment_id: str,
    worker_id: str,
    lease_seconds: int = 60,
    organization_id: str | None = None,
) -> ManagedResource | None:
    return await _lease_next_work(
        db,
        environment_id=environment_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        organization_id=resolve_organization_id(organization_id),
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
        organization_id=None,
    )


async def _lease_next_work(
    db: AsyncSession,
    *,
    environment_id: str | None,
    worker_id: str,
    lease_seconds: int,
    organization_id: str | None,
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
    if organization_id is not None:
        stmt = stmt.where(ManagedResource.organization_id == organization_id)
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
    generation = int(data.get("run_generation") or 0) + 1
    data["run_generation"] = generation
    data["lease"] = {
        "worker_id": worker_id,
        "lease_id": uuid4().hex,
        "generation": generation,
        "leased_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
    }
    await res_q.update_resource(db, work, data=data, status="leased")
    await db.flush()
    return work


async def admit_work_execution(
    db: AsyncSession,
    work: ManagedResource,
    execution_lease: WorkExecutionLease,
) -> WorkExecutionLease:
    """Fence and count one graph execution inside the caller's transaction."""

    if work.status not in LEASED_WORK_STATUSES:
        raise WorkLeaseError("Work item is no longer available for execution")
    if not _lease_matches(work, execution_lease):
        raise WorkLeaseError("Work execution lease was superseded before admission")

    data = dict(work.data or {})
    attempt = int(data.get("attempt") or 0) + 1
    max_attempts = int(get_settings().vma_work_max_attempts)
    if max_attempts > 0 and attempt > max_attempts:
        data["error"] = {
            "type": "max_attempts_exceeded",
            "message": f"Work item exceeded {max_attempts} execution attempts",
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        data["finished_at"] = _utcnow_iso()
        await res_q.update_resource(db, work, data=data, status="error")
        await _release_work_quota(db, work, actor_id=execution_lease.worker_id)
        raise WorkAttemptsExhausted(
            session_id=str(data.get("session_id") or ""),
            organization_id=work.organization_id,
            work_id=work.id,
            attempt=attempt,
            max_attempts=max_attempts,
        )

    data["attempt"] = attempt
    data["started_at"] = _utcnow_iso()
    data["started_by"] = execution_lease.worker_id
    await res_q.update_resource(db, work, data=data, status="running")
    return WorkExecutionLease(
        work_id=execution_lease.work_id,
        worker_id=execution_lease.worker_id,
        lease_id=execution_lease.lease_id,
        generation=execution_lease.generation,
        attempt=attempt,
    )


async def ack_work(
    db: AsyncSession,
    work: ManagedResource,
    *,
    worker_id: str | None = None,
    lease_id: str | None = None,
) -> ManagedResource:
    _require_lease_owner(
        work,
        worker_id=worker_id,
        lease_id=lease_id,
        action="ack",
        require_lease_id=False,
    )
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
    lease_id: str | None = None,
    lease_seconds: int = 60,
    payload: dict[str, Any] | None = None,
    preserve_status: bool = False,
) -> ManagedResource:
    _require_lease_owner(work, worker_id=worker_id, lease_id=lease_id, action="heartbeat")
    data = dict(work.data or {})
    now = datetime.now(timezone.utc)
    data["last_heartbeat_at"] = now.isoformat()
    data["last_heartbeat"] = payload or {}
    lease = dict(data.get("lease") or {})
    if worker_id:
        lease["worker_id"] = worker_id
    lease["expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
    data["lease"] = lease
    await res_q.update_resource(
        db,
        work,
        data=data,
        status=work.status if preserve_status else "running",
    )
    return work


async def _heartbeat_execution_lease(
    execution_lease: WorkExecutionLease,
    *,
    stop: asyncio.Event,
    lease_seconds: int,
) -> None:
    interval = max(1.0, lease_seconds / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, execution_lease.work_id)
            if work is None or not _lease_matches(work, execution_lease):
                return
            await heartbeat_work(
                db,
                work,
                worker_id=execution_lease.worker_id,
                lease_id=execution_lease.lease_id,
                lease_seconds=lease_seconds,
                payload={"type": "execution"},
                preserve_status=True,
            )
            await db.commit()


async def stop_work(db: AsyncSession, work: ManagedResource, *, payload: dict[str, Any]) -> ManagedResource:
    data = dict(work.data or {})
    data["stop"] = payload
    data["stopped_at"] = _utcnow_iso()
    await res_q.update_resource(db, work, data=data, status="stopped")
    await _release_work_quota(db, work)
    return work


async def _release_work_quota(
    db: AsyncSession,
    work: ManagedResource,
    *,
    actor_id: str | None = None,
) -> None:
    from app.config import get_settings

    if not get_settings().vma_governance_enabled:
        return
    await governance_service().release_active_work_in_session(
        db,
        organization_id=work.organization_id,
        reference_id=work.id,
        actor_id=actor_id,
    )


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


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def _require_lease_owner(
    work: ManagedResource,
    *,
    worker_id: str | None,
    lease_id: str | None = None,
    action: str,
    require_lease_id: bool = True,
) -> None:
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
    current_lease_id = lease.get("lease_id")
    if require_lease_id and current_lease_id and not lease_id:
        raise WorkLeaseError(f"lease_id is required to {action} this work lease")
    if lease_id is not None and str(current_lease_id or "") != str(lease_id):
        raise WorkLeaseError(f"Worker {worker_id} does not own the current work lease generation")


def _execution_lease(work: ManagedResource) -> WorkExecutionLease:
    lease = dict((work.data or {}).get("lease") or {})
    worker_id = str(lease.get("worker_id") or "")
    lease_id = str(lease.get("lease_id") or "")
    generation = int(lease.get("generation") or (work.data or {}).get("run_generation") or 0)
    if not worker_id or not lease_id or generation < 1:
        raise WorkLeaseError("Work item does not have a complete execution lease")
    return WorkExecutionLease(
        work_id=work.id,
        worker_id=worker_id,
        lease_id=lease_id,
        generation=generation,
        attempt=int((work.data or {}).get("attempt") or 0),
    )


def _lease_matches(work: ManagedResource, execution_lease: WorkExecutionLease) -> bool:
    lease = dict((work.data or {}).get("lease") or {})
    return (
        str(lease.get("worker_id") or "") == execution_lease.worker_id
        and str(lease.get("lease_id") or "") == execution_lease.lease_id
        and int(lease.get("generation") or 0) == execution_lease.generation
    )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
