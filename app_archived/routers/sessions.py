import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_contract import (
    normalize_agent_tools,
    validate_mcp_bindings,
)
from app.auth import require_api_access
from app.config import get_settings
from app.db.engine import get_session, session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as env_q
from app.db.queries import event_idempotency as idempotency_q
from app.db.queries import events as events_q
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.event_validation import validate_system_message_batch, validate_user_define_outcome_event
from app.governance import (
    TenantIdempotencyClaim,
    claim_tenant_idempotency,
    complete_tenant_idempotency,
)
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
    AgentWithOverrides,
    SessionCreateRequest,
    SessionDeletedResponse,
    SessionFileResourceCreateRequest,
    SessionResourceDeletedResponse,
    SessionResourceResponse,
    SessionResourceTokenRotateRequest,
    SessionResponse,
    SessionUpdateRequest,
    session_to_response,
)
from app.pagination import filter_created_at, normalize_sort_order, paginate, sort_by_created_at
from app.runtime.agent_resolution import effective_agent_version, resolve_session_agent_config
from app.runtime.dispatch import dispatch_work
from app.runtime.sandbox_lifecycle import (
    SandboxInputMismatchError,
    SandboxLifecycleConfigurationError,
    SandboxLifecycleStateError,
    append_session_file_to_sandbox,
    build_appended_session_input_descriptor,
    build_session_input_descriptor_for_append,
    delete_session_sandbox,
    lock_session_sandbox_for_file_append,
    pause_session_sandbox,
    provision_session_sandbox,
    session_has_managed_sandbox,
)
from app.runtime.sandbox_providers import SandboxProviderError
from app.runtime.work_queue import (
    enqueue_session_run,
    execute_work_item,
    get_active_session_work,
    should_execute_inline,
    stop_work,
)
from app.runtime.vma_preview_bus import vma_preview_bus
from app.session_resources import (
    create_session_resource,
    delete_session_resource_file,
    ensure_session_resource_deletable,
    find_existing_file_session_resource,
    rotate_session_resource_token,
    session_has_memory_store,
    session_resource_response,
    session_resources_response,
    validate_e2b_file_mount_path,
    validate_user_message_file_references,
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
from app.organization import resolve_organization_id
from app.schema import events as event_schema
from app.schema import session as session_schema

SESSION_LIST_STATUSES = {SESSION_IDLE, SESSION_RUNNING, SESSION_RESCHEDULING, SESSION_TERMINATED}
VMA_PREVIEWABLE_EVENT_TYPES = frozenset({"agent.message", "agent.thinking"})
IDEMPOTENCY_KEY_MAX_BYTES = 255

def _validate_idempotency_key(idempotency_key: str) -> None:
    encoded_key = idempotency_key.encode("utf-8")
    if (
        not encoded_key
        or len(encoded_key) > IDEMPOTENCY_KEY_MAX_BYTES
        or idempotency_key.strip() != idempotency_key
        or any(ord(character) < 32 or ord(character) == 127 for character in idempotency_key)
    ):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key must be 1-255 bytes without surrounding whitespace or control characters",
        )


def _idempotency_error(claim: TenantIdempotencyClaim) -> HTTPException:
    if claim.disposition == "conflict":
        return HTTPException(
            status_code=422,
            detail="Idempotency-Key was already used with a different request",
        )
    return HTTPException(
        status_code=409,
        detail="A request with this Idempotency-Key is still in progress",
    )


