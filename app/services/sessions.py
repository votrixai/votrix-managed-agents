"""Session use cases: hold a conversation, and run one turn of it.

Two callers with different jobs:

* the API, which accepts messages — and refuses them while the agent is busy,
  because there is no queue behind this;
* the worker, which runs exactly one turn per invocation.

The rule that makes the whole thing work is that a session is claimed
atomically. Everything else here is arranged around that one fact.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import quote

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import (
    Agent,
    AgentVersion,
    Environment,
    File,
    Session,
    SessionEvent,
    SessionFile,
    SessionSandbox,
)
from app.db.models.memory import MEMORY_ACCESS_READ_WRITE
from app.db.queries import agents as agents_q
from app.db.queries import environments as environments_q
from app.db.queries import sessions as sessions_q
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.models import events as event_types
from app.models.errors import (
    Conflict,
    MemoryStoreUnavailable,
    NotFound,
    SandboxUnavailable,
    SessionBusy,
    SessionCancelled,
)
from app.models.sessions import IDLE, RUNNING, STOP_ERROR, STOP_INTERRUPTED, TERMINATED
from app.services import agents as agents_service
from app.services import environments as environments_service
from app.services import files as files_service
from app.services import memory as memory_service
from app.services import memory_records
from app.utils.sandbox import OUTPUTS_DIR, UPLOADS_DIR, Image, Sandbox
from app.utils.timing import timed
from app.utils.volume import (
    InvalidVolumeBinding,
    SandboxVolumeMount,
    Volume,
    memory_mount_path,
)

# A turn may hold the session this long before we call it hung.
TURN_TIMEOUT_SECONDS = 600

# Deliberately much shorter than a turn: the lease is not a time budget, it is
# a liveness signal. A worker that dies is noticed within this window rather
# than after the full turn timeout.
LEASE_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 45

# Long enough for a container to finish downloading, short enough that a URL
# leaking out of a log is worth nothing by the time anyone reads it.
SKILL_URL_TTL_SECONDS = 300
# The container fetches its attached files during provisioning, so this only
# has to outlive one sandbox start.
FILE_URL_TTL_SECONDS = 300

# The same ceiling CMA puts on an agent.
MAX_SKILLS_PER_AGENT = 20
MAX_MEMORY_STORES_PER_SESSION = 8

# A Cloud Run instance accepts many cheap API requests at once, but a cold
# Session fans out into an E2B container plus up to ten simultaneous object
# downloads.  Bound only that expensive path; reads, streams and warm turns do
# not pass through this semaphore.
MAX_CONCURRENT_SESSION_PROVISIONS = 4
_session_provision_slots = asyncio.Semaphore(MAX_CONCURRENT_SESSION_PROVISIONS)

# How often a stream looks for new events. A turn produces something every few
# seconds at best, so this is well inside "live" for a reader, and the query it
# runs is an indexed range scan that almost always comes back empty.
STREAM_POLL_SECONDS = 0.3
# Proxies and load balancers close connections that go quiet. A turn can think
# for minutes without emitting, so the stream says something regardless.
STREAM_HEARTBEAT_SECONDS = 15
# A browser tab left open should not poll forever. The client reconnects with
# the last id it saw and misses nothing.
STREAM_MAX_SECONDS = 1800
STREAM_BATCH = 100

Emit = Callable[[str, dict[str, Any]], Awaitable[SessionEvent]]

logger = structlog.get_logger(__name__)

# Built on first dispatch and kept for the life of the process.
_tasks_client: Any | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- API side ---------------------------------------------------------------


async def create_session(
    db: AsyncSession,
    *,
    organization_id: str,
    agent_id: str,
    environment_id: str,
    agent_version: int | None = None,
    model: str | dict[str, Any] | None = None,
    title: str | None = None,
    resources: list[dict[str, Any]] | None = None,
) -> Session:
    """Open a conversation and give it a sandbox to live in.

    The agent version is pinned now and never moves, so editing the agent
    later cannot change what a conversation already in flight is running.

    A ``model`` is pinned on the same terms. Left out, the session stores none
    and the pinned agent version's model applies at run time.

    Attached files go in while the container is being built. That is the only
    chance: a session keeps one sandbox for its whole life, so there is nowhere
    to put a file handed over later.
    """
    async with timed(
        "session_provision_slot_wait",
        organization_id=organization_id,
        max_concurrent=MAX_CONCURRENT_SESSION_PROVISIONS,
    ):
        await _session_provision_slots.acquire()
    try:
        return await _create_session(
            db,
            organization_id=organization_id,
            agent_id=agent_id,
            environment_id=environment_id,
            agent_version=agent_version,
            model=model,
            title=title,
            resources=resources,
        )
    finally:
        _session_provision_slots.release()


async def _create_session(
    db: AsyncSession,
    *,
    organization_id: str,
    agent_id: str,
    environment_id: str,
    agent_version: int | None = None,
    model: str | dict[str, Any] | None = None,
    title: str | None = None,
    resources: list[dict[str, Any]] | None = None,
) -> Session:
    agent = await _require_agent(db, agent_id=agent_id, organization_id=organization_id)
    version_number = agent_version if agent_version is not None else agent.active_version
    version = await agents_q.get_agent_version(
        db,
        agent_id=agent_id,
        version=version_number,
        organization_id=organization_id,
    )
    if version is None:
        raise NotFound(f"Agent {agent_id} has no version {version_number}")

    environment = await environments_q.get_environment(
        db,
        environment_id=environment_id,
        organization_id=organization_id,
    )
    if environment is None:
        raise NotFound(f"Environment {environment_id} not found")
    # Archived, still building, or built and failed — all mean this session
    # would have nothing to run in.
    environment = await environments_service.require_usable(db, environment)

    session = await sessions_q.create_session(
        db,
        organization_id=organization_id,
        agent_id=agent_id,
        agent_version=version_number,
        environment_id=environment_id,
        # Normalised on the way in, never on the way out, so every reader of the
        # column — runtime, API response, a future query — sees one shape.
        model=agents_service.normalize_model(model) if model is not None else None,
        title=title,
    )
    # Resolved before the container exists, so a missing or half-uploaded file
    # fails the request rather than leaving a running session short of an input
    # it was told it had.
    await provision_sandbox(
        db,
        session,
        version=version,
        environment=environment,
        resources=resources,
    )
    await db.commit()
    return session


async def get_session(db: AsyncSession, *, session_id: str, organization_id: str) -> Session:
    session = await sessions_q.get_session(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    if session is None:
        raise NotFound(f"Session {session_id} not found")
    return session


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
    return await sessions_q.list_sessions(
        db,
        organization_id=organization_id,
        status=status,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def list_session_files(db: AsyncSession, *, session_id: str):
    """The File resources snapshotted onto one Session."""
    return await sessions_q.list_session_files(db, session_id=session_id)


async def list_session_memory_stores(db: AsyncSession, *, session_id: str):
    """The Memory Store resources snapshotted onto one Session."""
    return await sessions_q.list_session_memory_stores(db, session_id=session_id)


async def update_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    title: str | None,
) -> Session:
    session = await get_session(db, session_id=session_id, organization_id=organization_id)
    await sessions_q.update_session_title(db, session, title=title)
    await db.commit()
    return session


async def archive_session(db: AsyncSession, *, session_id: str, organization_id: str) -> Session:
    session = await get_session(db, session_id=session_id, organization_id=organization_id)
    await sessions_q.archive_session(db, session)
    await db.commit()
    return session


async def delete_session(db: AsyncSession, *, session_id: str, organization_id: str) -> Session:
    session = await get_session(db, session_id=session_id, organization_id=organization_id)
    await sessions_q.delete_session(db, session)
    await db.commit()
    return session


async def send_events(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    events: list[dict[str, Any]],
) -> list[SessionEvent]:
    """Accept a batch of client events, or refuse the whole batch.

    Claiming the session is what makes "one turn at a time" true. Reading the
    status and then writing it would let two requests both see an idle session,
    both accept a message, and leave the second one with nobody to run it.

    Nothing is written unless the claim succeeds.
    """
    session = await get_session(db, session_id=session_id, organization_id=organization_id)

    if any(event.get("type") == event_types.USER_INTERRUPT for event in events):
        if len(events) > 1:
            raise Conflict("user.interrupt must be sent on its own")
        return [await cancel_session(db, session_id=session_id, organization_id=organization_id)]

    claimed = await sessions_q.claim_session(
        db,
        session_id=session_id,
        lease_seconds=LEASE_SECONDS,
    )
    # claim_session goes around the ORM either way, so the in-memory row is
    # stale here whether we won or lost.
    await db.refresh(session)
    if not claimed:
        raise SessionBusy()

    appended = [
        await sessions_q.append_event(
            db,
            session,
            type=event["type"],
            source="user",
            payload={key: value for key, value in event.items() if key != "type"},
        )
        for event in events
    ]
    await db.commit()

    # The whole batch goes, not the first of it. A paused graph is resumed with
    # one decision per call it stopped on, counted; handing over a subset would
    # fail inside the turn instead of here.
    await _dispatch_turn(
        db,
        session_id=session_id,
        generation=session.lock_version,
        events=events,
    )
    return appended


async def cancel_session(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
) -> SessionEvent:
    """Stop whatever is running. The one path that skips the gate.

    An interrupt exists precisely to reach a busy session, so it cannot be made
    to wait for one. The running worker finds out on its next write, when the
    generation it captured no longer matches.

    Stopping an idle session is not an error — the user pressed the button, and
    that gets recorded — but there is nothing to release, so the session itself
    is left alone. Two interrupts in a row therefore do the same thing as one.
    """
    session = await get_session(db, session_id=session_id, organization_id=organization_id)
    if session.status == TERMINATED:
        raise Conflict(f"Session {session_id} is terminated")

    event = await sessions_q.append_event(
        db,
        session,
        type=event_types.USER_INTERRUPT,
        source="user",
        payload={},
    )

    if session.status == RUNNING:
        # Nothing beyond the type: the client knows what it sent, and a field
        # here listing "what was cut off" beside `requires_action`'s list of
        # "what to answer" is two meanings one glance apart.
        stop_reason: dict[str, Any] = {"type": STOP_INTERRUPTED}
        await sessions_q.release_session(db, session, status=IDLE, stop_reason=stop_reason)
        # The engine emits this itself when a turn ends normally; an interrupted
        # turn unwinds through an exception instead, so nobody else will.
        await sessions_q.append_event(
            db,
            session,
            type=event_types.SESSION_STATUS_IDLE,
            source="system",
            payload={"stop_reason": stop_reason},
        )

    await db.commit()
    return event


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
    await get_session(db, session_id=session_id, organization_id=organization_id)
    return await sessions_q.list_events(
        db,
        session_id=session_id,
        organization_id=organization_id,
        after_seq=after_seq,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def get_event(db: AsyncSession, *, event_id: str, organization_id: str) -> SessionEvent:
    event = await sessions_q.get_event(db, event_id=event_id, organization_id=organization_id)
    if event is None:
        raise NotFound(f"Event {event_id} not found")
    return event


async def stream_events(
    *,
    session_id: str,
    organization_id: str,
    after_seq: int | None = None,
) -> AsyncIterator[SessionEvent | None]:
    """Yield events as they are written, then keep waiting for more.

    Reads the table rather than watching the turn, because the turn is not
    here: under `cloud` dispatch it runs in another process, and even inline it
    belongs to a different request than this one. What makes that work is that
    a turn commits one event at a time — so a reader sees each one within a
    poll of it being written, rather than the whole turn at the end.

    Takes no database session from the caller and holds none of its own. A
    stream lives for as long as someone is watching, and a connection held open
    that long is a connection out of the pool for that long; each poll borrows
    one and gives it straight back.

    `None` is yielded on a poll that found nothing, so the caller can keep the
    connection warm without knowing how long it has been quiet.
    """
    async with session_scope() as db:
        # Once, up front: the tenant check, and a 404 the client sees as a
        # failed request rather than as an empty stream.
        await get_session(db, session_id=session_id, organization_id=organization_id)

    last_seq = after_seq or 0
    deadline = time.monotonic() + STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        async with session_scope() as db:
            page = await sessions_q.list_events(
                db,
                session_id=session_id,
                organization_id=organization_id,
                after_seq=last_seq,
                limit=STREAM_BATCH,
            )
            events = page.items
            session = await sessions_q.get_session(
                db, session_id=session_id, organization_id=organization_id
            )

        for event in events:
            last_seq = event.seq
            yield event

        # A terminated session will never produce another event, and a deleted
        # one has nothing left to watch.
        if session is None or session.status == TERMINATED:
            return
        if not events:
            yield None
            await asyncio.sleep(STREAM_POLL_SECONDS)


async def _dispatch_turn(
    db: AsyncSession,
    *,
    session_id: str,
    generation: int,
    events: list[dict[str, Any]],
) -> None:
    """Get the turn running, by whichever route this deployment uses.

    `inline` runs it here and now, so the request does not come back until the
    agent is done. Slow, but a fresh checkout can hold a conversation without
    a queue, a worker process, or any configuration.

    Failures are swallowed in either mode. The message really was accepted —
    it is committed — and `_fail` records what went wrong as a `session.error`
    event, so the client learns about it the same way it learns everything
    else, instead of from a 500 on a request that actually succeeded.
    """
    if get_settings().turn_dispatch == "cloud":
        await _enqueue_task(session_id=session_id, generation=generation, events=events)
        return

    try:
        await process_session(db, session_id=session_id, events=events)
    except Exception:
        logger.exception("inline_turn_failed", session_id=session_id)


# --- worker side ------------------------------------------------------------


async def process_session(
    db: AsyncSession,
    *,
    session_id: str,
    events: list[dict[str, Any]],
) -> None:
    """Run one turn. The API already claimed the session before enqueueing us.

    `events` is the batch the client sent, carried in the task payload. There
    is no need to reconstruct it from the event log: the history the agent
    needs lives in the graph checkpoint, not in our tables.
    """
    session = await sessions_q.get_session_by_id(db, session_id=session_id)
    if session is None:
        raise NotFound(f"Session {session_id} not found")

    # Either the turn was cancelled before we got here, or this is a second
    # delivery of a task we already ran. Both mean: do nothing.
    if session.status != RUNNING:
        return

    sandbox = await sessions_q.get_sandbox(
        db,
        session_id=session_id,
        organization_id=session.organization_id,
    )
    if sandbox is None or _sandbox_is_gone(sandbox):
        await _terminate(db, session, reason="sandbox_unavailable")
        raise SandboxUnavailable(f"Session {session_id} has no usable sandbox")

    version = await agents_q.get_agent_version(
        db,
        agent_id=session.agent_id,
        version=session.agent_version,
        organization_id=session.organization_id,
    )
    if version is None:
        await _terminate(db, session, reason="agent_version_missing")
        raise NotFound(f"Agent version {session.agent_version} not found")

    # Whatever generation the session is on now is the one this turn owns.
    # Any release — an interrupt, a failure, the next turn — moves it on.
    generation = session.lock_version

    # What the user attached. Named in the prompt rather than left to be
    # discovered, so the agent does not have to go looking for its own inputs.
    attached = [
        row.path
        for row in await sessions_q.list_session_files(db, session_id=session_id)
    ]
    attached_memory_stores = [
        {
            "name": row.name,
            "description": row.description,
            "instructions": row.instructions,
            "access": row.access,
            "mount_path": row.mount_path,
        }
        for row in await sessions_q.list_session_memory_stores(
            db, session_id=session_id
        )
    ]

    container = Sandbox.from_id(
        sandbox.external_sandbox_id,
        session.id,
        session.organization_id,
    )

    async def _sync_after_filesystem_tool(_tool_name: str) -> None:
        # The callback runs after the tool result has been emitted, so every
        # write is complete before its mounted files are hashed. Failures are
        # retried by the final reconciliation rather than failing the model's
        # otherwise successful turn.
        await collect_memories(
            db,
            session,
            container,
            report_failure=False,
        )

    heartbeat = asyncio.create_task(_hold_lease(session_id))
    try:
        from app.runtime.engine import execute_agent

        # The denominator. Every other timing in a turn is a slice of this one,
        # and what the slices do not add up to is what nothing is watching yet.
        async with timed(
            "turn_finished",
            session_id=session_id,
            trigger=events[0].get("type") if events else None,
        ):
            await asyncio.wait_for(
                execute_agent(
                    session=session,
                    version=version,
                    events=events,
                    sandbox=container,
                    attached_files=attached,
                    attached_memory_stores=attached_memory_stores,
                    tool_completed=(
                        _sync_after_filesystem_tool
                        if attached_memory_stores
                        else None
                    ),
                    emit=_emitter(db, session, generation),
                ),
                timeout=TURN_TIMEOUT_SECONDS,
            )
    except SessionCancelled:
        # cancel_session already released the session and recorded the stop.
        # The agent may still have finished something before it was cut off,
        # and a half-written deliverable is the user's too.
        await collect_memories(db, session, container)
        await collect_outputs(db, session, container)
        await db.commit()
        return
    except BaseException as exc:
        await collect_memories(db, session, container)
        await collect_outputs(db, session, container)
        await _fail(db, session, exc)
        raise
    finally:
        heartbeat.cancel()

    # Before the release, never after: the client takes `idle` as the sign that
    # the turn is done, and everything the turn produced has to be fetchable by
    # then. It is also the last moment the container is certainly awake.
    await collect_memories(db, session, container)
    await collect_outputs(db, session, container)
    await sessions_q.release_session(db, session, status=IDLE)
    await db.commit()


def _emitter(db: AsyncSession, session: Session, generation: int) -> Emit:
    """Build the callback the engine writes its output through.

    Checking right here — immediately before the write — is what lets an
    interrupt take effect without anyone polling for it: the first event the
    agent produces after being cancelled is the one that gets refused.

    The check is on the generation, not on the status. By the time a cancelled
    worker next tries to write, the user may already have sent another message
    and started a fresh turn, putting the session back into `running` — a
    status check would wave the stale worker through into someone else's turn.
    """

    async def emit(type: str, payload: dict[str, Any]) -> SessionEvent:
        # Re-reading is safe because each event is committed on its own, so
        # there is never pending work here for the refresh to discard.
        await db.refresh(session, ["status", "lock_version", "last_event_seq"])
        if session.lock_version != generation or session.status != RUNNING:
            raise SessionCancelled(f"Session {session.id} was interrupted")
        event = await sessions_q.append_event(
            db,
            session,
            type=type,
            source="agent",
            payload=payload,
        )
        # One commit per event, so the client's stream sees output as it is
        # produced instead of in one lump when the turn ends.
        await db.commit()
        return event

    return emit


async def _enqueue_task(
    *, session_id: str, generation: int, events: list[dict[str, Any]]
) -> None:
    """Hand the turn to Cloud Tasks, which calls back into this service to run it.

    The task is named after the turn it runs, so a retry that was already
    delivered is rejected by name instead of running the turn a second time —
    Cloud Tasks guarantees at-least-once delivery, and this is what turns that
    into at-most-once. `lock_version` is what makes the name unique: it moves
    every time a turn ends, so the next message on this session gets its own.
    """
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import tasks_v2
    from google.protobuf import duration_pb2

    settings = get_settings()
    client = _cloud_tasks_client()
    parent = client.queue_path(
        settings.tasks_project,
        settings.tasks_location,
        settings.tasks_queue,
    )
    worker_url = settings.worker_url.rstrip("/")
    task = tasks_v2.Task(
        name=f"{parent}/tasks/turn-{session_id}-{generation}",
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{worker_url}/internal/sessions/{quote(session_id, safe='')}/process",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"events": events}).encode(),
            # Cloud Tasks signs the call, and the endpoint checks the signature.
            # Without it anything that learned the URL could start turns.
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.tasks_service_account,
                audience=worker_url,
            ),
        ),
        # Give the task longer than a turn is allowed to take, so Cloud Tasks
        # never retries something that is merely still working.
        dispatch_deadline=duration_pb2.Duration(seconds=TURN_TIMEOUT_SECONDS + 120),
    )
    try:
        await client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        logger.info("turn_already_queued", session_id=session_id, generation=generation)


def _cloud_tasks_client():
    """One client for the process. Building it opens a channel."""
    global _tasks_client
    if _tasks_client is None:
        from google.cloud import tasks_v2

        _tasks_client = tasks_v2.CloudTasksAsyncClient()
    return _tasks_client


async def _hold_lease(session_id: str) -> None:
    """Renew the lease until cancelled — the worker's proof that it is alive.

    Runs on its own connection: one AsyncSession cannot be shared by two
    coroutines, and this one runs alongside the turn.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        async with session_scope() as db:
            session = await sessions_q.get_session_by_id(db, session_id=session_id)
            if session is None or session.status != RUNNING:
                return
            await sessions_q.extend_session_lease(db, session, lease_seconds=LEASE_SECONDS)
            await db.commit()


