import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_scope
from app.db.models import Session as SessionRow
from app.db.models import SessionEvent, SessionFile
from app.db.queries import DEFAULT_PAGE_SIZE
from app.db.queries import sessions as sessions_q
from app.models import events as event_models
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.events import (
    DeltaFrame,
    EventResponse,
    ListEventsResponse,
    SendEventsRequest,
    SendEventsResponse,
)
from app.models.files import FileResponse, LiveFileRequest, LiveUploadRequest
from app.models.sessions import (
    SessionCreateRequest,
    SessionFileResourceResponse,
    SessionMemoryStoreResourceResponse,
    SessionResponse,
    SessionUpdateRequest,
    SessionUsageResponse,
)
from app.routers.deps import Db, OrganizationId
from app.routers.files import to_file
from app.services import sessions as service
from app.utils.sandbox import UPLOADS_DIR

class _Unset:
    """Tells "the caller did not say" apart from "there is no container"."""


_UNSET = _Unset()

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreateRequest, db: Db, organization_id: OrganizationId):
    session = await service.create_session(
        db,
        organization_id=organization_id,
        agent_id=body.agent_id,
        environment_id=body.environment_id,
        agent_version=body.agent_version,
        model=body.model,
        account_id=body.account_id,
        title=body.title,
        resources=[resource.model_dump() for resource in body.resources],
    )
    return await to_session(db, session)


