"""Bounded 0 ↔ 1 reconciliation; Cloud Scheduler supplies recovery at zero.

The DB gate closes *before* a scale-down request leaves this process. Every
command also changes a Cloud Run annotation under an etag precondition. A lost
or delayed PATCH(0) therefore cannot land after a confirmed, newer PATCH(1).
Only the matching, fully reconciled command reopens execution.
"""

import asyncio
from datetime import UTC, datetime
from functools import lru_cache
from uuid import uuid4

import google.auth
import structlog
from google.auth.transport.requests import AuthorizedSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import SessionTurn, WorkerPoolControl

logger = structlog.get_logger(__name__)
COMMAND_ANNOTATION = "vma.votrix.ai/scaling-command"
CONTROL_ID = 1


@lru_cache
def _client() -> AuthorizedSession:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials, refresh_timeout=3, max_refresh_attempts=1)


def _request_sync(method: str, body: dict | None) -> dict:
    response = _client().request(
        method,
        "https://run.googleapis.com/v2/" + get_settings().vma_worker_pool,
        json=body,
        params={"updateMask": "scaling.manualInstanceCount,annotations"} if body else None,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()


async def _request(method: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(_request_sync, method, body)


async def allow_execution(db: AsyncSession) -> bool:
    if not get_settings().worker_pool_on_demand:
        return True
    control = await db.get(WorkerPoolControl, CONTROL_ID, with_for_update=True)
    if control is None or not control.ready:
        return False
    # A short turn entirely between Scheduler ticks must reset the cooldown too.
    control.idle_since = None
    return True


async def _lock(db: AsyncSession) -> WorkerPoolControl | None:
    # Competing API/Scheduler/worker invocations leave the current one in charge.
    # No session advisory locks: Supabase transaction pooling is supported.
    control = await db.scalar(select(WorkerPoolControl).where(
        WorkerPoolControl.id == CONTROL_ID
    ).with_for_update(skip_locked=True))
    if control is None and await db.get(WorkerPoolControl, CONTROL_ID) is None:
        raise RuntimeError("Worker pool control missing; apply the database migrations")
    return control


async def reconcile(*, wake_only: bool = False) -> str:
    if not get_settings().worker_pool_on_demand:
        return "disabled"
    async with asyncio.timeout(8):
        return await _reconcile(wake_only=wake_only)


async def _reconcile(*, wake_only: bool) -> str:
    async with session_scope() as db:
        control = await _lock(db)
        if control is None:
            return "busy"
        # Includes running, queued, expired-lease and delayed-retry turns. Queue
        # backlog alone is unsafe: an acknowledged duplicate can empty it.
        work = await db.scalar(select(select(SessionTurn.session_id).where(
            SessionTurn.done.is_(False)
        ).exists()))
        now = datetime.now(UTC)
        target = control.target
        if work:
            control.idle_since = None
            target = 1
        elif wake_only:
            return "ready" if control.ready else "idle"
        else:
            if control.idle_since is None:
                control.idle_since = now
            idle_since = control.idle_since.replace(tzinfo=UTC)
            if (now - idle_since).total_seconds() >= get_settings().vma_worker_pool_idle_seconds:
                target = 0
        if target != control.target or not control.command:
            control.target = target
            control.command = uuid4().hex
            control.ready = False
        fast_path = wake_only and control.ready
        # Persist intent and close admission BEFORE any remote mutation. This
        # transaction never locks turn rows, avoiding the worker's lock order.
        await db.commit()
        if fast_path:
            return "ready"

    async with session_scope() as db:
        control = await _lock(db)
        if control is None:
            return "busy"
        pool = await _request("GET")
        count = pool.get("scaling", {}).get("manualInstanceCount", 0)
        if int(count) > 1:
            raise RuntimeError("Disable on-demand control before manually scaling above one")
        matches = (
            int(count) == control.target
            and pool.get("annotations", {}).get(COMMAND_ANNOTATION) == control.command
        )
        settled = (
            not pool.get("reconciling", False)
            and pool.get("terminalCondition", {}).get("state") == "CONDITION_SUCCEEDED"
            and pool.get("observedGeneration") == pool.get("generation")
        )
        if matches and settled:
            control.ready = control.target == 1
            await db.commit()
            return "ready" if control.ready else "idle"
        if matches and pool.get("reconciling", False):
            return "pending"
        if control.ready or matches:
            # External drift or a failed operation: fence the retry with a new
            # command, committed before the next invocation sends its PATCH.
            control.ready = False
            control.command = uuid4().hex
            await db.commit()
            return "pending"
        await _request("PATCH", {
            "name": get_settings().vma_worker_pool,
            "etag": pool["etag"],
            "scaling": {"manualInstanceCount": control.target},
            "annotations": {**pool.get("annotations", {}), COMMAND_ANNOTATION: control.command},
        })
        logger.info("worker_pool_scale_requested", target=control.target)
        # PATCH returns a long-running operation, not a ready worker. Leave the
        # durable gate closed even if the response or this DB transaction dies.
        return "pending"


async def wake() -> None:
    try:
        await reconcile(wake_only=True)
    except Exception:
        # Admission has already committed. Scheduler will retry independently
        # even when no worker or successful Pub/Sub publish exists.
        logger.exception("worker_pool_wake_failed")