async def provision_sandbox(
    db: AsyncSession,
    session: Session,
    *,
    version: AgentVersion,
    environment: Environment,
    resources: list[dict[str, Any]] | None = None,
) -> Sandbox:
    """Give a new session the container it will run in, already stocked.

    What goes in is decided here; how it gets there is the sandbox's business.
    This layer only names files and skills — it never signs a URL or knows
    where an object lives in the bucket.
    """
    image = Image.from_environment(environment)
    if image is None:
        raise SandboxUnavailable(f"Environment {environment.id} has no image to start from")
    declared = resources or []
    for resource in declared:
        if resource.get("type") not in (None, "file", "memory_store"):
            raise Conflict(f"Unsupported resource type {resource.get('type')!r}")

    files = await _attach_files(
        db,
        session,
        [resource for resource in declared if resource.get("type") in (None, "file")],
    )
    memory_mounts = await _attach_memory_stores(
        db,
        session,
        [resource for resource in declared if resource.get("type") == "memory_store"],
    )

    return await Sandbox.provision(
        db,
        session,
        image=image,
        skill_ids=_skill_ids(version),
        files=files,
        memory_mounts=memory_mounts,
    )


async def capture_file(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    path: str,
) -> File:
    """Take one file out of a running session's sandbox, now.

    Outputs are normally collected when a turn ends, which is fine for a client
    that waits for the session to go idle and useless for one watching a long
    turn. This is the other case: the agent has finished a deliverable and
    somebody wants it before the rest of the work is done.

    What comes back is an ordinary file record — the same one the end of the
    turn would have produced, and downloaded the same way. Asking twice for an
    unchanged file returns the same record rather than a second copy.
    """
    session = await get_session(db, session_id=session_id, organization_id=organization_id)
    sandbox = await sessions_q.get_sandbox(
        db, session_id=session_id, organization_id=organization_id
    )
    if sandbox is None or _sandbox_is_gone(sandbox):
        raise Conflict(f"Session {session_id} no longer has a sandbox to read from")

    container = Sandbox.from_id(
        sandbox.external_sandbox_id, session.id, session.organization_id
    )
    relative = _output_path(path)
    try:
        file = await container.download_file(
            db,
            f"{OUTPUTS_DIR}/{relative}",
            scope_id=session.id,
            filename=relative,
        )
    except ValueError as exc:
        raise NotFound(f"{path!r} is not a file in this session's outputs") from exc
    await db.commit()
    return file


