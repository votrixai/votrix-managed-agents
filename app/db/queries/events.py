from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import ManagedSession, SessionEvent
from app.ids import new_id
from app.organization import resolve_organization_id
from app.session_errors import normalize_session_error_payload


async def append_event(
    db: AsyncSession,
    session: ManagedSession,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
    event_id: str | None = None,
) -> SessionEvent:
    # Allocate the per-session sequence in the database. Incrementing the ORM
    # attribute directly races when API and worker processes append concurrently.
    result = await db.execute(
        update(ManagedSession)
        .where(
            ManagedSession.id == session.id,
            ManagedSession.organization_id == session.organization_id,
        )
        .values(last_event_seq=ManagedSession.last_event_seq + 1)
        .returning(ManagedSession.last_event_seq)
        .execution_options(synchronize_session=False)
    )
    seq = result.scalar_one()
    set_committed_value(session, "last_event_seq", seq)
    event = SessionEvent(
        id=event_id or new_id("evt"),
        organization_id=session.organization_id,
        session_id=session.id,
        seq=seq,
        type=event_type,
        source=source or event_source(event_type),
        payload=_normalize_payload(event_type, payload),
    )
    db.add(event)
    await db.flush()
    return event


async def list_events(
    db: AsyncSession,
    *,
    session_id: str,
    after_seq: int = 0,
    limit: int = 100,
    organization_id: str | None = None,
) -> list[SessionEvent]:
    result = await db.execute(
        select(SessionEvent)
        .where(
            SessionEvent.session_id == session_id,
            SessionEvent.organization_id == resolve_organization_id(organization_id),
            SessionEvent.seq > after_seq,
        )
        .order_by(SessionEvent.seq.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_latest_event_seq(
    db: AsyncSession, *, session_id: str, organization_id: str | None = None
) -> int:
    result = await db.execute(
        select(SessionEvent.seq)
        .where(
            SessionEvent.session_id == session_id,
            SessionEvent.organization_id == resolve_organization_id(organization_id),
        )
        .order_by(SessionEvent.seq.desc())
        .limit(1)
    )
    return result.scalar_one_or_none() or 0


def event_source(event_type: str) -> str:
    return event_type.split(".", 1)[0] if "." in event_type else "system"


def _normalize_payload(
    event_type: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    normalized = dict(payload or {})
    normalized.setdefault("type", event_type)
    if event_type == "session.error":
        normalized = normalize_session_error_payload(normalized)
    normalized["processed_at"] = _default_processed_at(event_type)
    return normalized


def _default_processed_at(event_type: str) -> str | None:
    source = event_source(event_type)
    if source in {"user", "system"}:
        return None
    return datetime.now(timezone.utc).isoformat()
