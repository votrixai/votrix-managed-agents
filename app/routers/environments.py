import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_access
from app.config import get_settings
from app.db.engine import get_session
from app.db.queries import environments as env_q
from app.db.queries import resources as res_q
from app.db.queries.api_keys import WORKER_SCOPE
from app.metadata import merge_metadata, normalize_metadata
from app.models.common import ListResponse
from app.models.environments import (
    EnvironmentCreateRequest,
    EnvironmentDeletedResponse,
    EnvironmentResponse,
    EnvironmentUpdateRequest,
    environment_config_with_scope,
    environment_scope,
    environment_to_response,
)
from app.models.resources import GenericBody
from app.pagination import paginate, sort_by_created_at
from app.runtime.work_queue import WorkLeaseError, ack_work, heartbeat_work, lease_next_work, stop_work

router = APIRouter(
    prefix="/v1/environments",
    tags=["environments"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=EnvironmentResponse, status_code=201)
async def create_environment(
    body: EnvironmentCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    name = _normalize_environment_name(body.name)
    try:
        config = environment_config_with_scope(body.config, body.scope)
        _validate_sandbox_policy(config)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    environment = await env_q.create_environment(
        db,
        name=name,
        config=config,
        description=body.description,
        metadata=normalize_metadata(body.metadata),
    )
    await db.commit()
    return environment_to_response(environment)


@router.get("", response_model=ListResponse[EnvironmentResponse])
async def list_environments(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_session),
):
    environments = await env_q.list_environments(db, limit=1000, include_archived=include_archived)
    environments = sort_by_created_at(environments, order="desc")
    return paginate([environment_to_response(env) for env in environments], limit=limit, page=page)


@router.get("/{environment_id}", response_model=EnvironmentResponse)
async def retrieve_environment(
    environment_id: str,
    db: AsyncSession = Depends(get_session),
):
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment_to_response(environment)


@router.post("/{environment_id}", response_model=EnvironmentResponse)
@router.patch("/{environment_id}", response_model=EnvironmentResponse)
async def update_environment(
    environment_id: str,
    body: EnvironmentUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    name = _normalize_environment_name(body.name) if body.name is not None else None
    config = body.config
    try:
        if body.scope is not None:
            config = environment_config_with_scope(config or environment.config, body.scope)
        elif config is not None:
            config = environment_config_with_scope(config, environment_scope(environment.config))
        if config is not None:
            _validate_sandbox_policy(config)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    environment = await env_q.update_environment(
        db,
        environment,
        name=name,
        config=config,
        description=body.description,
        metadata=merge_metadata(environment.metadata_, body.metadata) if body.metadata is not None else None,
    )
    await db.commit()
    return environment_to_response(environment)


def _normalize_environment_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="environment name must be a non-empty string")
    return value.strip()


def _validate_sandbox_policy(config: dict[str, Any]) -> None:
    from app.runtime.sandbox_lifecycle import (
        E2B_PROVIDER,
        sandbox_policy_from_environment,
        selected_sandbox_provider,
    )

    if selected_sandbox_provider(config) == E2B_PROVIDER:
        sandbox_policy_from_environment(config)


@router.post("/{environment_id}/archive", response_model=EnvironmentResponse)
async def archive_environment(
    environment_id: str,
    db: AsyncSession = Depends(get_session),
):
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    environment = await env_q.archive_environment(db, environment)
    await db.commit()
    return environment_to_response(environment)


@router.delete("/{environment_id}", response_model=EnvironmentDeletedResponse)
async def delete_environment(
    environment_id: str,
    db: AsyncSession = Depends(get_session),
):
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    await env_q.delete_environment(db, environment)
    await db.commit()
    return {"id": environment.id, "type": "environment_deleted", "deleted": True}


async def require_worker_access(
    request: Request,
    x_worker_token: str | None = Header(default=None, alias="x-worker-token"),
) -> None:
    workspace = getattr(request.state, "current_workspace", None)
    if workspace is None or WORKER_SCOPE not in workspace.scopes:
        raise HTTPException(status_code=403, detail="API key is missing required scope: worker")

    # Database API keys carry a tenant-bound worker scope and are sufficient on
    # their own. Keep the legacy process-wide token only for local/env-key
    # deployments while they migrate to scoped keys.
    if workspace.source == "database_api_key":
        return None
    expected = get_settings().vma_worker_token
    if not expected:
        return None
    if not x_worker_token or not secrets.compare_digest(x_worker_token, expected):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return None


@router.get("/{environment_id}/work")
async def list_environment_work(
    environment_id: str,
    limit: int = 50,
    page: str | None = None,
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    work = await res_q.list_resources(
        db,
        resource_type="environment_work",
        parent_id=environment_id,
        limit=1000,
    )
    return paginate([_work_response(item) for item in work], limit=limit, page=page)


@router.get("/{environment_id}/work/poll")
async def poll_environment_work(
    environment_id: str,
    worker_id: str = Query(default="anonymous"),
    lease_seconds: int = Query(default=60, ge=5, le=3600),
    block_ms: int | None = Query(default=None, ge=1, le=999),
    reclaim_older_than_ms: int | None = Query(default=None, ge=1),
    anthropic_worker_id: str | None = Header(default=None, alias="Anthropic-Worker-ID"),
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    worker = anthropic_worker_id or worker_id or "anonymous"
    work = await _poll_next_work(
        db,
        environment_id=environment_id,
        worker_id=worker,
        lease_seconds=lease_seconds,
        block_ms=block_ms,
        reclaim_older_than_ms=reclaim_older_than_ms,
    )
    if work is None:
        return None
    await db.commit()
    return _work_response(work)


@router.get("/{environment_id}/work/stats")
async def environment_work_stats(
    environment_id: str,
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    queued = await res_q.list_resources(
        db,
        resource_type="environment_work",
        parent_id=environment_id,
        limit=1000,
    )
    counters = {
        "environment_id": environment_id,
        "queued": len([w for w in queued if w.status == "queued"]),
        "leased": len([w for w in queued if w.status == "leased"]),
        "running": len([w for w in queued if w.status == "running"]),
        "rescheduling": len([w for w in queued if w.status == "rescheduling"]),
        "completed": len([w for w in queued if w.status == "completed"]),
        "error": len([w for w in queued if w.status == "error"]),
        "stopped": len([w for w in queued if w.status == "stopped"]),
    }
    return {
        "type": "work_queue_stats",
        "depth": counters["queued"] + counters["rescheduling"],
        "pending": counters["leased"] + counters["running"],
        "oldest_queued_at": _oldest_queued_at(queued),
        "workers_polling": None,
        **counters,
        "legacy_type": "self_hosted_work_queue_stats",
    }


@router.get("/{environment_id}/work/{work_id}")
async def retrieve_environment_work(
    environment_id: str,
    work_id: str,
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    work = await _must_get_work(db, environment_id, work_id)
    return _work_response(work)


@router.post("/{environment_id}/work/{work_id}")
async def update_environment_work(
    environment_id: str,
    work_id: str,
    body: GenericBody,
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    work = await _must_get_work(db, environment_id, work_id)
    data = dict(work.data)
    patch = body.model_dump(mode="json")
    if set(patch) != {"metadata"}:
        raise HTTPException(status_code=422, detail="work updates only support metadata")
    if not isinstance(patch["metadata"], dict):
        raise HTTPException(status_code=422, detail="work metadata must be an object")
    data["metadata"] = merge_metadata(_string_metadata(data.get("metadata") or {}), patch["metadata"])
    await res_q.update_resource(db, work, data=data, status=work.status)
    await db.commit()
    return _work_response(work)


@router.post("/{environment_id}/work/{work_id}/ack")
async def ack_environment_work(
    environment_id: str,
    work_id: str,
    worker_id: str | None = Query(default=None),
    lease_id: str | None = Query(default=None),
    anthropic_worker_id: str | None = Header(default=None, alias="Anthropic-Worker-ID"),
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    # The generated Anthropic SDK ack method cannot carry our lease_id extension.
    # Lock the current row and let ack_work validate the worker owner; callers that
    # do supply a lease_id still receive generation fencing for stale leases.
    work = await _must_get_work(db, environment_id, work_id, for_update=True)
    try:
        await ack_work(
            db,
            work,
            worker_id=_worker_id_for_work(work, worker_id, anthropic_worker_id),
            lease_id=lease_id,
        )
    except WorkLeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return _work_response(work)


@router.post("/{environment_id}/work/{work_id}/heartbeat")
async def heartbeat_environment_work(
    environment_id: str,
    work_id: str,
    body: GenericBody | None = None,
    worker_id: str | None = Query(default=None),
    lease_id: str | None = Query(default=None),
    lease_seconds: int = Query(default=60, ge=5, le=3600),
    desired_ttl_seconds: int | None = Query(default=None, ge=1, le=3600),
    expected_last_heartbeat: str | None = None,
    anthropic_worker_id: str | None = Header(default=None, alias="Anthropic-Worker-ID"),
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    work = await _must_get_work(db, environment_id, work_id, for_update=True)
    _check_heartbeat_precondition(work, expected_last_heartbeat)
    ttl_seconds = desired_ttl_seconds or lease_seconds
    try:
        await heartbeat_work(
            db,
            work,
            worker_id=_worker_id_for_work(work, worker_id, anthropic_worker_id),
            lease_id=lease_id,
            lease_seconds=ttl_seconds,
            payload=body.model_dump(mode="json") if body is not None else {},
        )
    except WorkLeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    data = work.data or {}
    return {
        "type": "work_heartbeat",
        "last_heartbeat": data.get("last_heartbeat_at"),
        "lease_extended": True,
        "state": _work_state(work.status),
        "ttl_seconds": ttl_seconds,
        "payload": data.get("last_heartbeat") or {},
        "work": _work_response(work),
    }


@router.post("/{environment_id}/work/{work_id}/stop")
async def stop_environment_work(
    environment_id: str,
    work_id: str,
    body: GenericBody | None = None,
    _worker: None = Depends(require_worker_access),
    db: AsyncSession = Depends(get_session),
):
    await _must_get_environment(db, environment_id)
    work = await _must_get_work(db, environment_id, work_id)
    await stop_work(db, work, payload=body.model_dump(mode="json") if body is not None else {})
    await db.commit()
    return _work_response(work)


async def _poll_next_work(
    db: AsyncSession,
    *,
    environment_id: str,
    worker_id: str,
    lease_seconds: int,
    block_ms: int | None,
    reclaim_older_than_ms: int | None,
):
    deadline = None
    if block_ms is not None:
        deadline = datetime.now(timezone.utc) + timedelta(milliseconds=block_ms)

    while True:
        if reclaim_older_than_ms is not None:
            await _expire_reclaimable_work(
                db,
                environment_id=environment_id,
                reclaim_older_than_ms=reclaim_older_than_ms,
            )
        work = await lease_next_work(
            db,
            environment_id=environment_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if work is not None or deadline is None:
            return work
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.05, remaining))


async def _expire_reclaimable_work(
    db: AsyncSession,
    *,
    environment_id: str,
    reclaim_older_than_ms: int,
) -> None:
    threshold = datetime.now(timezone.utc) - timedelta(milliseconds=reclaim_older_than_ms)
    work_items = await res_q.list_resources(
        db,
        resource_type="environment_work",
        parent_id=environment_id,
        limit=1000,
    )
    for work in work_items:
        if work.status not in {"leased", "running"}:
            continue
        data = dict(work.data or {})
        lease = dict(data.get("lease") or {})
        reference = _parse_datetime(data.get("last_heartbeat_at") or lease.get("leased_at"))
        if reference is None or reference > threshold:
            continue
        lease["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        data["lease"] = lease
        data["reclaim_older_than_ms"] = reclaim_older_than_ms
        await res_q.update_resource(db, work, data=data)


def _work_response(work) -> dict:
    data = dict(work.data or {})
    response = {
        "id": work.id,
        "type": "work",
        "environment_id": work.parent_id,
        "created_at": work.created_at,
        "state": _work_state(work.status),
        "data": {
            "type": "session",
            "id": str(data.get("session_id") or ""),
        },
        "metadata": _string_metadata(data.get("metadata") or {}),
        "acknowledged_at": data.get("acked_at"),
        "started_at": data.get("started_at"),
        "latest_heartbeat_at": data.get("last_heartbeat_at"),
        "stop_requested_at": data.get("stop_requested_at") or data.get("stopped_at"),
        "stopped_at": data.get("stopped_at") or (data.get("finished_at") if work.status in {"completed", "error"} else None),
        "status": work.status,
        "legacy_type": "self_hosted_work",
    }
    for key, value in data.items():
        response.setdefault(key, value)
    return response


def _work_state(status: str) -> str:
    if status in {"queued", "rescheduling"}:
        return "queued"
    if status == "leased":
        return "starting"
    if status == "running":
        return "active"
    if status == "stopping":
        return "stopping"
    return "stopped"


def _string_metadata(metadata: dict) -> dict[str, str]:
    return {str(key): str(value) for key, value in metadata.items() if value is not None}


def _worker_id_for_work(work, worker_id: str | None, anthropic_worker_id: str | None) -> str | None:
    if worker_id:
        return worker_id
    if anthropic_worker_id:
        return anthropic_worker_id
    lease = (work.data or {}).get("lease") or {}
    lease_worker_id = lease.get("worker_id")
    return str(lease_worker_id) if lease_worker_id else None


def _check_heartbeat_precondition(work, expected_last_heartbeat: str | None) -> None:
    if expected_last_heartbeat is None:
        return
    last_heartbeat = (work.data or {}).get("last_heartbeat_at")
    if expected_last_heartbeat == "NO_HEARTBEAT":
        if last_heartbeat:
            raise HTTPException(status_code=412, detail="Heartbeat precondition failed")
        return
    if str(last_heartbeat or "") != expected_last_heartbeat:
        raise HTTPException(status_code=412, detail="Heartbeat precondition failed")


def _oldest_queued_at(work_items: list) -> datetime | None:
    queued = [work.created_at for work in work_items if work.status in {"queued", "rescheduling"}]
    return min(queued) if queued else None


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _must_get_environment(db: AsyncSession, environment_id: str):
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment


async def _must_get_work(
    db: AsyncSession,
    environment_id: str,
    work_id: str,
    *,
    for_update: bool = False,
):
    work = await res_q.get_resource(
        db,
        resource_id=work_id,
        resource_type="environment_work",
        parent_id=environment_id,
        for_update=for_update,
    )
    if work is None:
        raise HTTPException(status_code=404, detail="Environment work item not found")
    return work