async def attach_live_file(
    db: AsyncSession,
    *,
    session_id: str,
    organization_id: str,
    file_id: str,
    path: str | None = None,
) -> SessionFile:
    """Put an existing durable File into an existing Session sandbox.

    This is the post-create counterpart to a ``file`` resource on Session
    creation. It is deliberately accepted only while the Session is idle: the
    next turn re-reads ``session_files`` for its prompt, whereas an already
    running turn has already snapshotted that list and could observe the bytes
    without knowing the user attached them.

    The Session row stays locked across the E2B transfer and binding write.
    ``send_events`` claims that same row, so a turn cannot start between those
    two operations. The binding is flushed before the transfer but committed
    only after it succeeds; a failed copy therefore leaves no phantom
    resource. Repeating the exact request returns the original binding.
    """
    session = await sessions_q.get_session_for_update(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    if session is None:
        raise NotFound(f"Session {session_id} not found")
    if session.archived_at is not None:
        raise Conflict(f"Session {session_id} is archived")
    if session.status == RUNNING:
        raise SessionBusy()
    if session.status != IDLE:
        raise Conflict(
            f"Session {session_id} cannot accept files while {session.status}"
        )

    sandbox = await sessions_q.get_sandbox(
        db,
        session_id=session_id,
        organization_id=organization_id,
    )
    if sandbox is None or _sandbox_is_gone(sandbox):
        raise Conflict(f"Session {session_id} no longer has a sandbox to write to")
    if sandbox.provider != "e2b":
        raise Conflict(
            f"Session {session_id} uses unsupported sandbox provider {sandbox.provider!r}"
        )

    file = await files_service.get_file(
        db,
        file_id=file_id,
        organization_id=organization_id,
    )
    relative = _mount_path(path, fallback=file.filename)

    for existing in await sessions_q.list_session_files(db, session_id=session_id):
        if existing.path != relative:
            continue
        if existing.file_id == file.id:
            await db.commit()
            return existing
        raise Conflict(
            f"Another file is already attached at {relative!r} in Session {session_id}"
        )

    attached = await sessions_q.attach_file(
        db,
        session,
        file_id=file.id,
        path=relative,
    )
    container = Sandbox.from_id(
        sandbox.external_sandbox_id,
        session.id,
        session.organization_id,
    )
    try:
        # Existing sandboxes may predate the shared CMA/VMA path contract.
        # Re-applying the idempotent layout both backfills those aliases and
        # fails the attachment instead of committing bytes the agent cannot
        # reach through /mnt/session/uploads.
        await container.prepare_directories()
        await container.upload_file(db, f"{UPLOADS_DIR}/{relative}", file.id)
    except Exception as exc:
        # ``attach_file`` was intentionally flushed first so its unique path
        # constraint is part of this transaction, but it must never survive a
        # failed transfer as a resource that only exists on paper.
        message = f"File {file.id} could not be copied into Session {session.id}"
        await db.rollback()
        raise SandboxUnavailable(message) from exc

    await db.commit()
    return attached


def _output_path(requested: str) -> str:
    """Which file under `outputs/` is being asked for.

    Relative and inside that directory, both for the same reason the mount path
    is: everything else in the container is either the user's own input or the
    instructions the agent runs on, and neither is this endpoint's business.
    """
    candidate = requested.strip().removeprefix(f"{OUTPUTS_DIR}/")
    if not candidate:
        raise Conflict("A path is required")
    if candidate.startswith("/"):
        raise Conflict(f"{requested!r} must be relative to outputs/")
    parts = PurePosixPath(candidate).parts
    if any(part in ("..", ".") for part in parts):
        raise Conflict(f"{requested!r} is not a path inside outputs/")
    return "/".join(parts)


async def collect_outputs(db: AsyncSession, session: Session, sandbox: Sandbox) -> None:
    """Take whatever the agent left in `outputs/` before letting the session go.

    This is the only moment the container is reliably awake: it is idle from
    here, paused within the quarter hour, and eventually reclaimed. Anything
    not taken now would have to be taken from a container that must first be
    woken, and then from one that no longer exists.

    Failing here does not fail the turn. The agent did the work and the events
    are written; what is lost is the delivery, so it is recorded as an event
    the client can see rather than as an error on a turn that succeeded.
    """
    try:
        collected = await sandbox.discover_outputs(db)
    except Exception as exc:
        logger.exception("output_collection_failed", session_id=session.id)
        await sessions_q.append_event(
            db,
            session,
            type=event_types.SESSION_ERROR,
            source="system",
            payload={
                "error": {
                    "type": "output_collection_failed",
                    "message": f"The agent's output files could not be collected: {exc}",
                }
            },
        )
        return
    if collected:
        logger.info("outputs_collected", session_id=session.id, count=len(collected))


async def collect_memories(
    db: AsyncSession,
    session: Session,
    sandbox: Sandbox,
    *,
    report_failure: bool = True,
) -> bool:
    """Index mounted Volume changes without turning delivery into turn failure."""
    try:
        result = await memory_records.reconcile_session_memory_stores(
            db,
            session_id=session.id,
            organization_id=session.organization_id,
            sandbox=sandbox,
        )
    except Exception as exc:
        await db.rollback()
        logger.exception("memory_reconciliation_failed", session_id=session.id)
        if report_failure:
            await sessions_q.append_event(
                db,
                session,
                type=event_types.SESSION_ERROR,
                source="system",
                payload={
                    "error": {
                        "type": "memory_reconciliation_failed",
                        "message": str(exc),
                    }
                },
            )
        return False

    if result.changed:
        logger.info(
            "memories_reconciled",
            session_id=session.id,
            changed=result.changed,
        )
    if result.issues:
        logger.warning(
            "memory_reconciliation_rejected_files",
            session_id=session.id,
            count=len(result.issues),
        )
        if report_failure:
            await sessions_q.append_event(
                db,
                session,
                type=event_types.SESSION_ERROR,
                source="system",
                payload={
                    "error": {
                        "type": "memory_write_rejected",
                        "message": "Some mounted files could not become CMA Memories",
                        "issues": list(result.issues[:20]),
                    }
                },
            )
    return True


async def _attach_files(
    db: AsyncSession,
    session: Session,
    resources: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Record what this session was given, as `(file_id, path)` pairs.

    Each file is checked to belong to this organization and to have finished
    uploading, so a session never starts believing it has an input that is not
    there.
    """
    attached: list[tuple[str, str]] = []
    seen: set[str] = set()
    for resource in resources:
        file = await files_service.get_file(
            db,
            file_id=str(resource.get("file_id") or ""),
            organization_id=session.organization_id,
        )
        path = _mount_path(resource.get("path"), fallback=file.filename)
        if path in seen:
            raise Conflict(f"Two resources both want to be {path!r}")
        seen.add(path)

        await sessions_q.attach_file(db, session, file_id=file.id, path=path)
        attached.append((file.id, path))
    return attached


async def _attach_memory_stores(
    db: AsyncSession,
    session: Session,
    resources: list[dict[str, Any]],
) -> list[SandboxVolumeMount]:
    """Resolve persistent Stores into native E2B mounts before provisioning."""
    if len(resources) > MAX_MEMORY_STORES_PER_SESSION:
        raise Conflict(
            f"A Session may attach at most {MAX_MEMORY_STORES_PER_SESSION} Memory Stores"
        )

    mounts: list[SandboxVolumeMount] = []
    seen_stores: set[str] = set()
    seen_paths: set[str] = set()
    for resource in resources:
        memory_store_id = str(resource.get("memory_store_id") or "")
        if memory_store_id in seen_stores:
            raise Conflict(f"Memory Store {memory_store_id} was attached more than once")
        seen_stores.add(memory_store_id)

        access = str(resource.get("access") or MEMORY_ACCESS_READ_WRITE)
        if access != MEMORY_ACCESS_READ_WRITE:
            # E2B 2.31.0 has no read-only Volume mount option. Rejecting this is
            # the security boundary: filtering write_file/edit_file alone
            # would still let the execute tool write through the same mount.
            raise Conflict("read_only Memory Store mounts are not supported by E2B")

        store = await memory_service.require_attachable(
            db,
            memory_store_id=memory_store_id,
            organization_id=session.organization_id,
        )
        mount_path = memory_mount_path(store.name, store.id)
        if mount_path in seen_paths:
            raise Conflict(
                f"Two Memory Stores resolve to the same mount path {mount_path!r}"
            )
        seen_paths.add(mount_path)

        instructions = resource.get("instructions")
        attached = await sessions_q.attach_memory_store(
            db,
            session,
            store,
            access=access,
            instructions=str(instructions) if instructions is not None else None,
            mount_path=mount_path,
        )
        try:
            mounts.append(Volume.mount(store, attached.mount_path))
        except InvalidVolumeBinding as exc:
            raise MemoryStoreUnavailable(
                f"Memory Store {store.id} has no mountable provider Volume"
            ) from exc
    return mounts


def _mount_path(requested: str | None, *, fallback: str) -> str:
    """Where inside `uploads/` a file lands.

    Everything here is about one guarantee: the result stays under that
    directory. `skills/` sits beside it and the agent reads it as instructions,
    so an upload that could reach it would be an upload that rewrites the agent.
    """
    candidate = (requested or fallback or "").strip()
    if not candidate:
        raise Conflict("A file resource needs a path or a filename")
    if len(candidate) > 512:
        raise Conflict("That path is too long")
    if "\x00" in candidate:
        raise Conflict("A file resource path cannot contain NUL")
    if "\\" in candidate:
        raise Conflict("A file resource path must use forward slashes")
    # Stripping the leading slash off `/etc/passwd` would put the file in
    # `uploads/etc/passwd` — safe, and not what was asked for. A request that
    # cannot be honoured exactly is refused rather than turned into a different
    # one the client never made.
    if candidate.startswith("/"):
        raise Conflict(f"{candidate!r} must be relative to uploads/")
    raw_parts = candidate.split("/")
    if any(part in ("", "..", ".") for part in raw_parts):
        raise Conflict(f"{candidate!r} is not a path inside uploads/")
    # PurePosixPath is retained as a final platform-independent sanity check;
    # unlike it, the raw split above deliberately refuses paths it would
    # normalize (``a//b`` or ``a/./b``) rather than silently changing them.
    parts = PurePosixPath(candidate).parts
    return "/".join(parts)


def _skill_ids(version: AgentVersion) -> list[str]:
    """The skills this agent version asks for.

    Only `skill_id` is read. A CMA client also sends `type` and `version`; we
    have neither an Anthropic-provided library nor skill versions, so a
    reference to one simply does not resolve and the sandbox says so.
    """
    refs = version.skills or []
    if len(refs) > MAX_SKILLS_PER_AGENT:
        raise Conflict(f"An agent may reference at most {MAX_SKILLS_PER_AGENT} skills")
    return [str(ref.get("skill_id") or "") for ref in refs]


# --- internals --------------------------------------------------------------


async def _require_agent(db: AsyncSession, *, agent_id: str, organization_id: str) -> Agent:
    agent = await agents_q.get_agent(db, agent_id=agent_id, organization_id=organization_id)
    if agent is None:
        raise NotFound(f"Agent {agent_id} not found")
    return agent


def _sandbox_is_gone(sandbox: SessionSandbox) -> bool:
    """True once the sandbox can no longer be used.

    Only what we know for certain. `expires_at` looks like it belongs here and
    does not: it is when E2B would *pause* an idle container, not when the
    container dies, and a paused one wakes up as soon as anything connects.
    Worse, every connection pushes that moment back at E2B while the recorded
    copy stays where creation left it — so a session that ran past its first
    quarter hour would fail a check its container had long outlived.

    A container that really has gone announces itself: `ensure_connected` fails
    on the next call. Guessing earlier only ever ends sessions that were fine.
    """
    if sandbox.external_sandbox_id is None:
        return True
    return sandbox.state in ("terminated", "failed")


async def _terminate(db: AsyncSession, session: Session, *, reason: str) -> None:
    """End the session for good and tell the client why.

    Two events, in this order: what went wrong, then that the session is over.
    A client reads the first to explain itself and the second to stop waiting,
    and it must be able to stop waiting even if it does not understand the
    reason it was given.
    """
    await sessions_q.append_event(
        db,
        session,
        type=event_types.SESSION_ERROR,
        source="system",
        payload={"error": {"type": reason}},
    )
    await sessions_q.append_event(
        db,
        session,
        type=event_types.SESSION_STATUS_TERMINATED,
        source="system",
        payload={},
    )
    await sessions_q.release_session(
        db,
        session,
        status=TERMINATED,
        stop_reason={"type": reason},
    )
    await db.commit()


async def _fail(db: AsyncSession, session: Session, exc: BaseException) -> None:
    """Record a failed turn but leave the session usable.

    The `session.status_idle` is not decoration. `idle` is the only thing that
    means "the turn is over, your go", and a client on the stream has nothing
    else to wait for — a failed turn that skipped it would leave that client
    waiting for an event that is never coming, on a session that is in fact
    ready for its next message.
    """
    await sessions_q.append_event(
        db,
        session,
        type=event_types.SESSION_ERROR,
        source="system",
        payload={"error": {"type": type(exc).__name__, "message": str(exc)}},
    )
    await sessions_q.append_event(
        db,
        session,
        type=event_types.SESSION_STATUS_IDLE,
        source="system",
        payload={"stop_reason": {"type": STOP_ERROR}},
    )
    await sessions_q.release_session(
        db,
        session,
        status=IDLE,
        stop_reason={"type": STOP_ERROR},
    )
    await db.commit()
