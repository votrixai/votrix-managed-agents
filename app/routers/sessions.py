import asyncio
import json
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_contract import normalize_agent_tools, validate_mcp_bindings
from app.auth import require_api_access
from app.config import get_settings
from app.db.engine import get_session, session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as env_q
from app.db.queries import events as events_q
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.event_validation import validate_system_message_batch, validate_user_define_outcome_event
from app.metadata import merge_metadata, normalize_metadata
from app.models.common import ListResponse
from app.models.events import (
    SendEventsRequest,
    SendEventsResponse,
    SessionEventResponse,
    event_to_response,
    event_to_sse,
)
from app.models.sessions import (
    AgentReference,
    SessionCreateRequest,
    SessionResponse,
    SessionUpdateRequest,
    session_to_response,
)
from app.models.resources import GenericBody
from app.pagination import filter_created_at, normalize_sort_order, paginate, sort_by_created_at
from app.runtime.sandbox_lifecycle import (
    SandboxLifecycleConfigurationError,
    delete_session_sandbox,
    pause_session_sandbox,
    provision_session_sandbox,
    session_has_managed_sandbox,
)
from app.runtime.sandbox_providers import SandboxProviderError
from app.runtime.work_queue import enqueue_session_run, execute_work_item, should_execute_inline
from app.runtime.vma_preview_bus import vma_preview_bus
from app.session_resources import (
    create_session_resource,
    delete_session_resource_file,
    ensure_session_resource_deletable,
    rotate_session_resource_token,
    session_has_memory_store,
    session_resource_response,
    session_resources_response,
)
from app.session_state import (
    ACTIVE_STATUSES,
    ACTION_RESULT_EVENTS,
    SESSION_IDLE,
    SESSION_RESCHEDULING,
    SESSION_RUNNING,
    SESSION_TERMINATED,
    blocks_mutation,
    can_start_work,
    is_action_result,
    is_waiting_for_action,
    pending_action_ids,
    starts_work,
)

SESSION_LIST_STATUSES = {SESSION_IDLE, SESSION_RUNNING, SESSION_RESCHEDULING, SESSION_TERMINATED}
VMA_PREVIEWABLE_EVENT_TYPES = frozenset({"agent.message", "agent.thinking"})