@router.get("", response_model=ListResponse[SessionResponse])
async def list_sessions(
    db: Db,
    organization_id: OrganizationId,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    sessions = await service.list_sessions(
        db,
        organization_id=organization_id,
        status=status,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    # One query for the page rather than one per row: `to_session` resolves the
    # container itself when it is not told, and a listing is exactly where that
    # becomes a query per line.
    sandboxes = await sessions_q.get_sandbox_ids(
        db,
        session_ids=[session.id for session in sessions.items],
        organization_id=organization_id,
    )
    return ListResponse(
        data=[
            await to_session(db, session, sandbox_id=sandboxes.get(session.id))
            for session in sessions.items
        ],
        has_more=sessions.has_more,
        first_id=sessions.first_id,
        last_id=sessions.last_id,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def retrieve_session(session_id: str, db: Db, organization_id: OrganizationId):
    session = await service.get_session(db, session_id=session_id, organization_id=organization_id)
    return await to_session(db, session)


@router.get("/{session_id}/usage", response_model=SessionUsageResponse)
async def retrieve_session_usage(
    session_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    """Return OpenRouter's latest cumulative USD snapshot for this Session.

    VMA queries the provider on every request and does not persist per-response
    costs. `usage_usd` is the whole Session total as of `as_of`, not the cost of
    its last turn. Consumers that settle incrementally should store their last
    settled snapshot and debit only the positive difference.
    """
    usage = await service.get_session_usage(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    return SessionUsageResponse(
        session_id=session_id,
        account_id=usage.account_id,
        usage_usd=usage.usage_usd,
        as_of=usage.as_of,
    )


@router.post("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    body: SessionUpdateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    session = await service.update_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
        title=body.title,
    )
    return await to_session(db, session)


@router.delete("/{session_id}", response_model=DeletedResponse)
async def delete_session(session_id: str, db: Db, organization_id: OrganizationId):
    session = await service.delete_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    return DeletedResponse(id=session.id)


@router.post("/{session_id}/archive", response_model=SessionResponse)
async def archive_session(session_id: str, db: Db, organization_id: OrganizationId):
    session = await service.archive_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    return await to_session(db, session)


@router.post("/{session_id}/events", response_model=SendEventsResponse)
async def send_events(
    session_id: str,
    body: SendEventsRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Append client events. Refused with 409 while the agent is still replying.

    `user.interrupt` is the exception — it exists to reach a busy session, so
    it is accepted whatever the session is doing.
    """
    events = await service.send_events(
        db,
        session_id=session_id,
        organization_id=organization_id,
        # Flat dicts, the same shape the client sent. There is no `payload`
        # envelope to unwrap: `type` picks the shape and the rest is the event.
        events=[event.model_dump() for event in body.events],
    )
    return SendEventsResponse(data=[to_event(e) for e in events])


@router.get("/{session_id}/events", response_model=ListEventsResponse)
async def list_events(
    session_id: str,
    db: Db,
    organization_id: OrganizationId,
    after_seq: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    """The transcript, oldest first.

    `after_seq` is the cursor for tailing a session — a client tracking
    `last_event_seq` can ask for what it has not seen without having seen the
    last event. `after_id`/`before_id` page like everything else.
    """
    session, events = await service.list_events(
        db,
        session_id=session_id,
        organization_id=organization_id,
        after_seq=after_seq,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    page = page_of(events, to_event)
    return ListEventsResponse(
        data=page.data,
        has_more=page.has_more,
        first_id=page.first_id,
        last_id=page.last_id,
        last_event_seq=session.last_event_seq,
    )


@router.get("/{session_id}/events/stream")
async def stream_events(
    session_id: str,
    request: Request,
    organization_id: OrganizationId,
    after_seq: int | None = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Follow a session's events live, as server-sent events.

    Resuming works two ways and they mean the same thing: `after_seq`, and the
    `Last-Event-ID` header a browser's `EventSource` sends by itself after a
    dropped connection. Each event carries its `seq` as the SSE id, so a
    reconnect picks up exactly where the last one stopped.

    Asking without either replays the session from the beginning, so a page
    that was refreshed gets the whole transcript from this one call.

    The stream stays open across turns and closes when the session terminates,
    when the client goes away, or after half an hour — at which point the
    client reconnects with the id it last saw and misses nothing.

    No database session is taken: this connection can outlive a request by a
    long way, and holding a pooled connection open for it would spend the pool
    on readers rather than on work.
    """
    resume = after_seq if after_seq is not None else _seq_from(last_event_id)
    # Raised here rather than inside the stream, so an unknown session is a
    # failed request and not a connection that opens and says nothing.
    async with session_scope() as db:
        await service.get_session(db, session_id=session_id, organization_id=organization_id)

    return StreamingResponse(
        _sse(request, session_id=session_id, organization_id=organization_id, after_seq=resume),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers responses by default, which would hold every event
            # until the stream ended — exactly backwards.
            "X-Accel-Buffering": "no",
        },
    )


async def _sse(
    request: Request,
    *,
    session_id: str,
    organization_id: str,
    after_seq: int | None,
) -> AsyncIterator[str]:
    """Turn the service's events into the SSE wire format."""
    quiet_since = time.monotonic()
    async for event in service.stream_events(
        session_id=session_id,
        organization_id=organization_id,
        after_seq=after_seq,
    ):
        if await request.is_disconnected():
            return
        if event is None:
            # Nothing new. A comment keeps proxies from deciding the connection
            # is dead; clients ignore it.
            if time.monotonic() - quiet_since >= service.STREAM_HEARTBEAT_SECONDS:
                quiet_since = time.monotonic()
                yield ": keep-alive\n\n"
            continue
        quiet_since = time.monotonic()
        if isinstance(event, DeltaFrame):
            # No `id:`, deliberately. That field is what the browser stores and
            # sends back as `Last-Event-ID`, and it means "resume after this
            # point in the log" — a preview has no point in the log. Numbering
            # one would tell a reconnecting client to skip past events it has
            # never seen.
            yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
            continue
        yield (
            f"id: {event.seq}\n"
            f"event: {event.type}\n"
            f"data: {to_event(event).model_dump_json()}\n\n"
        )


def _seq_from(last_event_id: str | None) -> int | None:
    """Read the resume point out of a `Last-Event-ID` header.

    A client that sends something unreadable gets the transcript from the
    start, which is wasteful but never wrong — the alternative is silently
    skipping events it has not seen.
    """
    if last_event_id is None:
        return None
    try:
        return int(last_event_id)
    except ValueError:
        return None


@router.get("/{session_id}/events/{event_id}", response_model=EventResponse)
async def retrieve_event(
    session_id: str,
    event_id: str,
    db: Db,
    organization_id: OrganizationId,
):
    event = await service.get_event(db, event_id=event_id, organization_id=organization_id)
    return to_event(event)


@router.post("/{session_id}/live/files", response_model=FileResponse)
async def capture_live_file(
    session_id: str,
    body: LiveFileRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Take one file out of the sandbox now, without waiting for the turn.

    A session's outputs are collected when its turn ends. This is for the case
    where the agent has finished a deliverable partway through and the user
    wants it before the rest of the work is done — what comes back is the same
    kind of file record, downloaded the same way.

    409 once the sandbox is gone: the file existed in a container that no
    longer does, and anything already collected is on `/v1/files` instead.
    """
    file = await service.capture_file(
        db,
        session_id=session_id,
        organization_id=organization_id,
        path=body.path,
    )
    return to_file(file)


@router.post(
    "/{session_id}/live/uploads",
    response_model=SessionFileResourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def attach_live_upload(
    session_id: str,
    body: LiveUploadRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Put an already uploaded File into an existing Session sandbox.

    Upload the durable object through ``POST /v1/files`` first, then pass its
    id here. The Session must be idle and its E2B sandbox must still exist;
    this endpoint may wake a paused sandbox, but it never rebuilds one that has
    gone away. ``path`` is relative to ``uploads/`` and defaults to the File's
    filename. The mounted bytes are read-only to the agent.

    Retrying the same File at the same path is idempotent. A different File at
    an occupied path, a busy Session, or a Session without a usable sandbox is
    refused with 409.
    """
    attached = await service.attach_live_file(
        db,
        session_id=session_id,
        organization_id=organization_id,
        file_id=body.file_id,
        path=body.path,
    )
    return to_session_file_resource(attached)


def to_session_file_resource(row: SessionFile) -> SessionFileResourceResponse:
    return SessionFileResourceResponse(
        id=row.id,
        file_id=row.file_id,
        mount_path=f"{UPLOADS_DIR}/{row.path}",
        created_at=row.created_at,
        updated_at=row.created_at,
    )


async def to_session(
    db: AsyncSession,
    session: SessionRow,
    *,
    sandbox_id: str | None | _Unset = _UNSET,
) -> SessionResponse:
    """Written out by hand rather than via `from_attributes`.

    A column has to be named here to be published, which is what keeps
    `organization_id`, `lock_version` and the rest from leaking into responses
    the moment someone adds one.

    `sandbox_id` is looked up here unless the caller already has it. A listing
    resolves a whole page in one query and passes each in; anything answering
    about one Session lets this do it. Passing `None` explicitly states there
    is no container, and is not the same as leaving it out.
    """
    if isinstance(sandbox_id, _Unset):
        sandbox = await sessions_q.get_sandbox(
            db, session_id=session.id, organization_id=session.organization_id
        )
        sandbox_id = sandbox.id if sandbox is not None else None

    files = await service.list_session_files(db, session_id=session.id)
    memory_stores = await service.list_session_memory_stores(
        db, session_id=session.id
    )
    resources = [to_session_file_resource(row) for row in files]
    resources.extend(
        SessionMemoryStoreResourceResponse(
            id=row.id,
            memory_store_id=row.memory_store_id,
            access=row.access,
            instructions=row.instructions,
            mount_path=row.mount_path,
            name=row.name,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in memory_stores
    )
    resources.sort(key=lambda resource: (resource.created_at, resource.id))

    return SessionResponse(
        id=session.id,
        agent_id=session.agent_id,
        agent_version=session.agent_version,
        environment_id=session.environment_id,
        model=session.model,
        account_id=session.account_id,
        title=session.title,
        status=session.status,
        stop_reason=session.stop_reason,
        last_event_seq=session.last_event_seq,
        sandbox_id=sandbox_id,
        resources=resources,
        created_at=session.created_at,
        updated_at=session.updated_at,
        archived_at=session.archived_at,
    )


def to_event(event: SessionEvent) -> EventResponse:
    """A stored row becomes whichever of the fourteen shapes it was written as.

    `models.events` owns the mapping; the router only needs the result.
    """
    return event_models.from_row(event)
