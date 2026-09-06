"""Durable ownership around the existing session runner.

Session rows are locked before turn rows everywhere. Locks and connections are
held for state/checkpoint writes, never for the lifetime of an agent run.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Session, SessionEvent, SessionTurn
from app.db.queries import sessions as sessions_q
from app.services.worker_pool import allow_execution

logger = structlog.get_logger(__name__)
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
RECOVERY_SECONDS = 60


def now() -> datetime:
    return datetime.now(UTC)


def expired(value: datetime | None) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now()


class LeaseLost(Exception):
    """This delivery may no longer change the session or its checkpoint."""


@dataclass
class Execution:
    session_id: str
    generation: int
    owner: str
    attempts: int
    events: list[dict[str, Any]]
    history_ids: list[str] | None

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.generation}"

    async def lock(self, db: AsyncSession) -> tuple[Session, SessionTurn]:
        session = await db.get(
            Session, self.session_id, with_for_update=True, populate_existing=True
        )
        turn = await db.get(
            SessionTurn,
            (self.session_id, self.generation),
            with_for_update=True,
            populate_existing=True,
        )
        if (
            session is None
            or turn is None
            or turn.done
            or turn.owner != self.owner
            or expired(turn.lease_until)
            or session.deleted_at is not None
            or session.status != "running"
            or session.lock_version != self.generation
        ):
            raise LeaseLost(self.key)
        return session, turn

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        async with session_scope() as db:
            await self.lock(db)
            yield
            await db.commit()

    async def save_history(self, ids: list[str]) -> None:
        async with session_scope() as db:
            _, turn = await self.lock(db)
            if turn.history_ids is None:
                turn.history_ids = ids
            self.history_ids = turn.history_ids
            await db.commit()

    async def emit(
        self, key: str, event_type: str, payload: dict[str, Any]
    ) -> SessionEvent | None:
        # Ending the turn and publishing idle must be one transaction. The
        # runner calls finish only after output collection has completed.
        if event_type == "session.status_idle":
            return
        event_id = (
            "evt_" + hashlib.sha256(f"{self.key}:{key}".encode()).hexdigest()[:48]
        )
        async with session_scope() as db:
            session, _ = await self.lock(db)
            existing = await db.get(SessionEvent, event_id)
            if existing is None:
                existing = await sessions_q.append_event(
                    db,
                    session,
                    type=event_type,
                    source="agent",
                    payload=payload,
                    event_id=event_id,
                )
            await db.commit()
            return existing

    async def publish(self, event_type: str, text: str) -> None:
        async with session_scope() as db:
            session, _ = await self.lock(db)
            await sessions_q.publish_delta(
                db, session_id=session.id, type=event_type, text=text
            )
            await db.commit()

    async def finish(self, stop_reason: dict[str, Any]) -> None:
        async with session_scope() as db:
            session, _ = await self.lock(db)
            await sessions_q.append_event(
                db,
                session,
                type="session.status_idle",
                source="agent",
                payload={"stop_reason": stop_reason},
            )
            await sessions_q.release_session(db, session, stop_reason=stop_reason)
            await db.commit()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            async with session_scope() as db:
                session, turn = await self.lock(db)
                turn.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
                session.lease_expires_at = turn.lease_until
                await db.commit()


async def acquire(session_id: str, generation: int) -> Execution | None:
    async with session_scope() as db:
        session = await db.get(Session, session_id, with_for_update=True)
        turn = await db.get(SessionTurn, (session_id, generation), with_for_update=True)
        if turn is None or turn.done:
            return None
        if (
            session is None
            or session.status != "running"
            or session.deleted_at is not None
            or session.lock_version != generation
        ):
            turn.done = True
            turn.events = []
            turn.history_ids = None
            await db.commit()
            return None
        if not expired(turn.lease_until):
            return None
        if not expired(turn.retry_after):
            return None
        if not await allow_execution(db):
            return None
        turn.owner = uuid4().hex
        turn.retry_after = None
        turn.attempts += 1
        turn.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        session.lease_expires_at = turn.lease_until
        execution = Execution(
            session_id,
            generation,
            turn.owner,
            turn.attempts,
            turn.events,
            turn.history_ids,
        )
        await db.commit()
        return execution


def retryable(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (DBAPIError, PoolTimeout, httpx.TransportError, ConnectionError, TimeoutError),
    ):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    return status == 429 or (isinstance(status, int) and status >= 500)


async def _record_failure(execution: Execution, exc: BaseException) -> None:
    from app.services.sessions import _fail

    try:
        async with session_scope() as db:
            session, turn = await execution.lock(db)
            if retryable(exc) and turn.attempts < get_settings().vma_turn_max_attempts:
                turn.owner = None
                turn.lease_until = None
                turn.available_at = now() + timedelta(seconds=RECOVERY_SECONDS)
                turn.retry_after = turn.available_at
                await db.commit()
                logger.warning(
                    "turn_retry_pending", session_id=session.id, attempt=turn.attempts
                )
            else:
                await _fail(db, session, exc)
    except LeaseLost:
        pass


async def run_turn(session_id: str, generation: int) -> None:
    from app.services.sessions import process_session

    execution = await acquire(session_id, generation)
    if execution is None:
        return  # completed, cancelled, stale, or already owned

    async def run():
        if execution.attempts > get_settings().vma_turn_max_attempts:
            raise RuntimeError("worker recovery attempts exhausted")
        async with session_scope() as db:
            await process_session(
                db, session_id=session_id, events=execution.events, execution=execution
            )

    runner = asyncio.create_task(run())
    heartbeat = asyncio.create_task(execution.heartbeat())
    try:
        done, _ = await asyncio.wait(
            (runner, heartbeat), return_when=asyncio.FIRST_COMPLETED
        )
        if runner in done:
            await runner
        else:
            await heartbeat  # a failed heartbeat must stop execution
            raise LeaseLost(execution.key)
    except asyncio.CancelledError:
        # Leave the lease in place during shutdown. A replacement cannot
        # start until this process has had time to stop its in-flight work.
        raise
    except LeaseLost:
        pass
    except Exception as exc:
        logger.exception(
            "owned_turn_failed", session_id=session_id, attempt=execution.attempts
        )
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await _record_failure(execution, exc)
    finally:
        runner.cancel()
        heartbeat.cancel()
        await asyncio.gather(runner, heartbeat, return_exceptions=True)


async def recover_once() -> int:
    """Republish queued/expired work, including a lost initial publish.

    Reserve a short retry window before publishing. If this process also dies
    in that gap, the next pass still discovers the durable turn.
    """
    from app.services.pubsub import publish_turn

    async with session_scope() as db:
        turns = (
            await db.scalars(
                select(SessionTurn)
                .where(
                    SessionTurn.done.is_(False),
                    SessionTurn.available_at <= now(),
                    or_(
                        SessionTurn.lease_until.is_(None),
                        SessionTurn.lease_until <= now(),
                    ),
                )
                .order_by(SessionTurn.available_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        keys = [(turn.session_id, turn.generation) for turn in turns]
        for turn in turns:
            turn.available_at = now() + timedelta(seconds=RECOVERY_SECONDS)
        await db.commit()
    for session_id, generation in keys:
        try:
            await publish_turn(session_id=session_id, generation=generation)
        except Exception:
            logger.exception("turn_republish_failed", session_id=session_id)
    return len(keys)