router = APIRouter(
    prefix="/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    agent_id, version = _resolve_agent_ref(body.agent)
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None or agent.archived_at is not None:
        raise HTTPException(status_code=404, detail="Agent not found")
    pinned_version = version or agent.active_version
    agent_version = await agents_q.get_agent_version(db, agent_id=agent.id, version=pinned_version)
    if agent_version is None:
        raise HTTPException(status_code=404, detail="Agent version not found")

    environment = await env_q.get_environment(db, body.environment_id)
    if environment is None or environment.deleted_at is not None or environment.archived_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    vault_ids = await _validate_session_vault_ids(db, body.vault_ids, workspace_id=agent.workspace_id)

    session = await sessions_q.create_session(
        db,
        agent=agent,
        agent_version=agent_version.version,
        environment=environment,
        title=body.title,
        metadata=normalize_metadata(body.metadata),
        resources=[],
        vault_ids=vault_ids,
    )
    for resource_data in body.resources:
        await create_session_resource(db, session, resource_data, allowed_types={"file", "github_repository", "memory_store"})
    await _create_multiagent_session_threads(db, session, agent_version)
    await events_q.append_event(
        db,
        session,
        event_type="session.status_idle",
        payload={"type": "session.status_idle", "status": "idle", "stop_reason": {"type": "end_turn"}},
    )
    try:
        await provision_session_sandbox(
            db,
            session=session,
            agent_version=agent_version,
            environment_config=environment.config,
        )
    except SandboxLifecycleConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SandboxProviderError as exc:
        raise HTTPException(status_code=503, detail="Sandbox provider is unavailable") from exc
    await db.commit()
    return await _session_response(db, session)


@router.get("", response_model=ListResponse[SessionResponse])
async def list_sessions(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    order: str = "desc",
    agent_id: str | None = None,
    agent_version: int | None = None,
    memory_store_id: str | None = None,
    deployment_id: str | None = None,
    statuses: list[str] | None = Query(default=None),
    statuses_brackets: list[str] | None = Query(default=None, alias="statuses[]"),
    created_at_gt: datetime | None = Query(default=None, alias="created_at[gt]"),
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lt: datetime | None = Query(default=None, alias="created_at[lt]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    sessions = await sessions_q.list_sessions(db, limit=1000, include_archived=include_archived)
    sessions = filter_created_at(
        sessions,
        created_at_gt=created_at_gt,
        created_at_gte=created_at_gte,
        created_at_lt=created_at_lt,
        created_at_lte=created_at_lte,
    )
    requested_statuses = [*(statuses or []), *(statuses_brackets or [])]
    if requested_statuses:
        invalid_statuses = sorted(set(requested_statuses) - SESSION_LIST_STATUSES)
        if invalid_statuses:
            raise HTTPException(status_code=422, detail="statuses must be idle, rescheduling, running, or terminated")
        allowed_statuses = set(requested_statuses)
        sessions = [session for session in sessions if session.status in allowed_statuses]
    if agent_id is not None:
        sessions = [session for session in sessions if session.agent_id == agent_id]
        if agent_version is not None:
            sessions = [session for session in sessions if session.agent_version == agent_version]
    if memory_store_id is not None:
        filtered_sessions = []
        for session in sessions:
            if await session_has_memory_store(db, session, memory_store_id):
                filtered_sessions.append(session)
        sessions = filtered_sessions
    if deployment_id is not None:
        sessions = [
            session
            for session in sessions
            if str((session.metadata_ or {}).get("deployment_id") or "") == deployment_id
        ]
    sessions = sort_by_created_at(sessions, order=order)
    responses = []
    for session in sessions:
        responses.append(await _session_response(db, session))
    return paginate(responses, limit=limit, page=page)


@router.get("/{session_id}", response_model=SessionResponse)
async def retrieve_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    return await _session_response(db, session)


@router.post("/{session_id}", response_model=SessionResponse)
@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    body: SessionUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    if blocks_mutation(session.status):
        raise HTTPException(status_code=409, detail=f"Cannot update a {session.status} session")
    if body.vault_ids is not None:
        raise HTTPException(status_code=422, detail="Session vault_ids updates are reserved and not supported")

    status_details = None
    if body.agent is not None:
        if session.status != SESSION_IDLE or is_waiting_for_action(session.stop_reason):
            raise HTTPException(status_code=409, detail="Session agent config can only be updated while idle")
        status_details = await _merge_session_agent_overlay(db, session, body.agent)

    session = await sessions_q.update_session(
        db,
        session,
        title=body.title,
        metadata=merge_metadata(session.metadata_, body.metadata) if body.metadata is not None else None,
        status_details=status_details,
    )
    response = await _session_response(db, session)
    await events_q.append_event(
        db,
        session,
        event_type="session.updated",
        payload={"type": "session.updated", "session": response.model_dump(mode="json")},
    )
    await db.commit()
    return response


@router.post("/{session_id}/archive", response_model=SessionResponse)
async def archive_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    if blocks_mutation(session.status):
        raise HTTPException(status_code=409, detail=f"Cannot archive a {session.status} session")
    try:
        await pause_session_sandbox(
            workspace_id=session.workspace_id,
            session_id=session.id,
        )
    except SandboxProviderError as exc:
        raise HTTPException(status_code=503, detail="Sandbox provider is unavailable") from exc
    session = await sessions_q.archive_session(db, session)
    await db.commit()
    return await _session_response(db, session)


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    if blocks_mutation(session.status):
        raise HTTPException(status_code=409, detail=f"Cannot delete a {session.status} session")
    try:
        await delete_session_sandbox(
            workspace_id=session.workspace_id,
            session_id=session.id,
        )
    except SandboxProviderError as exc:
        raise HTTPException(status_code=503, detail="Sandbox provider is unavailable") from exc
    await sessions_q.delete_session(db, session)
    await events_q.append_event(
        db,
        session,
        event_type="session.deleted",
        payload={"type": "session.deleted"},
    )
    await db.commit()
    return {"id": session.id, "type": "session_deleted", "deleted": True}


@router.post("/{session_id}/cancel", response_model=SessionResponse)
async def cancel_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        await pause_session_sandbox(
            workspace_id=session.workspace_id,
            session_id=session.id,
        )
    except SandboxProviderError as exc:
        raise HTTPException(status_code=503, detail="Sandbox provider is unavailable") from exc
    stop_reason = {"type": "cancelled"}
    await sessions_q.update_session(db, session, status=SESSION_TERMINATED, stop_reason=stop_reason)
    await events_q.append_event(
        db,
        session,
        event_type="session.status_terminated",
        payload={"type": "session.status_terminated", "status": "terminated", "stop_reason": stop_reason},
    )
    await db.commit()
    return await _session_response(db, session)


@router.post("/{session_id}/resume", response_model=SessionResponse)
async def resume_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == SESSION_TERMINATED:
        raise HTTPException(status_code=409, detail="Terminated sessions cannot be resumed")
    if is_waiting_for_action(session.stop_reason):
        raise HTTPException(status_code=409, detail="Session requires action results before it can resume")
    if not can_start_work(session.status, session.stop_reason):
        raise HTTPException(status_code=409, detail=f"Cannot resume a {session.status} session")
    environment = await env_q.get_environment(db, session.environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    work = await enqueue_session_run(db, session, trigger="session.resume")
    await db.commit()
    if should_execute_inline(environment.config):
        background_tasks.add_task(execute_work_item, work.id)
    return await _session_response(db, session)


@router.post("/{session_id}/events", response_model=SendEventsResponse)
async def send_events(
    session_id: str,
    body: SendEventsRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Session is archived")
    if session.status == SESSION_TERMINATED:
        raise HTTPException(status_code=409, detail="Session is terminated")
    environment = await env_q.get_environment(db, session.environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")

    event_inputs = list(body.events)
    await _validate_event_batch(db, session, event_inputs)

    appended = []
    should_run = False
    should_interrupt = False
    resolved_required_actions = False
    for event_input in body.events:
        payload = event_input.model_dump(mode="json")
        event = await events_q.append_event(
            db,
            session,
            event_type=event_input.type,
            payload=payload,
        )
        appended.append(event_to_response(event))
        if event_input.type == "user.interrupt":
            should_interrupt = True
        elif starts_work(event_input.type):
            should_run = True
        elif is_action_result(event_input.type):
            resolved_required_actions = await _all_pending_actions_resolved(db, session)
            should_run = resolved_required_actions

    work = None
    if should_interrupt:
        stop_reason = {"type": "interrupted"}
        await sessions_q.update_session(db, session, status=SESSION_IDLE, stop_reason=stop_reason)
        await events_q.append_event(
            db,
            session,
            event_type="session.status_idle",
            payload={"type": "session.status_idle", "status": SESSION_IDLE, "stop_reason": stop_reason},
        )
    elif should_run:
        if resolved_required_actions:
            await sessions_q.update_session(db, session, status=SESSION_IDLE, stop_reason={"type": "action_submitted"})
        work = await enqueue_session_run(
            db,
            session,
            trigger="session.events",
            metadata={"event_ids": [event.id for event in appended]},
        )
    await db.commit()

    if work is not None and should_execute_inline(environment.config):
        background_tasks.add_task(execute_work_item, work.id)

    return SendEventsResponse(data=appended)


@router.get("/{session_id}/events", response_model=ListResponse[SessionEventResponse])
async def list_session_events(
    session_id: str,
    after_seq: int = 0,
    limit: int = 100,
    page: str | None = None,
    order: str = "asc",
    created_at_gt: datetime | None = Query(default=None, alias="created_at[gt]"),
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lt: datetime | None = Query(default=None, alias="created_at[lt]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    types: list[str] | None = Query(default=None),
    types_brackets: list[str] | None = Query(default=None, alias="types[]"),
    db: AsyncSession = Depends(get_session),
):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    events = await events_q.list_events(db, session_id=session.id, after_seq=after_seq, limit=1000)
    events = filter_created_at(
        events,
        created_at_gt=created_at_gt,
        created_at_gte=created_at_gte,
        created_at_lt=created_at_lt,
        created_at_lte=created_at_lte,
    )
    requested_types = [*(types or []), *(types_brackets or [])]
    if requested_types:
        allowed_types = set(requested_types)
        events = [event for event in events if event.type in allowed_types]
    order = normalize_sort_order(order, default="asc")
    if order == "desc":
        events = list(reversed(events))
    return paginate([event_to_response(e) for e in events], limit=limit, page=page)


@router.get("/{session_id}/events/stream")
async def stream_session_events(
    session_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    requested_deltas = _requested_event_deltas(event_deltas, event_deltas_brackets)
    return await _stream_response(
        session_id,
        request,
        _resume_after_seq(after_seq, last_event_id),
        event_deltas=requested_deltas,
    )


@router.get("/{session_id}/stream")
async def stream_session_events_alias(
    session_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    requested_deltas = _requested_event_deltas(event_deltas, event_deltas_brackets)
    return await _stream_response(
        session_id,
        request,
        _resume_after_seq(after_seq, last_event_id),
        event_deltas=requested_deltas,
    )


@router.post("/{session_id}/resources", status_code=201)
async def add_session_resource(
    session_id: str,
    body: GenericBody,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    await _ensure_session_inputs_mutable(db, session)
    data = body.model_dump(mode="json")
    resource = await create_session_resource(db, session, data, allowed_types={"file"})
    await db.commit()
    return session_resource_response(resource)


@router.get("/{session_id}/resources")
async def list_session_resources(
    session_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    await _must_get_session(db, session_id)
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session_id,
        limit=1000,
    )
    return paginate([session_resource_response(resource) for resource in resources], limit=limit, page=page)


@router.get("/{session_id}/resources/{resource_id}")
async def retrieve_session_resource(
    session_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_session),
):
    await _must_get_session(db, session_id)
    resource = await res_q.get_resource(
        db,
        resource_id=resource_id,
        resource_type="session_resource",
        parent_id=session_id,
    )
    if resource is None:
        raise HTTPException(status_code=404, detail="Session resource not found")
    return session_resource_response(resource)


@router.post("/{session_id}/resources/{resource_id}")
async def update_session_resource(
    session_id: str,
    resource_id: str,
    body: GenericBody,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    await _ensure_session_inputs_mutable(db, session)
    resource = await res_q.get_resource(
        db,
        resource_id=resource_id,
        resource_type="session_resource",
        parent_id=session_id,
    )
    if resource is None:
        raise HTTPException(status_code=404, detail="Session resource not found")
    data = rotate_session_resource_token(resource, body.model_dump(mode="json"))
    await res_q.update_resource(db, resource, data=data)
    await db.commit()
    return session_resource_response(resource)


@router.delete("/{session_id}/resources/{resource_id}")
async def delete_session_resource(
    session_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    await _ensure_session_inputs_mutable(db, session)
    resource = await res_q.get_resource(
        db,
        resource_id=resource_id,
        resource_type="session_resource",
        parent_id=session_id,
    )
    if resource is None:
        raise HTTPException(status_code=404, detail="Session resource not found")
    ensure_session_resource_deletable(resource)
    await delete_session_resource_file(db, resource)
    await res_q.delete_resource(db, resource)
    await db.commit()
    return {"id": resource.id, "type": "session_resource_deleted", "deleted": True}


@router.get("/{session_id}/threads")
async def list_session_threads(
    session_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    threads = await res_q.list_resources(
        db,
        resource_type="session_thread",
        parent_id=session_id,
        limit=1000,
    )
    responses = [await _session_thread_response(db, session, thread) for thread in threads]
    responses.insert(0, await _session_thread_response(db, session, None))
    return paginate(responses, limit=limit, page=page)


@router.get("/{session_id}/threads/{thread_id}")
async def retrieve_session_thread(
    session_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    if thread_id == _primary_thread_id(session):
        return await _session_thread_response(db, session, None)
    thread = await res_q.get_resource(
        db,
        resource_id=thread_id,
        resource_type="session_thread",
        parent_id=session_id,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Session thread not found")
    return await _session_thread_response(db, session, thread)


@router.post("/{session_id}/threads/{thread_id}/archive")
async def archive_session_thread(
    session_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    if thread_id == _primary_thread_id(session):
        details = dict(session.status_details or {})
        details["primary_thread_archived_at"] = datetime.now(timezone.utc).isoformat()
        await sessions_q.update_session(db, session, status_details=details)
        await db.commit()
        return await _session_thread_response(db, session, None)
    thread = await res_q.get_resource(
        db,
        resource_id=thread_id,
        resource_type="session_thread",
        parent_id=session_id,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Session thread not found")
    await res_q.archive_resource(db, thread)
    await db.commit()
    return await _session_thread_response(db, session, thread)


@router.get("/{session_id}/threads/{thread_id}/events")
async def list_session_thread_events(
    session_id: str,
    thread_id: str,
    after_seq: int = 0,
    limit: int = 100,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    session = await _must_get_session(db, session_id)
    await _must_get_session_thread(db, session, thread_id)
    primary_thread_id = _primary_thread_id(session)
    events = await events_q.list_events(db, session_id=session_id, after_seq=after_seq, limit=1000)
    filtered = [
        event_to_response(event)
        for event in events
        if _event_belongs_to_thread(event, thread_id, primary_thread_id)
    ]
    return paginate(filtered, limit=limit, page=page)


@router.get("/{session_id}/threads/{thread_id}/stream")
async def stream_session_thread_events(
    session_id: str,
    thread_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: AsyncSession = Depends(get_session),
):
    if _requested_event_deltas(event_deltas, event_deltas_brackets):
        raise HTTPException(status_code=400, detail="event_deltas is only supported on session event streams")
    session = await _must_get_session(db, session_id)
    await _must_get_session_thread(db, session, thread_id)
    return await _stream_response(
        session_id,
        request,
        _resume_after_seq(after_seq, last_event_id),
        thread_id=thread_id,
    )


async def _validate_event_batch(db: AsyncSession, session, event_inputs: list) -> None:
    event_types = [event.type for event in event_inputs]
    validate_system_message_batch(event_types)
    for event_input in event_inputs:
        if event_input.type == "user.define_outcome":
            validate_user_define_outcome_event(event_input.model_dump(mode="json"))
    if session.status in ACTIVE_STATUSES:
        if event_types == ["user.interrupt"]:
            return
        raise HTTPException(status_code=409, detail=f"Cannot send events while session is {session.status}")

    if is_waiting_for_action(session.stop_reason):
        if event_types == ["user.interrupt"]:
            return
        action_result_inputs = [event_input for event_input in event_inputs if event_input.type != "system.message"]
        if not action_result_inputs or not all(event_input.type in ACTION_RESULT_EVENTS for event_input in action_result_inputs):
            raise HTTPException(status_code=409, detail="Session is waiting for required action results")
        await _validate_action_results(db, session, action_result_inputs)
        return

    if any(event_type in ACTION_RESULT_EVENTS for event_type in event_types):
        raise HTTPException(status_code=409, detail="Session is not waiting for action results")

async def _validate_action_results(db: AsyncSession, session, event_inputs: list) -> None:
    pending_ids = pending_action_ids(session.stop_reason)
    history = await events_q.list_events(db, session_id=session.id, after_seq=0, limit=1000)
    events_by_id = {event.id: event for event in history}
    resolved_ids = _resolved_action_ids(history)
    batch_targets: set[str] = set()

    for event_input in event_inputs:
        payload = event_input.model_dump(mode="json")
        target_id = _action_result_target(event_input.type, payload)
        if not target_id:
            raise HTTPException(status_code=422, detail=f"{event_input.type} must reference a blocking event")
        if target_id not in pending_ids:
            raise HTTPException(status_code=409, detail=f"Action event is not pending: {target_id}")
        if target_id in resolved_ids or target_id in batch_targets:
            raise HTTPException(status_code=409, detail=f"Action event is already resolved: {target_id}")

        blocking_event = events_by_id.get(target_id)
        if blocking_event is None:
            raise HTTPException(status_code=404, detail=f"Blocking event not found: {target_id}")
        _validate_action_result_matches_blocker(event_input.type, payload, blocking_event.type)
        batch_targets.add(target_id)


def _validate_action_result_matches_blocker(result_type: str, payload: dict[str, Any], blocker_type: str) -> None:
    if result_type == "user.custom_tool_result":
        if blocker_type != "agent.custom_tool_use":
            raise HTTPException(status_code=409, detail="custom_tool_result must target agent.custom_tool_use")
        return

    if result_type == "user.tool_result":
        if blocker_type != "agent.tool_use":
            raise HTTPException(status_code=409, detail="tool_result must target agent.tool_use")
        return

    if result_type == "user.tool_confirmation":
        if blocker_type not in {"agent.tool_use", "agent.mcp_tool_use"}:
            raise HTTPException(status_code=409, detail="tool_confirmation must target agent tool use")
        result = payload.get("result")
        if result not in {"allow", "deny"}:
            raise HTTPException(status_code=422, detail='tool_confirmation.result must be "allow" or "deny"')
        if result != "deny" and payload.get("deny_message") is not None:
            raise HTTPException(status_code=422, detail="tool_confirmation.deny_message is only allowed when result is deny")


async def _all_pending_actions_resolved(db: AsyncSession, session) -> bool:
    pending_ids = pending_action_ids(session.stop_reason)
    if not pending_ids:
        return False
    history = await events_q.list_events(db, session_id=session.id, after_seq=0, limit=1000)
    return pending_ids.issubset(_resolved_action_ids(history))


def _resolved_action_ids(events) -> set[str]:
    resolved: set[str] = set()
    for event in events:
        target_id = _action_result_target(event.type, event.payload)
        if target_id:
            resolved.add(target_id)
    return resolved


def _action_result_target(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "user.custom_tool_result":
        value = payload.get("custom_tool_use_id")
    elif event_type in {"user.tool_confirmation", "user.tool_result"}:
        value = payload.get("tool_use_id")
    else:
        return None
    return str(value) if value else None


async def _merge_session_agent_overlay(db: AsyncSession, session, update: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(update, dict):
        raise HTTPException(status_code=422, detail="session agent update must be an object")
    unsupported = set(update) - {"tools", "mcp_servers"}
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise HTTPException(status_code=422, detail=f"Unsupported session agent update fields: {names}")

    version = await agents_q.get_agent_version(db, agent_id=session.agent_id, version=session.agent_version)
    if version is None:
        raise HTTPException(status_code=404, detail="Agent version not found")

    details = dict(session.status_details or {})
    overlay = dict(details.get("agent") or {})
    tools = overlay.get("tools", version.tools)
    mcp_servers = overlay.get("mcp_servers", version.mcp_servers)

    if "tools" in update:
        tools = update["tools"] or []
        if not isinstance(tools, list):
            raise HTTPException(status_code=422, detail="agent.tools must be an array or null")
        tools = normalize_agent_tools(tools)
        overlay["tools"] = tools
    if "mcp_servers" in update:
        mcp_servers = update["mcp_servers"] or []
        if not isinstance(mcp_servers, list):
            raise HTTPException(status_code=422, detail="agent.mcp_servers must be an array or null")
        overlay["mcp_servers"] = mcp_servers

    validate_mcp_bindings(mcp_servers, tools)
    details["agent"] = overlay
    return details


async def _session_response(db: AsyncSession, session) -> SessionResponse:
    version = await agents_q.get_agent_version(
        db,
        agent_id=session.agent_id,
        version=session.agent_version,
        workspace_id=session.workspace_id,
    )
    agent = _session_agent_snapshot(version, session.status_details or {}) if version is not None else None
    resources = await session_resources_response(db, session)
    return session_to_response(session, agent=agent, resources=resources)


def _session_agent_snapshot(version, details: dict[str, Any]) -> dict[str, Any]:
    overlay = dict(details.get("agent") or {})
    return {
        "id": version.agent_id,
        "type": "agent",
        "name": version.name,
        "version": version.version,
        "model": version.model,
        "system": version.system,
        "description": version.description,
        "tools": overlay.get("tools", version.tools),
        "mcp_servers": overlay.get("mcp_servers", version.mcp_servers),
        "skills": version.skills,
        "multiagent": version.multiagent,
    }


async def _validate_session_vault_ids(
    db: AsyncSession,
    vault_ids: list[str],
    *,
    workspace_id: str,
) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_id in vault_ids:
        vault_id = str(raw_id or "")
        if not vault_id:
            raise HTTPException(status_code=422, detail="vault_ids must not contain empty values")
        if vault_id in seen:
            continue
        vault = await res_q.get_resource(db, resource_id=vault_id, resource_type="vault", workspace_id=workspace_id)
        if vault is None or vault.archived_at is not None:
            raise HTTPException(status_code=404, detail="Vault not found")
        resolved.append(vault_id)
        seen.add(vault_id)
    return resolved


async def _session_thread_response(db: AsyncSession, session, thread, *, archived: bool = False) -> dict[str, Any]:
    version = await agents_q.get_agent_version(
        db,
        agent_id=session.agent_id,
        version=session.agent_version,
        workspace_id=session.workspace_id,
    )
    data = dict(getattr(thread, "data", None) or {})
    created_at = getattr(thread, "created_at", session.created_at)
    updated_at = getattr(thread, "updated_at", session.updated_at)
    archived_at = getattr(thread, "archived_at", None)
    if thread is None:
        archived_at = (session.status_details or {}).get("primary_thread_archived_at") or archived_at
    if archived and archived_at is None:
        archived_at = datetime.now(timezone.utc)
    status = data.get("status") or session.status
    if status not in {"running", "idle", "rescheduling", "terminated"}:
        status = "idle"
    return {
        "id": getattr(thread, "id", _primary_thread_id(session)),
        "type": "session_thread",
        "session_id": session.id,
        "agent": data.get("agent") or (_session_thread_agent_snapshot(version) if version is not None else None),
        "status": status,
        "parent_thread_id": data.get("parent_thread_id"),
        "stats": data.get("stats") or {},
        "usage": data.get("usage") or {},
        "archived_at": archived_at,
        "created_at": created_at,
        "updated_at": updated_at,
    }


async def _must_get_session_thread(db: AsyncSession, session, thread_id: str):
    if thread_id == _primary_thread_id(session):
        return None
    thread = await res_q.get_resource(
        db,
        resource_id=thread_id,
        resource_type="session_thread",
        parent_id=session.id,
        workspace_id=session.workspace_id,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Session thread not found")
    return thread


async def _create_multiagent_session_threads(db: AsyncSession, session, version) -> None:
    multiagent = version.multiagent or {}
    if not isinstance(multiagent, dict):
        return
    roster = multiagent.get("agents") or []
    if not isinstance(roster, list):
        return
    primary_thread_id = _primary_thread_id(session)
    for entry in roster:
        if not isinstance(entry, dict) or entry.get("type") != "agent":
            continue
        agent_id = entry.get("id")
        agent_version = entry.get("version")
        if not agent_id or agent_version is None:
            continue
        referenced_version = await agents_q.get_agent_version(
            db,
            agent_id=str(agent_id),
            version=int(agent_version),
            workspace_id=session.workspace_id,
        )
        if referenced_version is None:
            raise HTTPException(status_code=422, detail=f"Referenced multiagent version not found: {agent_id}@{agent_version}")
        await res_q.create_resource(
            db,
            resource_type="session_thread",
            parent_id=session.id,
            name=f"agent:{agent_id}:{agent_version}",
            status="idle",
            data={
                "status": "idle",
                "parent_thread_id": primary_thread_id,
                "agent": _session_thread_agent_snapshot(referenced_version),
                "multiagent": {
                    "type": "delegated_agent",
                    "coordinator_agent_id": session.agent_id,
                    "coordinator_agent_version": session.agent_version,
                },
            },
            workspace_id=session.workspace_id,
        )


def _session_thread_agent_snapshot(version) -> dict[str, Any]:
    return {
        "id": version.agent_id,
        "type": "agent",
        "name": version.name,
        "version": version.version,
        "model": version.model,
        "system": version.system,
        "description": version.description,
        "tools": version.tools,
        "mcp_servers": version.mcp_servers,
        "skills": version.skills,
    }


def _primary_thread_id(session) -> str:
    return f"thrd_{session.id}_primary"


def _event_belongs_to_thread(event, thread_id: str, primary_thread_id: str) -> bool:
    event_thread_id = _event_thread_id(event)
    if thread_id == primary_thread_id:
        return event_thread_id in {None, "", primary_thread_id}
    return event_thread_id == thread_id


def _event_thread_id(event) -> str | None:
    payload = event.payload or {}
    value = payload.get("thread_id")
    if value:
        return str(value)
    thread = payload.get("thread")
    if isinstance(thread, dict) and thread.get("id"):
        return str(thread["id"])
    return None


def _resolve_agent_ref(agent: str | AgentReference) -> tuple[str, int | None]:
    if isinstance(agent, str):
        return agent, None
    return agent.id, agent.version


async def _must_get_session(db: AsyncSession, session_id: str):
    session = await sessions_q.get_session(db, session_id)
    if session is None or session.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _ensure_session_inputs_mutable(db: AsyncSession, session) -> None:
    if await session_has_managed_sandbox(
        db,
        workspace_id=session.workspace_id,
        session_id=session.id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Session inputs are sealed; create a new Session to change resources",
        )


async def _stream_response(
    session_id: str,
    request: Request,
    after_seq: int,
    thread_id: str | None = None,
    event_deltas: frozenset[str] = frozenset(),
) -> StreamingResponse:
    async def event_generator():
        current_seq = after_seq
        interval = max(float(get_settings().vma_event_poll_interval_seconds), 0.05)
        workspace = getattr(request.state, "current_workspace", None)
        workspace_id = getattr(workspace, "id", None)
        preview_event_types: dict[str, str] = {}
        durable_preview_event_ids: set[str] = set()

        async with AsyncExitStack() as stack:
            preview_queue = None
            if event_deltas:
                preview_queue = await stack.enter_async_context(
                    vma_preview_bus.subscribe(session_id, workspace_id=workspace_id)
                )

            while True:
                if await request.is_disconnected():
                    break
                async with session_scope() as db:
                    session = await sessions_q.get_session(db, session_id, workspace_id=workspace_id)
                    if session is None or session.deleted_at is not None:
                        yield "event: error\ndata: {\"type\":\"not_found_error\",\"message\":\"Session not found\"}\n\n"
                        break
                    events = await events_q.list_events(
                        db,
                        session_id=session_id,
                        after_seq=current_seq,
                        limit=100,
                        workspace_id=workspace_id,
                    )

                if events:
                    for event in events:
                        # The durable cursor advances even when a thread filter hides
                        # an event, otherwise an unrelated event is fetched forever.
                        current_seq = max(current_seq, event.seq)
                        _reconcile_preview_event(preview_event_types, durable_preview_event_ids, event)
                        if thread_id is not None and not _event_belongs_to_thread(
                            event,
                            thread_id,
                            _primary_thread_id(session),
                        ):
                            continue
                        yield event_to_sse(event)
                    continue

                if preview_queue is None:
                    yield ": ping\n\n"
                    await asyncio.sleep(interval)
                    continue

                try:
                    frame = await asyncio.wait_for(preview_queue.get(), timeout=interval)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue

                if _preview_frame_requested(
                    frame,
                    event_deltas,
                    preview_event_types,
                    durable_preview_event_ids,
                ):
                    yield _preview_frame_to_sse(frame)

                # Preserve the runtime's frame order and drain its current burst
                # before polling for the authoritative buffered database event.
                while True:
                    try:
                        frame = preview_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if _preview_frame_requested(
                        frame,
                        event_deltas,
                        preview_event_types,
                        durable_preview_event_ids,
                    ):
                        yield _preview_frame_to_sse(frame)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _requested_event_deltas(
    event_deltas: list[str] | None,
    event_deltas_brackets: list[str] | None,
) -> frozenset[str]:
    requested = frozenset([*(event_deltas or []), *(event_deltas_brackets or [])])
    invalid = sorted(requested - VMA_PREVIEWABLE_EVENT_TYPES)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail="event_deltas must contain only agent.message or agent.thinking",
        )
    return requested


def _resume_after_seq(after_seq: int, last_event_id: str | None) -> int:
    if last_event_id is None:
        return after_seq
    try:
        header_seq = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an event sequence number") from exc
    if header_seq < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an event sequence number")
    return max(after_seq, header_seq)


def _preview_frame_requested(
    frame: dict[str, Any],
    requested: frozenset[str],
    preview_event_types: dict[str, str],
    durable_preview_event_ids: set[str],
) -> bool:
    if frame.get("type") == "event_start":
        event = frame.get("event")
        if not isinstance(event, dict):
            return False
        event_id = event.get("id")
        event_type = event.get("type")
        if not isinstance(event_id, str) or not isinstance(event_type, str):
            return False
        if event_id in durable_preview_event_ids:
            return False
        if event_type not in requested:
            return False
        preview_event_types[event_id] = event_type
        return True

    if frame.get("type") == "event_delta":
        event_id = frame.get("event_id")
        return isinstance(event_id, str) and preview_event_types.get(event_id) in requested
    return False


def _reconcile_preview_event(
    preview_event_types: dict[str, str],
    durable_preview_event_ids: set[str],
    event,
) -> None:
    preview_event_types.pop(event.id, None)
    if event.type in VMA_PREVIEWABLE_EVENT_TYPES:
        durable_preview_event_ids.add(event.id)
    if event.type == "span.model_request_end":
        preview_event_types.clear()


def _preview_frame_to_sse(frame: dict[str, Any]) -> str:
    return (
        f"event: {frame['type']}\n"
        f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
    )