router = APIRouter(
    prefix="/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Optional tenant-scoped identity for safely retrying Session creation. "
            "The native SDK supplies one automatically."
        ),
    ),
    db: AsyncSession = Depends(get_session),
):
    idempotency_claim: TenantIdempotencyClaim | None = None
    if idempotency_key is not None:
        _validate_idempotency_key(idempotency_key)
        idempotency_claim = await claim_tenant_idempotency(
            db,
            organization_id=resolve_organization_id(),
            operation="sessions.create",
            idempotency_key=idempotency_key,
            request_payload=body.model_dump(mode="json", exclude_none=False),
        )
        if idempotency_claim.disposition == "replay":
            return SessionResponse.model_validate(idempotency_claim.response_body)
        if not idempotency_claim.acquired:
            raise _idempotency_error(idempotency_claim)

    agent_id, version, overrides = _resolve_agent_ref(body.agent)
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None or agent.archived_at is not None:
        raise HTTPException(status_code=404, detail="Agent not found")
    pinned_version = version or agent.active_version
    agent_version = await agents_q.get_agent_version(db, agent_id=agent.id, version=pinned_version)
    if agent_version is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    agent_config = await resolve_session_agent_config(
        db,
        agent_version,
        overrides,
        organization_id=agent.organization_id,
    )

    environment = await env_q.get_environment(db, body.environment_id)
    if environment is None or environment.deleted_at is not None or environment.archived_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")

    # TODO: reuse the vault_ids validation logic (existence + archived checks).
    # Skipped for now -- not implemented in this pass.
    vault_ids = list(body.vault_ids)

    if len(body.initial_events) > 50:
        raise HTTPException(status_code=422, detail="initial_events accepts at most 50 events")

    # Funding selection is cut entirely for now: sessions_q.create_session
    # resolves its own model-credential binding from the Organization's
    # default policy when funding_type is omitted.
    session = await sessions_q.create_session(
        db,
        agent=agent,
        agent_version=agent_version.version,
        environment=environment,
        title=body.title,
        metadata=normalize_metadata(body.metadata),
        resources=[],
        vault_ids=vault_ids,
        agent_config=agent_config,
    )
    for resource_data in body.resources:
        await create_session_resource(db, session, resource_data, allowed_types={"file", "github_repository", "memory_store"})
    await _create_multiagent_session_threads(db, session, agent_version)

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

    # Only after the sandbox exists do we decide whether to start work --
    # enqueueing a run before the sandbox is ready would race the worker.
    work = None
    if body.initial_events:
        appended_ids = []
        for event_input in body.initial_events:
            payload = event_input.model_dump(mode="json")
            event = await events_q.append_event(db, session, event_type=event_input.type, payload=payload)
            appended_ids.append(event.id)
        session = await sessions_q.update_session(db, session, status=session_schema.RUNNING)
        work = await enqueue_session_run(
            db,
            session,
            trigger="session.create",
            metadata={"event_ids": appended_ids},
        )
    else:
        await events_q.append_event(
            db,
            session,
            event_type=event_schema.SESSION_STATUS_IDLE,
            payload={
                "type": event_schema.SESSION_STATUS_IDLE,
                "status": session_schema.IDLE,
                "stop_reason": {"type": session_schema.STOP_END_TURN},
            },
        )

    response = await _session_response(db, session)
    if idempotency_claim is not None:
        await complete_tenant_idempotency(
            db,
            idempotency_claim,
            response_status=201,
            response_body=response.model_dump(mode="json"),
        )
    await db.commit()

    if work is not None:
        if get_settings().vma_work_dispatch_mode == "hybrid":
            background_tasks.add_task(dispatch_work, work.id, attempt=0)
        elif should_execute_inline(environment.config):
            background_tasks.add_task(execute_work_item, work.id)

    return response


def _resolve_agent_ref(
    agent: str | AgentReference | AgentWithOverrides,
) -> tuple[str, int | None, AgentWithOverrides | None]:
    if isinstance(agent, str):
        return agent, None, None
    if isinstance(agent, AgentWithOverrides):
        return agent.id, agent.version, agent
    return agent.id, agent.version, None


async def _create_multiagent_session_threads(db: AsyncSession, session, version) -> None:
    multiagent = version.multiagent or {}
    if not isinstance(multiagent, dict):
        return
    roster = multiagent.get("agents") or []
    if not isinstance(roster, list):
        return
    primary_thread_id = f"thrd_{session.id}_primary"
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
            organization_id=session.organization_id,
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
            organization_id=session.organization_id,
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


