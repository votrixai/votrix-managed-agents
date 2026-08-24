from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.db.models import (
    MemoryStore,
    Sandbox,
    Session,
    SessionEvent,
    SessionFile,
    SessionMemoryStore,
)
from app.db.models.sandboxes import SANDBOX_PROVISIONING, SANDBOX_TERMINATED
from app.db.models.sessions import IDLE, RUNNING
from app.db.queries import DEFAULT_PAGE_SIZE, Page, fetch_page
from app.models.sessions import STOP_REQUIRES_ACTION
from app.utils.id_generator import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- sessions ---------------------------------------------------------------


async def create_session(
    db: AsyncSession,
    *,
    organization_id: str,
    agent_id: str,
    agent_version: int,
    environment_id: str,
    model: dict[str, Any] | None = None,
    account_id: str | None = None,
    title: str | None = None,
) -> Session:
    session = Session(
        id=new_id("sess"),
        organization_id=organization_id,
        agent_id=agent_id,
        agent_version=agent_version,
        environment_id=environment_id,
        model=model,
        account_id=account_id,
        title=title,
        status=IDLE,
    )
    db.add(session)
    await db.flush()
    return session


async def get_session_by_id(db: AsyncSession, *, session_id: str) -> Session | None:
    """Look up a session without a tenant filter — for the worker only.

    A worker is woken by a queue, not by a tenant, so it has no organization of
    its own; it reads `session.organization_id` off the row it gets back. Never
    call this from a request that carries a caller's organization.
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def get_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
) -> Session | None:
    result = await db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.organization_id == organization_id,
            Session.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    *,
    organization_id: str,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    stmt = select(Session).where(
        Session.organization_id == organization_id,
        Session.deleted_at.is_(None),
    )
    if status is not None:
        stmt = stmt.where(Session.status == status)
    if not include_archived:
        stmt = stmt.where(Session.archived_at.is_(None))
    return await fetch_page(
        db, stmt, sort=Session.created_at, id_column=Session.id,
        limit=limit, before_id=before_id, after_id=after_id,
    )


async def update_session_title(db: AsyncSession, session: Session, *, title: str | None) -> None:
    session.title = title
    await db.flush()


async def archive_session(db: AsyncSession, session: Session) -> None:
    session.archived_at = _now()
    await db.flush()


async def delete_session(db: AsyncSession, session: Session) -> None:
    session.deleted_at = _now()
    await db.flush()


# --- events -----------------------------------------------------------------


async def append_event(
    db: AsyncSession,
    session: Session,
    *,
    type: str,
    source: str,
    payload: dict[str, Any] | None = None,
) -> SessionEvent:
    """Append one event, letting the database hand out its sequence number.

    The number has to be allocated by the database rather than by adding one to
    the copy of the row this request happens to be holding. Two requests do
    write to one session at the same time, by design: `user.interrupt` exists
    precisely to reach a session while its turn is still producing output, so
    the agent's next `agent.message` and the interrupt's own events are racing
    for the same counter.

    Adding one in memory loses that race. Both sides read the same committed
    value, both write the same `seq`, and the unique constraint on
    `(session_id, seq)` rejects the second — which rolls back the whole
    interrupt, so the user's stop is not delayed but lost.

    The worker's own generation check cannot help here. It fires on what
    `cancel_session` writes, and the collision happens *inside* that function,
    before it commits and therefore before anything it did is visible.

    `UPDATE ... RETURNING` reads and writes in one statement, so concurrent
    callers get 3 and 4 rather than 3 and 3. Which of them gets which does not
    matter: the ordering these numbers carry is "not the same as each other",
    not "in the order I would have guessed".
    """
    # The wake-up rides along in the statement that was going out anyway. It
    # reads as though it fires too early — the event row is not inserted until
    # below — and it does not: Postgres holds a notification until the
    # transaction commits, so a reader cannot be woken before the row it is
    # being woken for is visible. What the fold saves is a round trip on every
    # event a turn emits, and against the hosted pooler a round trip is ~150ms.
    #
    # Only the session id travels. What the event says is read back from the
    # table, so a notification lost, doubled or late costs a reader nothing but
    # the wait it would have had anyway.
    returning: list[Any] = [Session.last_event_seq]
    if _wakes_readers(db):
        returning.append(func.pg_notify(event_channel(), session.id))

    seq = (
        await db.execute(
            update(Session)
            .where(Session.id == session.id)
            .values(last_event_seq=Session.last_event_seq + 1)
            .returning(*returning)
        )
    ).scalar_one()

    # The row moved behind the ORM's back, so tell the in-memory copy what it
    # now holds — as a *committed* value, not an assignment. A plain
    # `session.last_event_seq = seq` would mark the attribute dirty, and the
    # next flush would write this request's number back over a number some
    # other request had since allocated. That is the same bug again, one layer
    # up.
    set_committed_value(session, "last_event_seq", seq)

    event = SessionEvent(
        id=new_id("evt"),
        organization_id=session.organization_id,
        session_id=session.id,
        seq=seq,
        type=type,
        source=source,
        payload=payload or {},
    )
    db.add(event)
    await db.flush()
    return event


def event_channel() -> str:
    """The `NOTIFY` channel streams are woken on.

    One channel for the whole deployment rather than one per session: a listener
    holds a single shared connection, and issuing `LISTEN`/`UNLISTEN` on it as
    every stream opens and closes is a race that buys nothing — a process with
    no reader for a session drops the notification on a dictionary lookup.

    The schema is in the name because staging and production can sit in one
    Postgres, and notifications are not scoped by `search_path`.
    """
    return f"vma_session_events_{get_settings().database_schema or 'public'}"


def delta_channel() -> str:
    """The channel streamed previews travel on, contents and all.

    Separate from `event_channel` because the two carry opposite things. That
    one is a doorbell: its payload is a session id, and the reader answers it by
    running the query it was going to run anyway, which is why a notification
    lost or doubled there costs nothing. This one *is* the payload — there is no
    row to go back and read, so a lost notification loses the text.

    That asymmetry is the reason they are not merged. A single channel would
    have to be sniffed at the far end to tell an id from a preview, and every
    process listening for one would wake for the other: the worker takes no
    streams and would still be woken four times a second by every turn in the
    tenant.
    """
    return f"vma_session_deltas_{get_settings().database_schema or 'public'}"


# Postgres refuses a `NOTIFY` payload over 8000 bytes outright, and a refusal
# here would abort the transaction the turn is running in — a dropped preview
# taking the whole turn with it. This is what is left for the text once the
# envelope and a session id are paid for, with enough slack that nobody has to
# do this arithmetic again.
DELTA_PAYLOAD_BUDGET_BYTES = 6000


async def publish_delta(
    db: AsyncSession,
    *,
    session_id: str,
    type: str,
    text: str,
) -> None:
    """Broadcast a preview. No row, no seq, no commit of its own.

    The entire cost is one round trip to say it. Nothing is inserted, and in
    particular the session's `last_event_seq` is left alone — the counter that
    `append_event` bumps sits on a single row, so writing previews through it
    would rewrite that row four times a second for the length of every turn,
    leaving a dead tuple each time and holding a lock every other writer to the
    session queues behind.

    Delivery is best effort by construction: Postgres drops a notification
    nobody is listening for, so a turn no client is watching publishes into
    silence and pays only for having spoken. That is the desired behaviour and
    not a gap to close — what a preview is *for* expires the moment the
    `agent.message` lands.
    """
    if not _wakes_readers(db):
        return
    channel = delta_channel()
    for piece in _within_payload_budget(text):
        payload = json.dumps(
            {"s": session_id, "t": type, "x": piece},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await db.execute(
            sa_text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": payload},
        )


def _within_payload_budget(text: str) -> Iterator[str]:
    """Split on a byte budget without splitting a character in half.

    Almost always yields the whole string on the first pass; the engine's own
    flush ceiling keeps a normal delta an order of magnitude under this. The
    loop exists for the pathological case — a model that emits several thousand
    characters between two flushes — where the alternative is a `NOTIFY` that
    raises inside the turn.

    Cutting on the byte budget can land in the middle of a multi-byte character,
    so the cut walks back off any continuation byte (`10xxxxxx`) first. CJK is
    three bytes per character, which is exactly where a naive character-count
    ceiling would have been wrong.
    """
    raw = text.encode()
    while raw:
        if len(raw) <= DELTA_PAYLOAD_BUDGET_BYTES:
            yield raw.decode()
            return
        cut = DELTA_PAYLOAD_BUDGET_BYTES
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        yield raw[:cut].decode()
        raw = raw[cut:]


def _wakes_readers(db: AsyncSession) -> bool:
    """Whether this database can carry a wake-up at all.

    SQLite has no `pg_notify`, and a fresh checkout runs on SQLite. There is
    nothing to fall back to and nothing to warn about: with no listener there is
    no reader waiting to be told, and the stream polls as it always did.

    For previews this is not a degradation to the poll — it is their absence.
    They live only on the channel, so on SQLite a turn simply has no previews
    and a client sees whole messages, which is what it saw before any of this.
    """
    return db.get_bind().dialect.name == "postgresql"


async def list_events(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    after_seq: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    """The conversation in order, oldest first.

    Two cursors, because events already had one that is worth keeping:
    `after_seq` is what a client tailing a session uses, since it is tracking
    `last_event_seq` anyway and does not have to have seen the last event to
    ask for the next. `after_id`/`before_id` are here so this page reads like
    every other one.

    Nothing is filtered out. This returns the table, and the table is only the
    log — streamed previews never reach it, so there is no longer a category of
    row that has to be hidden from the transcript that stores it.
    """
    stmt = select(SessionEvent).where(
        SessionEvent.session_id == session_id,
        SessionEvent.organization_id == organization_id,
    )
    if after_seq is not None:
        stmt = stmt.where(SessionEvent.seq > after_seq)
    return await fetch_page(
        db,
        stmt,
        sort=SessionEvent.seq,
        id_column=SessionEvent.id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        # Oldest first: this is a transcript, and a transcript reads forwards.
        descending=False,
    )


async def get_event(
    db: AsyncSession,
    *,
    event_id: str,
    organization_id: str,
) -> SessionEvent | None:
    result = await db.execute(
        select(SessionEvent).where(
            SessionEvent.id == event_id,
            SessionEvent.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


# --- the session gate -------------------------------------------------------


async def claim_session(
    db: AsyncSession,
    *,
    session_id: str,
    lease_seconds: int = 300,
    answering: bool = False,
) -> bool:
    """Try to take the session. True if we got it, False if it was busy.

    There is no queue behind this: a message that cannot claim the session is
    refused outright. That makes the atomicity load-bearing — reading the
    status and then writing it would let two callers both see `idle`, both
    accept a message, and leave the second one with nothing to run it.

    A session whose lease has lapsed is up for grabs again, which is what stops
    a worker that died mid-reply from locking the session out forever.

    `answering` says this batch is the answer a paused graph is waiting for. A
    session parked on `requires_action` is idle but not free: its turn is over,
    and yet a tool call is still open and the client owes us the result. Anyone
    else claiming it restarts the agent from the top, which cancels that call —
    so the parking is a closed gate to everything except the answer, and the
    rest are told the session is busy and come back. `user.interrupt` is the way
    out of a parking nobody ever answers, and it does not come through here.

    The parking is cleared on the way in, because `stop_reason` describes the
    last turn that finished and this one has just started.

    The row is changed behind the ORM's back, so load the session *after*
    claiming it, never before.
    """
    now = _now()
    free = [
        Session.id == session_id,
        Session.deleted_at.is_(None),
        or_(
            Session.status == IDLE,
            and_(
                Session.status == RUNNING,
                or_(
                    Session.lease_expires_at.is_(None),
                    Session.lease_expires_at <= now,
                ),
            ),
        ),
    ]
    if not answering:
        # Asked of the extracted type rather than of the column: a cleared
        # `stop_reason` reaches the database as SQL NULL from one direction and
        # as JSON `null` from the other, and both of those extract to NULL while
        # only one of them `IS NULL`. Reading through the arrow costs nothing
        # and stops a stored `null` from shutting the gate on everybody.
        parked = Session.stop_reason["type"].as_string()
        free.append(or_(parked.is_(None), parked != STOP_REQUIRES_ACTION))
    result = await db.execute(
        update(Session)
        .where(*free)
        .values(
            status=RUNNING,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            stop_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return result.rowcount == 1


async def extend_session_lease(
    db: AsyncSession,
    session: Session,
    *,
    lease_seconds: int = 300,
) -> None:
    """Push the lease out — the worker's heartbeat."""
    session.lease_expires_at = _now() + timedelta(seconds=lease_seconds)
    await db.flush()