async def _session_response(db: AsyncSession, session) -> SessionResponse:
    version = await agents_q.get_agent_version(
        db,
        agent_id=session.agent_id,
        version=session.agent_version,
        organization_id=session.organization_id,
    )
    agent = _session_agent_snapshot(version, session.status_details or {}) if version is not None else None
    resources = await session_resources_response(db, session)
    return session_to_response(session, agent=agent, resources=resources)


def _session_agent_snapshot(version, details: dict[str, Any]) -> dict[str, Any]:
    effective = effective_agent_version(version, details)
    return {
        "id": version.agent_id,
        "type": "agent",
        "name": version.name,
        "version": version.version,
        "model": effective.model,
        "system": effective.system,
        "description": version.description,
        "tools": effective.tools,
        "mcp_servers": effective.mcp_servers,
        "skills": effective.skills,
        "multiagent": version.multiagent,
    }


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
    ...


@router.get("/{session_id}", response_model=SessionResponse)
async def retrieve_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}", response_model=SessionResponse)
@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    body: SessionUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}/archive", response_model=SessionResponse)
async def archive_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.delete("/{session_id}", response_model=SessionDeletedResponse)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}/cancel", response_model=SessionResponse)
async def cancel_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}/resume", response_model=SessionResponse)
async def resume_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}/events", response_model=SendEventsResponse)
async def send_events(
    session_id: str,
    body: SendEventsRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Optional identity for this event batch. Reusing the key with the same request "
            "replays the original successful response without appending events or work."
        ),
    ),
    db: AsyncSession = Depends(get_session),
):
    ...


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
    ...


@router.get("/{session_id}/events/stream")
async def stream_session_events(
    session_id: str,
    request: Request,
    after_seq: int | None = Query(default=None, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    ...


@router.get("/{session_id}/stream")
async def stream_session_events_alias(
    session_id: str,
    request: Request,
    after_seq: int | None = Query(default=None, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    ...


@router.post(
    "/{session_id}/resources",
    response_model=SessionResourceResponse,
    status_code=201,
)
async def add_session_resource(
    session_id: str,
    body: SessionFileResourceCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get(
    "/{session_id}/resources",
    response_model=ListResponse[SessionResourceResponse],
)
async def list_session_resources(
    session_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get(
    "/{session_id}/resources/{resource_id}",
    response_model=SessionResourceResponse,
)
async def retrieve_session_resource(
    session_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post(
    "/{session_id}/resources/{resource_id}",
    response_model=SessionResourceResponse,
)
async def update_session_resource(
    session_id: str,
    resource_id: str,
    body: SessionResourceTokenRotateRequest,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.delete(
    "/{session_id}/resources/{resource_id}",
    response_model=SessionResourceDeletedResponse,
)
async def delete_session_resource(
    session_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get("/{session_id}/threads")
async def list_session_threads(
    session_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get("/{session_id}/threads/{thread_id}")
async def retrieve_session_thread(
    session_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.post("/{session_id}/threads/{thread_id}/archive")
async def archive_session_thread(
    session_id: str,
    thread_id: str,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get("/{session_id}/threads/{thread_id}/events")
async def list_session_thread_events(
    session_id: str,
    thread_id: str,
    after_seq: int = 0,
    limit: int = 100,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    ...


@router.get("/{session_id}/threads/{thread_id}/stream")
async def stream_session_thread_events(
    session_id: str,
    thread_id: str,
    request: Request,
    after_seq: int | None = Query(default=None, ge=0),
    event_deltas: list[str] | None = Query(default=None),
    event_deltas_brackets: list[str] | None = Query(default=None, alias="event_deltas[]"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: AsyncSession = Depends(get_session),
):
    ...