async def release_session(
    db: AsyncSession,
    session: Session,
    *,
    status: str = IDLE,
    stop_reason: dict[str, Any] | None = None,
) -> None:
    """Hand the session back so it will accept the next message.

    Moving `lock_version` on is what tells a worker it no longer owns the
    session — without it, a worker whose turn was interrupted would see the
    *next* turn's `running` status and carry on writing into someone else's
    conversation.
    """
    session.status = status
    session.stop_reason = stop_reason
    session.lease_expires_at = None
    session.lock_version += 1
    await db.flush()


async def list_stuck_sessions(db: AsyncSession, *, limit: int = 100) -> list[Session]:
    """Sessions left `running` by a worker that never came back, claimed.

    The lease already lets the next message take one of these over, so this is
    for the janitor: tidy them back to `idle` and tear down their sandbox
    rather than waiting for a user to bump into it.

    `SKIP LOCKED` is what makes more than one janitor safe. The sweep reads
    these and then writes to each — an error event and a release — so two
    sweepers reading the same row would both append, and the conversation
    would end with two identical `worker_lost` frames. Locking on the way out
    of the select hands each sweeper a set nobody else is holding, and the
    rows stay locked until the sweep commits.

    Nothing live is ever locked here: a session only qualifies once its lease
    has lapsed, which is to say once whoever was renewing it is gone.
    """
    now = _now()
    stmt = (
        select(Session)
        .where(
            Session.deleted_at.is_(None),
            Session.status == RUNNING,
            or_(
                Session.lease_expires_at.is_(None),
                Session.lease_expires_at <= now,
            ),
        )
        .order_by(Session.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# --- sandboxes --------------------------------------------------------------


async def create_sandbox(
    db: AsyncSession,
    session: Session,
    *,
    provider: str,
    ttl_seconds: int,
    external_sandbox_id: str | None = None,
    expires_at: datetime | None = None,
    network_access: bool = True,
) -> Sandbox:
    sandbox = Sandbox(
        id=new_id("sbx"),
        organization_id=session.organization_id,
        session_id=session.id,
        environment_id=session.environment_id,
        provider=provider,
        external_sandbox_id=external_sandbox_id,
        state=SANDBOX_PROVISIONING,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        network_access=network_access,
    )
    db.add(sandbox)
    await db.flush()
    return sandbox


async def get_sandbox_ids(
    db: AsyncSession,
    *,
    session_ids: Sequence[str],
    organization_id: str,
) -> dict[str, str]:
    """Which container each of these conversations owns.

    A batch of the lookup `get_sandbox` does one at a time, because the session
    listing needs the answer for a whole page and asking per row would put a
    query behind every line of it. The partial unique index on `session_id`
    is what makes one row per session a fact rather than an assumption.
    """

    if not session_ids:
        return {}
    result = await db.execute(
        select(Sandbox.session_id, Sandbox.id).where(
            Sandbox.session_id.in_(list(session_ids)),
            Sandbox.organization_id == organization_id,
        )
    )
    return {session_id: sandbox_id for session_id, sandbox_id in result.all()}


async def get_sandbox(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
) -> Sandbox | None:
    result = await db.execute(
        select(Sandbox).where(
            Sandbox.session_id == session_id,
            Sandbox.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def update_sandbox_state(
    db: AsyncSession,
    sandbox: Sandbox,
    *,
    state: str,
    external_sandbox_id: str | None = None,
    expires_at: datetime | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    sandbox.state = state
    if external_sandbox_id is not None:
        sandbox.external_sandbox_id = external_sandbox_id
    if expires_at is not None:
        sandbox.expires_at = expires_at
    sandbox.error = error
    sandbox.last_active_at = _now()
    sandbox.lock_version += 1
    await db.flush()


async def list_expired_sandboxes(db: AsyncSession, *, limit: int = 100) -> list[Sandbox]:
    """Sandboxes past their expiry, of either kind — the janitor's input."""
    result = await db.execute(
        select(Sandbox)
        .where(
            Sandbox.expires_at.is_not(None),
            Sandbox.expires_at <= _now(),
            Sandbox.state != SANDBOX_TERMINATED,
        )
        .order_by(Sandbox.expires_at)
        .limit(limit)
    )
    return list(result.scalars().all())


# --- files a session was given, or produced ---------------------------------


async def attach_file(
    db: AsyncSession,
    session: Session,
    *,
    file_id: str,
    path: str,
) -> SessionFile:
    attached = SessionFile(
        id=new_id("sfile"),
        organization_id=session.organization_id,
        session_id=session.id,
        file_id=file_id,
        path=path,
    )
    db.add(attached)
    await db.flush()
    return attached


async def list_session_files(
    db: AsyncSession,
    *,
    session_id: str,
) -> list[SessionFile]:
    stmt = select(SessionFile).where(SessionFile.session_id == session_id)
    result = await db.execute(stmt.order_by(SessionFile.path))
    return list(result.scalars().all())


# --- Memory Stores mounted into a Session -----------------------------------


async def attach_memory_store(
    db: AsyncSession,
    session: Session,
    store: MemoryStore,
    *,
    access: str,
    instructions: str | None,
    mount_path: str,
) -> SessionMemoryStore:
    attached = SessionMemoryStore(
        id=new_id("sesrsc"),
        organization_id=session.organization_id,
        session_id=session.id,
        memory_store_id=store.id,
        access=access,
        instructions=instructions,
        mount_path=mount_path,
        name=store.name,
        description=store.description,
    )
    db.add(attached)
    await db.flush()
    return attached


async def list_session_memory_stores(
    db: AsyncSession,
    *,
    session_id: str,
) -> list[SessionMemoryStore]:
    stmt = select(SessionMemoryStore).where(
        SessionMemoryStore.session_id == session_id
    )
    result = await db.execute(stmt.order_by(SessionMemoryStore.mount_path))
    return list(result.scalars().all())
