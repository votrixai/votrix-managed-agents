import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as env_q
from app.db.queries import events as events_q
from app.db.queries import resources as res_q
from app.db.queries import session_sandboxes as sandboxes_q
from app.db.queries import sessions as sessions_q
from app.governance import QuotaExceededError
from app.governance_runtime import governance_service
from app.runtime.agent_resolution import effective_agent_version
from app.runtime.contracts import EffectiveAgentVersion, RuntimeEventEmitter, RuntimePreviewEmitter, RuntimeResult
from app.runtime.model_credentials import (
    MODEL_CREDENTIAL_BINDING_KEY,
    ModelCredentialUnavailableError,
    binding_from_status_details,
    load_bound_model_credential,
    resolve_model_credential_binding,
    status_details_with_binding,
    validate_binding_for_model,
)
from app.runtime.model_inputs import referenced_managed_file_ids
from app.runtime.providers import runtime_provider_id
from app.runtime.sandbox import sandbox_plan_from_environment
from app.runtime.sandbox_inputs import SandboxInputDescriptor
from app.runtime.sandbox_outputs import persist_discovered_outputs
from app.runtime.work_queue import WorkExecutionLease
from app.secret_cipher import decrypt_secret_values
from app.session_state import (
    SESSION_IDLE,
    SESSION_RESCHEDULING,
    SESSION_RUNNING,
    SESSION_TERMINATED,
    can_start_work,
    is_waiting_for_action,
)
from app.organization import resolve_organization_id

logger = structlog.get_logger()
_running_sessions: set[str] = set()
_running_lock = asyncio.Lock()
MAX_TRANSIENT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 300
MAX_MEMORY_CONTEXT_STORES = 8
MAX_MEMORY_CONTEXT_ITEMS_PER_STORE = 20
MAX_MEMORY_CONTEXT_CONTENT_CHARS = 1000
RUNTIME_HISTORY_PAGE_SIZE = 500


class SessionRunStopped(RuntimeError):
    """Raised when a concurrent control-plane action stops an active run."""


def schedule_session_run(session_id: str, *, organization_id: str | None = None) -> None:
    asyncio.create_task(run_session_turn(session_id, organization_id=organization_id))


async def run_session_turn(
    session_id: str,
    *,
    organization_id: str | None = None,
    work_lease: WorkExecutionLease | None = None,
) -> bool:
    scoped_organization_id = resolve_organization_id(organization_id)
    run_key = f"{scoped_organization_id}:{session_id}"
    async with _running_lock:
        if run_key in _running_sessions:
            logger.info("session_already_running", session_id=session_id, organization_id=scoped_organization_id)
            return False
        _running_sessions.add(run_key)

    try:
        return await _run_session_turn(
            session_id,
            organization_id=scoped_organization_id,
            work_lease=work_lease,
        )
    finally:
        async with _running_lock:
            _running_sessions.discard(run_key)


async def _run_session_turn(
    session_id: str,
    *,
    organization_id: str,
    work_lease: WorkExecutionLease | None = None,
) -> bool:
    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id, organization_id=organization_id, for_update=True)
        if session is None or session.deleted_at is not None:
            return False
        if work_lease is not None:
            if not await _work_lease_is_current(db, work_lease):
                return False
            existing_lease = dict((session.status_details or {}).get("execution_lease") or {})
            recovering_same_work = (
                session.status == SESSION_RUNNING
                and str(existing_lease.get("work_id") or "") == work_lease.work_id
            )
            if not can_start_work(session.status, session.stop_reason) and not recovering_same_work:
                return False
        elif not can_start_work(session.status, session.stop_reason):
            return False
        if get_settings().vma_governance_enabled:
            token_decision = await governance_service().preflight_model_tokens_in_session(
                db,
                organization_id=organization_id,
                source_type="session",
                source_id=session.id,
            )
            if not token_decision.allowed:
                raise QuotaExceededError(token_decision)
        status_details = _status_details_with_execution_lease(session.status_details, work_lease)
        await sessions_q.update_session(
            db,
            session,
            status=SESSION_RUNNING,
            status_details=status_details,
            stop_reason={"type": "in_progress"},
        )
        await events_q.append_event(
            db,
            session,
            event_type="session.status_running",
            payload={"type": "session.status_running", "status": SESSION_RUNNING},
        )
        await db.commit()

    try:
        async with session_scope() as db:
            session = await sessions_q.get_session(db, session_id, organization_id=organization_id)
            if session is None:
                return False
            agent = await agents_q.get_agent(db, session.agent_id, organization_id=organization_id)
            if agent is None:
                raise RuntimeError(f"Agent {session.agent_id} not found")
            version = await agents_q.get_agent_version(
                db,
                agent_id=session.agent_id,
                version=session.agent_version,
                organization_id=organization_id,
            )
            if version is None:
                raise RuntimeError(f"Agent version {session.agent_version} not found")
            environment = await env_q.get_environment(db, session.environment_id, organization_id=organization_id)
            if environment is None or environment.deleted_at is not None:
                raise RuntimeError(f"Environment {session.environment_id} not found")
            history = await _list_runtime_history(
                db,
                session_id=session.id,
                organization_id=organization_id,
            )
            effective_version = effective_agent_version(version, session.status_details)
            runtime_context = await _runtime_context_for_session(
                db,
                session,
                effective_version,
                session_file_content_ids=_current_turn_model_file_ids(
                    history,
                    dict(session.run_state or {}),
                ),
                prefer_persisted_sandbox_inputs=True,
            )
            await _append_runtime_context_events(db, session, runtime_context)
            # Legacy Sessions may acquire their immutable model Credential binding
            # while building runtime context. Commit it before making the model call.
            await db.commit()

        async def emit_event(payload: dict[str, Any]) -> str:
            return await _persist_runtime_event(
                session_id,
                organization_id,
                payload,
                work_lease=work_lease,
            )

        async def emit_preview(frame: dict[str, Any]) -> None:
            from app.runtime.vma_preview_bus import publish_vma_preview

            await publish_vma_preview(session_id, frame, organization_id=organization_id)

        result = await _execute(
            effective_version,
            history,
            environment.config,
            runtime_context=runtime_context,
            organization_id=organization_id,
            session_id=session_id,
            emit_event=emit_event,
            emit_preview=emit_preview,
        )

        usage_fence_current = await _record_model_usage_after_result(
            session_id=session_id,
            organization_id=organization_id,
            effective_version=effective_version,
            history=history,
            result=result,
            work_lease=work_lease,
        )
        if not usage_fence_current:
            return False

        async with session_scope() as db:
            session = await sessions_q.get_session(db, session_id, organization_id=organization_id, for_update=True)
            if session is None:
                return False
            if work_lease is not None and not await _execution_lease_is_current(db, session, work_lease):
                return False
            if session.status != SESSION_RUNNING or is_waiting_for_action(session.stop_reason):
                return True
            await persist_discovered_outputs(db, session, result.sandbox_outputs)
            blocking_event_ids: list[str] = list(result.blocking_event_ids)
            explicit_confirmation_events = any(event.get("requires_confirmation") for event in result.tool_events)
            if not result.events_persisted:
                for tool_event in result.tool_events:
                    event = await events_q.append_event(
                        db,
                        session,
                        event_type=tool_event["type"],
                        payload=tool_event,
                    )
                    if _is_required_action_event(
                        result,
                        tool_event,
                        explicit_confirmation_events=explicit_confirmation_events,
                    ):
                        blocking_event_ids.append(event.id)
            if result.requires_action:
                stop_reason = {"type": "requires_action", "event_ids": blocking_event_ids}
                await sessions_q.update_session(
                    db,
                    session,
                    status=SESSION_IDLE,
                    status_details=_status_details_without_runtime_retry(session.status_details),
                    stop_reason=stop_reason,
                    run_state=result.run_state,
                    sandbox_state=result.sandbox_state,
                )
                await events_q.append_event(
                    db,
                    session,
                    event_type="session.status_idle",
                    payload={
                        "type": "session.status_idle",
                        "status": SESSION_IDLE,
                        "stop_reason": stop_reason,
                        "usage": result.usage or {},
                    },
                )
                await db.commit()
                return True
            if (
                result.final_text
                and not result.events_persisted
                and not any(event["type"] == "agent.message" for event in result.tool_events)
            ):
                await events_q.append_event(
                    db,
                    session,
                    event_type="agent.message",
                    payload={
                        "type": "agent.message",
                        "content": [{"type": "text", "text": result.final_text}],
                    },
                )
            stop_reason = {"type": "end_turn"}
            status_details = _status_details_without_runtime_retry(session.status_details)
            outcome_evaluation = _outcome_evaluation_from_history(history, result)
            if outcome_evaluation is not None:
                outcome_event = await events_q.append_event(
                    db,
                    session,
                    event_type="span.outcome_evaluation_end",
                    payload={"type": "span.outcome_evaluation_end", **outcome_evaluation},
                )
                outcome_evaluation = {**outcome_evaluation, "event_id": outcome_event.id}
                status_details = _status_details_with_outcome_evaluation(status_details, outcome_evaluation)
            await sessions_q.update_session(
                db,
                session,
                status=SESSION_IDLE,
                status_details=status_details,
                stop_reason=stop_reason,
                run_state=result.run_state,
                sandbox_state=result.sandbox_state,
            )
            await events_q.append_event(
                db,
                session,
                event_type="session.status_idle",
                payload={
                    "type": "session.status_idle",
                    "status": SESSION_IDLE,
                    "stop_reason": stop_reason,
                    "usage": result.usage or {},
                },
            )
            await db.commit()
            return True
    except SessionRunStopped:
        logger.info("session_run_stopped", session_id=session_id, organization_id=organization_id)
        return True
    except Exception as exc:
        if _is_transient_runtime_error(exc):
            logger.warning("session_run_transient_error", session_id=session_id, error_type=exc.__class__.__name__)
        else:
            logger.exception("session_run_failed", session_id=session_id)
        async with session_scope() as db:
            session = await sessions_q.get_session(db, session_id, organization_id=organization_id, for_update=True)
            if session is None:
                return False
            if work_lease is not None and not await _execution_lease_is_current(db, session, work_lease):
                return False
            if _is_transient_runtime_error(exc):
                await _mark_transient_runtime_failure(db, session, exc)
                await db.commit()
                return True
            stop_reason = {"type": "error"}
            await sessions_q.update_session(
                db,
                session,
                status=SESSION_TERMINATED,
                status_details=_status_details_without_runtime_retry(session.status_details),
                stop_reason=stop_reason,
            )
            await events_q.append_event(
                db,
                session,
                event_type="session.error",
                payload={
                    "type": "session.error",
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            )
            await events_q.append_event(
                db,
                session,
                event_type="session.status_terminated",
                payload={"type": "session.status_terminated", "status": SESSION_TERMINATED, "stop_reason": stop_reason},
            )
            await db.commit()
            return True


async def _record_model_usage_after_result(
    *,
    session_id: str,
    organization_id: str,
    effective_version: EffectiveAgentVersion,
    history: list[Any],
    result: RuntimeResult,
    work_lease: WorkExecutionLease | None,
) -> bool:
    """Durably meter one provider result after verifying its execution fence.

    The usage transaction commits before output/session finalization so a later
    persistence failure cannot erase provider usage that has already occurred.
    """
    if not get_settings().vma_governance_enabled:
        return True
    dimensions, total_tokens = _model_usage_dimensions(result.usage)
    if total_tokens is None:
        return True

    async with session_scope() as db:
        session = await sessions_q.get_session(
            db,
            session_id,
            organization_id=organization_id,
            for_update=True,
        )
        if session is None:
            return False
        if work_lease is not None:
            if not await _execution_lease_is_current(db, session, work_lease):
                return False
        elif session.status != SESSION_RUNNING:
            return False

        model_config = dict(effective_version.model or {})
        provider = runtime_provider_id(
            model_config,
            runtime=effective_version.runtime,
        )
        model = str(model_config.get("id") or model_config.get("model") or "").strip() or None
        idempotency_key = _model_usage_idempotency_key(
            session_id=session_id,
            history=history,
            work_lease=work_lease,
        )
        decision = await governance_service().postflight_model_tokens_in_session(
            db,
            organization_id=organization_id,
            total_tokens=total_tokens,
            idempotency_key=idempotency_key,
            provider=provider,
            model=model,
            source_type="session",
            source_id=session_id,
            dimensions=dimensions,
            data={
                "work_id": work_lease.work_id if work_lease is not None else None,
                "lease_generation": work_lease.generation if work_lease is not None else None,
            },
        )
        await db.commit()

    if decision.over_limit_by:
        logger.warning(
            "model_token_quota_overrun_recorded",
            organization_id=organization_id,
            session_id=session_id,
            total_tokens=total_tokens,
            over_limit_by=decision.over_limit_by,
        )
    return True


def _model_usage_dimensions(usage: dict[str, Any] | None) -> tuple[dict[str, Any], int | None]:
    dimensions = _normalized_usage_mapping(usage)
    if not dimensions:
        return {}, None

    total_tokens = _nonnegative_usage_int(dimensions.get("total_tokens"))
    if total_tokens is None:
        input_tokens = _first_usage_int(dimensions, "input_tokens", "prompt_tokens")
        output_tokens = _first_usage_int(dimensions, "output_tokens", "completion_tokens")
        if input_tokens is None and output_tokens is None:
            return dimensions, None
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
        dimensions.setdefault("total_tokens", total_tokens)
    return dimensions, total_tokens


def _normalized_usage_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, int) and raw_value >= 0:
            normalized[key] = raw_value
        elif isinstance(raw_value, dict):
            child = _normalized_usage_mapping(raw_value)
            if child:
                normalized[key] = child
    return normalized


def _first_usage_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _nonnegative_usage_int(mapping.get(key))
        if value is not None:
            return value
    return None


def _nonnegative_usage_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _model_usage_idempotency_key(
    *,
    session_id: str,
    history: list[Any],
    work_lease: WorkExecutionLease | None,
) -> str:
    if work_lease is not None:
        return f"model_tokens:{work_lease.work_id}"
    history_id = getattr(history[-1], "id", None) if history else None
    return f"model_tokens:{session_id}:{history_id or 'initial'}"


async def _list_runtime_history(
    db,
    *,
    session_id: str,
    organization_id: str,
) -> list[Any]:
    """Load the complete ordered event history without a fixed first-page cap."""
    history: list[Any] = []
    after_seq = 0
    while True:
        page = await events_q.list_events(
            db,
            session_id=session_id,
            after_seq=after_seq,
            limit=RUNTIME_HISTORY_PAGE_SIZE,
            organization_id=organization_id,
        )
        if not page:
            return history
        history.extend(page)
        next_after_seq = int(page[-1].seq)
        if next_after_seq <= after_seq:
            raise RuntimeError("Session event pagination did not advance")
        after_seq = next_after_seq
        if len(page) < RUNTIME_HISTORY_PAGE_SIZE:
            return history


def _current_turn_model_file_ids(
    history: list[Any],
    previous_run_state: dict[str, Any],
) -> frozenset[str]:
    """Return attachments used by the exact input Deep Agents will consume.

    Deep Agents resumes a pending tool action before considering a new user
    message. Otherwise it selects the last user input after
    ``last_input_event_seq``. Mirroring that ordering here prevents historical
    file references from causing repeated R2 downloads.
    """

    pending = previous_run_state.get("pending_actions")
    if isinstance(pending, list) and pending:
        # Keep this decision byte-for-byte aligned with Deep Agents: an
        # incomplete pending action does not suppress a later user input.
        from app.runtime.deepagents_engine import _resume_command

        command, _processed_seq = _resume_command(history, pending)
        if command is not None:
            return frozenset()
    last_seq = int(previous_run_state.get("last_input_event_seq") or 0)
    candidate = None
    for event in history:
        if event.seq > last_seq and event.type in {"user.message", "user.define_outcome"}:
            candidate = event
    if candidate is None or candidate.type != "user.message":
        return frozenset()
    payload = candidate.payload if isinstance(candidate.payload, dict) else {}
    return referenced_managed_file_ids(
        payload.get("content") or payload.get("text") or ""
    )


def _status_details_with_execution_lease(
    status_details: dict[str, Any] | None,
    work_lease: WorkExecutionLease | None,
) -> dict[str, Any]:
    details = dict(status_details or {})
    if work_lease is None:
        details.pop("execution_lease", None)
    else:
        details["execution_lease"] = {
            "work_id": work_lease.work_id,
            "worker_id": work_lease.worker_id,
            "lease_id": work_lease.lease_id,
            "generation": work_lease.generation,
        }
    return details


async def _work_lease_is_current(db, work_lease: WorkExecutionLease) -> bool:
    work = await res_q.get_work_item_for_worker(db, work_lease.work_id)
    if work is None or work.status not in {"leased", "running"}:
        return False
    lease = dict((work.data or {}).get("lease") or {})
    return (
        str(lease.get("worker_id") or "") == work_lease.worker_id
        and str(lease.get("lease_id") or "") == work_lease.lease_id
        and int(lease.get("generation") or 0) == work_lease.generation
    )


async def _execution_lease_is_current(db, session, work_lease: WorkExecutionLease) -> bool:
    if not await _work_lease_is_current(db, work_lease):
        return False
    lease = dict((session.status_details or {}).get("execution_lease") or {})
    return (
        str(lease.get("work_id") or "") == work_lease.work_id
        and str(lease.get("worker_id") or "") == work_lease.worker_id
        and str(lease.get("lease_id") or "") == work_lease.lease_id
        and int(lease.get("generation") or 0) == work_lease.generation
    )


async def _persist_runtime_event(
    session_id: str,
    organization_id: str,
    payload: dict[str, Any],
    *,
    work_lease: WorkExecutionLease | None = None,
) -> str:
    event_payload = dict(payload)
    event_type = str(event_payload.get("type") or "")
    if not event_type:
        raise ValueError("Runtime events require a type")
    preferred_id = event_payload.pop("_event_id", None)
    if preferred_id is not None:
        preferred_id = str(preferred_id)
        if len(preferred_id) > 64:
            raise ValueError("Runtime event id exceeds 64 characters")
    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id, organization_id=organization_id, for_update=True)
        if session is None or session.deleted_at is not None:
            raise RuntimeError("Session disappeared while persisting a runtime event")
        if work_lease is not None and not await _execution_lease_is_current(db, session, work_lease):
            raise SessionRunStopped("Session execution lease was superseded")
        if session.status != SESSION_RUNNING:
            raise SessionRunStopped(f"Session is no longer running: {session.status}")
        event = await events_q.append_event(
            db,
            session,
            event_type=event_type,
            payload=event_payload,
            event_id=preferred_id,
        )
        await db.commit()
        return event.id


async def _mark_transient_runtime_failure(db, session, exc: Exception) -> None:
    details = dict(session.status_details or {})
    details.pop("execution_lease", None)
    retry_state = dict(details.get("runtime_retry") or {})
    attempt = int(retry_state.get("attempt") or 0) + 1
    retry_after_seconds = _retry_after_seconds(exc, attempt)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
    error = {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "status_code": _status_code(exc),
    }
    details["runtime_retry"] = {
        "attempt": attempt,
        "max_attempts": MAX_TRANSIENT_RETRY_ATTEMPTS,
        "retry_after_seconds": retry_after_seconds,
        "retry_at": retry_at.isoformat(),
        "error": error,
    }
    if attempt >= MAX_TRANSIENT_RETRY_ATTEMPTS:
        stop_reason = {
            "type": "error",
            "transient": True,
            "attempt": attempt,
            "max_attempts": MAX_TRANSIENT_RETRY_ATTEMPTS,
            "error_type": exc.__class__.__name__,
        }
        await sessions_q.update_session(
            db,
            session,
            status=SESSION_TERMINATED,
            status_details=details,
            stop_reason=stop_reason,
        )
        await events_q.append_event(
            db,
            session,
            event_type="session.error",
            payload={
                "type": "session.error",
                "message": str(exc),
                "error_type": exc.__class__.__name__,
                "transient": True,
                "attempt": attempt,
            },
        )
        await events_q.append_event(
            db,
            session,
            event_type="session.status_terminated",
            payload={"type": "session.status_terminated", "status": SESSION_TERMINATED, "stop_reason": stop_reason},
        )
        return

    stop_reason = {
        "type": "transient_error",
        "error_type": exc.__class__.__name__,
        "attempt": attempt,
        "max_attempts": MAX_TRANSIENT_RETRY_ATTEMPTS,
        "retry_after_seconds": retry_after_seconds,
        "retry_at": retry_at.isoformat(),
    }
    await sessions_q.update_session(
        db,
        session,
        status=SESSION_RESCHEDULING,
        status_details=details,
        stop_reason=stop_reason,
    )
    await events_q.append_event(
        db,
        session,
        event_type="session.error",
        payload={
            "type": "session.error",
            "message": str(exc),
            "error_type": exc.__class__.__name__,
            "transient": True,
            "attempt": attempt,
        },
    )
    await events_q.append_event(
        db,
        session,
        event_type="session.status_rescheduled",
        payload={"type": "session.status_rescheduled", "status": SESSION_RESCHEDULING, "stop_reason": stop_reason},
    )


def _is_transient_runtime_error(exc: Exception) -> bool:
    status_code = _status_code(exc)
    if status_code is not None and (status_code in {408, 409, 425, 429} or status_code >= 500):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = exc.__class__.__name__.lower()
    return any(
        marker in name
        for marker in (
            "ratelimit",
            "rate_limit",
            "timeout",
            "connection",
            "temporar",
            "serviceunavailable",
            "internalserver",
        )
    )


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return None


def _retry_after_seconds(exc: Exception, attempt: int) -> int:
    direct = _positive_int(getattr(exc, "retry_after", None))
    if direct is not None:
        return min(direct, MAX_RETRY_AFTER_SECONDS)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        header_value = None
        try:
            header_value = headers.get("retry-after")
        except AttributeError:
            header_value = None
        parsed = _positive_int(header_value)
        if parsed is not None:
            return min(parsed, MAX_RETRY_AFTER_SECONDS)
    backoff = DEFAULT_RETRY_AFTER_SECONDS * (2 ** max(attempt - 1, 0))
    return min(backoff, MAX_RETRY_AFTER_SECONDS)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _status_details_without_runtime_retry(status_details: dict[str, Any] | None) -> dict[str, Any]:
    details = dict(status_details or {})
    details.pop("runtime_retry", None)
    details.pop("execution_lease", None)
    return details


def _status_details_with_outcome_evaluation(
    status_details: dict[str, Any],
    outcome_evaluation: dict[str, Any],
) -> dict[str, Any]:
    details = dict(status_details or {})
    evaluations = list(details.get("outcome_evaluations") or [])
    evaluations.append(outcome_evaluation)
    details["outcome_evaluations"] = evaluations
    return details


async def _execute(
    version,
    history,
    environment_config: dict[str, Any] | None = None,
    *,
    runtime_context: dict[str, Any] | None = None,
    organization_id: str | None = None,
    session_id: str | None = None,
    emit_event: RuntimeEventEmitter | None = None,
    emit_preview: RuntimePreviewEmitter | None = None,
) -> RuntimeResult:
    from app.runtime.deepagents_engine import execute_deep_agent

    return await execute_deep_agent(
        version,
        history,
        environment_config,
        runtime_context=runtime_context,
        organization_id=organization_id,
        session_id=session_id,
        emit_event=emit_event,
        emit_preview=emit_preview,
    )


async def _execute_local(
    version,
    history,
    environment_config: dict[str, Any] | None = None,
    *,
    runtime_context: dict[str, Any] | None = None,
) -> RuntimeResult:
    await asyncio.sleep(0.05)
    sandbox_plan = sandbox_plan_from_environment(environment_config)
    memory_context = _memory_context_for_run_state(runtime_context)
    latest_action = _latest_user_action_event(history)
    if latest_action is not None:
        return RuntimeResult(
            final_text=_local_action_result_text(latest_action),
            run_state={
                "backend": "local",
                "agent_version_id": version.id,
                "resumed_from": latest_action.type,
                "memory_context": memory_context,
                "mcp_auth": _mcp_context_for_run_state(runtime_context),
            },
            sandbox_state={**sandbox_plan.summary, "runtime_backend": "local"},
        )

    latest = _latest_user_text(history)
    if latest:
        custom_tool = _first_custom_tool(version.tools)
        if custom_tool is not None:
            name = str(custom_tool.get("name") or "custom_tool")
            return RuntimeResult(
                final_text="",
                tool_events=[
                    {
                        "type": "agent.custom_tool_use",
                        "name": name,
                        "input": {"prompt": latest},
                        "tool": _public_tool_summary(custom_tool),
                    }
                ],
                requires_action=True,
                run_state={
                    "backend": "local",
                    "agent_version_id": version.id,
                    "pending_action": "custom_tool",
                    "memory_context": memory_context,
                    "mcp_auth": _mcp_context_for_run_state(runtime_context),
                },
                sandbox_state={**sandbox_plan.summary, "runtime_backend": "local"},
            )

        confirmation_tool = _first_confirmation_tool(version.tools)
        if confirmation_tool is not None:
            return RuntimeResult(
                final_text="",
                tool_events=[
                    {
                        "type": confirmation_tool["event_type"],
                        "name": confirmation_tool["name"],
                        "input": {"prompt": latest},
                        "permission_policy": {"type": "always_ask"},
                        "tool": confirmation_tool["tool"],
                    }
                ],
                requires_action=True,
                run_state={
                    "backend": "local",
                    "agent_version_id": version.id,
                    "pending_action": "tool_confirmation",
                    "memory_context": memory_context,
                    "mcp_auth": _mcp_context_for_run_state(runtime_context),
                },
                sandbox_state={**sandbox_plan.summary, "runtime_backend": "local"},
            )

        text = f"Votrix Managed Agents local runtime received: {latest}"
        memory_prompt = _memory_context_prompt(runtime_context)
        if memory_prompt:
            text = f"{text}\n\n{memory_prompt}"
    else:
        text = "Votrix Managed Agents local runtime is idle."
    return RuntimeResult(
        final_text=text,
        run_state={
            "backend": "local",
            "agent_version_id": version.id,
            "memory_context": memory_context,
            "mcp_auth": _mcp_context_for_run_state(runtime_context),
        },
        sandbox_state={**sandbox_plan.summary, "runtime_backend": "local"},
    )


async def _runtime_context_for_session(
    db,
    session,
    version: EffectiveAgentVersion,
    *,
    session_file_content_ids: set[str] | frozenset[str] | None = None,
    prefer_persisted_sandbox_inputs: bool = False,
) -> dict[str, Any]:
    """Resolve the run-scoped control-plane context.

    Normal E2B turns set ``prefer_persisted_sandbox_inputs`` because the
    sandbox binding is then the authority for the already-materialized
    filesystem. Create-time provisioning, append fallback, and state backends
    leave it false and retain complete materialization. In the persisted case,
    ``session_file_content_ids`` selects only the immutable files whose bytes
    the current model request needs.
    """
    session_resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        limit=1000,
        organization_id=session.organization_id,
    )
    persisted_inputs = (
        await _persisted_e2b_inputs_for_session(
            db,
            session,
            session_resources=session_resources,
        )
        if prefer_persisted_sandbox_inputs
        else None
    )
    selected_file_ids = (
        frozenset(session_file_content_ids or ())
        if persisted_inputs is not None
        else None
    )
    memory_resources = [
        resource
        for resource in session_resources
        if (resource.data or {}).get("type") == "memory_store"
    ][:MAX_MEMORY_CONTEXT_STORES]
    memory_stores = []
    for resource in memory_resources:
        data = dict(resource.data or {})
        memory_store_id = str(data.get("memory_store_id") or "")
        if not memory_store_id:
            continue
        memories = await res_q.list_resources(
            db,
            resource_type="memory",
            parent_id=memory_store_id,
            limit=MAX_MEMORY_CONTEXT_ITEMS_PER_STORE,
            organization_id=session.organization_id,
        )
        memory_stores.append(
            {
                "memory_store_id": memory_store_id,
                "name": data.get("name"),
                "mount_path": data.get("mount_path"),
                "access": data.get("access"),
                "instructions": data.get("instructions"),
                "memories": [_memory_context_item(memory) for memory in memories if not (memory.data or {}).get("redacted")],
            }
        )
    credentials = await _vault_credentials_for_session(db, session)
    model_credential = await _model_credential_for_session(db, session, version)
    runtime_context = {
        "organization_id": session.organization_id,
        "session_id": session.id,
        "checkpoint_thread_id": session.runtime_thread_id,
        "previous_run_state": dict(session.run_state or {}),
        "memory_stores": memory_stores,
        "provider_secrets": _provider_secrets_from_credentials(
            [model_credential] if model_credential is not None else []
        ),
        "mcp_auth": await _mcp_auth_context_for_session(db, session, version, credentials=credentials),
        "session_resource_types": sorted(
            {
                str((resource.data or {}).get("type") or "")
                for resource in session_resources
                if (resource.data or {}).get("type")
            }
        ),
        "session_files": await _session_files_for_runtime(
            session_resources,
            content_file_ids=selected_file_ids,
            immutable_manifest=(
                persisted_inputs["immutable_manifest"]
                if persisted_inputs is not None
                else None
            ),
        ),
        "skill_archives": (
            []
            if persisted_inputs is not None
            else await _skill_archives_for_runtime(db, session, version)
        ),
        "subagents": await _subagents_for_runtime(db, session, version),
    }
    if persisted_inputs is not None:
        runtime_context["persisted_sandbox_inputs"] = {
            key: value
            for key, value in persisted_inputs.items()
            if key != "immutable_manifest"
        }
    return runtime_context


async def _persisted_e2b_inputs_for_session(
    db,
    session,
    *,
    session_resources: list[Any],
) -> dict[str, Any] | None:
    """Load the content-free input identity for an already sealed E2B Session.

    Old or partially provisioned bindings fall back to full materialization so
    this optimization cannot silently weaken resume verification.
    """

    record = await sandboxes_q.get_session_sandbox(
        db,
        session.id,
        organization_id=session.organization_id,
    )
    if (
        record is None
        or record.provider != "e2b"
        or not record.external_sandbox_id
    ):
        return None
    config = dict(record.config or {})
    from app.runtime.sandbox_lifecycle import persisted_input_descriptor_from_config

    descriptor = persisted_input_descriptor_from_config(config)
    if descriptor is None:
        return None
    _validate_descriptor_session_files(descriptor, session_resources)
    return {
        "provider": "e2b",
        "skill_sources": list(descriptor.skill_sources),
        "memory_sources": list(descriptor.memory_sources),
        "mutable_roots": list(descriptor.mutable_roots),
        "immutable_manifest": descriptor.immutable_manifest,
    }


def _validate_descriptor_session_files(
    descriptor: SandboxInputDescriptor,
    session_resources: list[Any],
) -> None:
    """Compare frozen file entries with DB metadata before any object read."""

    from app.runtime.sandbox_lifecycle import SandboxInputMismatchError

    expected = {
        item.path: item.sha256
        for item in descriptor.files
        if item.source == "session_file"
    }
    current: dict[str, str] = {}
    for resource in session_resources:
        data = dict(resource.data or {})
        if data.get("type") != "file":
            continue
        session_file = dict(data.get("session_file") or {})
        path = str(data.get("mount_path") or f"/mnt/session/uploads/{resource.id}")
        sha256 = str(session_file.get("sha256") or "")
        stored = dict(session_file.get("storage") or {})
        if (
            not sha256
            or not isinstance(stored.get("key"), str)
            or not stored.get("key")
            or not bool(data.get("read_only", True))
            or path in current
        ):
            raise SandboxInputMismatchError(
                "Session file metadata no longer matches the sandbox binding; create a new Session"
            )
        current[path] = sha256
    if current != expected:
        raise SandboxInputMismatchError(
            "Session file metadata no longer matches the sandbox binding; create a new Session"
        )


async def _model_credential_for_session(db, session, version: EffectiveAgentVersion):
    details = dict(session.status_details or {})
    binding = binding_from_status_details(details)
    if binding is None:
        if MODEL_CREDENTIAL_BINDING_KEY in details:
            raise ModelCredentialUnavailableError("The Session model credential binding is invalid")
        # Compatibility for Sessions created before durable bindings existed:
        # select once on their first runtime turn, then persist the same contract
        # used by newly created Sessions.
        binding = await resolve_model_credential_binding(
            db,
            model=version.model,
            runtime=version.runtime,
            vault_ids=details.get("vault_ids") or [],
            organization_id=session.organization_id,
        )
        details = status_details_with_binding(details, binding)
        await sessions_q.update_session(db, session, status_details=details)

    validate_binding_for_model(binding, model=version.model, runtime=version.runtime)
    return await load_bound_model_credential(
        db,
        binding=binding,
        organization_id=session.organization_id,
    )


async def _vault_credentials_for_session(db, session) -> list[Any]:
    credentials: list[Any] = []
    for vault_id in (session.status_details or {}).get("vault_ids") or []:
        vault = await res_q.get_resource(
            db,
            resource_id=str(vault_id),
            resource_type="vault",
            organization_id=session.organization_id,
        )
        if vault is None or vault.archived_at is not None:
            continue
        credentials.extend(
            await res_q.list_resources(
                db,
                resource_type="credential",
                parent_id=str(vault_id),
                limit=1000,
                include_archived=False,
                organization_id=session.organization_id,
            )
        )
    return credentials


def _provider_secrets_from_credentials(credentials: list[Any]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for credential in credentials:
        auth = decrypt_secret_values(dict((credential.data or {}).get("auth") or {}))
        if auth.get("type") != "environment_variable":
            continue
        name = str(auth.get("secret_name") or "").strip()
        value = auth.get("secret_value")
        if name and isinstance(value, str) and value:
            secrets.setdefault(name, value)
    return secrets


async def _mcp_auth_context_for_session(
    db,
    session,
    version: EffectiveAgentVersion,
    *,
    credentials: list[Any] | None = None,
) -> dict[str, Any]:
    servers = [server for server in version.mcp_servers or [] if isinstance(server, dict)]
    if not servers:
        return {"servers": [], "errors": []}

    credentials = credentials if credentials is not None else await _vault_credentials_for_session(db, session)

    by_url: dict[str, Any] = {}
    for credential in credentials:
        auth = decrypt_secret_values(dict((credential.data or {}).get("auth") or {}))
        if not isinstance(auth, dict):
            continue
        url = _normalized_mcp_url(auth.get("mcp_server_url"))
        if url and url not in by_url:
            by_url[url] = credential

    resolved_servers = []
    errors = []
    for server in servers:
        server_url = str(server.get("url") or "")
        credential = by_url.get(_normalized_mcp_url(server_url))
        if credential is None:
            error = {
                "type": "mcp_auth_missing",
                "mcp_server_name": str(server.get("name") or ""),
                "mcp_server_url": server_url,
            }
            errors.append(error)
            resolved_servers.append({**error, "status": "missing", "credential_id": None, "vault_id": None})
            continue
        auth = decrypt_secret_values(dict((credential.data or {}).get("auth") or {}))
        token = auth.get("token") or auth.get("access_token")
        resolved = {
            "type": "mcp_auth",
            "status": "matched",
            "mcp_server_name": str(server.get("name") or ""),
            "mcp_server_url": server_url,
            "credential_id": credential.id,
            "vault_id": credential.parent_id,
            "auth_type": str(auth.get("type") or "mcp_oauth"),
        }
        if isinstance(token, str) and token:
            resolved["headers"] = {"Authorization": f"Bearer {token}"}
        resolved_servers.append(resolved)
    return {"servers": resolved_servers, "errors": errors}


async def _session_files_for_runtime(
    session_resources: list[Any],
    *,
    content_file_ids: set[str] | frozenset[str] | None = None,
    immutable_manifest: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return mounted-file metadata and selectively hydrate verified bytes.

    ``None`` preserves the legacy full materialization needed by create-time
    provisioning and the state backend.  Passing a concrete set (including an
    empty set) downloads only entries whose scoped or source ID is named.  The
    complete metadata index is always returned so model-input validation keeps
    detecting unmounted and ambiguous aliases without object-store reads. When
    a sealed manifest is supplied, every database row must still match that
    frozen Session identity before any selected object is read.
    """

    from app import storage

    files: list[dict[str, Any]] = []
    max_bytes = int(get_settings().vma_max_file_upload_bytes)
    selected_ids = None if content_file_ids is None else set(content_file_ids)
    for resource in session_resources:
        data = dict(resource.data or {})
        if data.get("type") != "file":
            continue
        stored = dict((data.get("session_file") or {}).get("storage") or {})
        key = stored.get("key")
        if not isinstance(key, str) or not key:
            continue
        declared_size = (data.get("session_file") or {}).get("size_bytes")
        if isinstance(declared_size, int) and declared_size > max_bytes:
            raise RuntimeError(f"Session file exceeds the runtime limit: {data.get('mount_path') or key}")
        file_id = str(data.get("file_id") or "")
        source_file_id = str(data.get("source_file_id") or "")
        expected_sha256 = str((data.get("session_file") or {}).get("sha256") or "")
        path = str(data.get("mount_path") or f"/mnt/session/uploads/{resource.id}")
        if immutable_manifest is not None:
            sealed_sha256 = immutable_manifest.get(path)
            if not isinstance(sealed_sha256, str) or not sealed_sha256:
                raise RuntimeError(f"Session file is absent from its sealed manifest: {path}")
            if not expected_sha256 or expected_sha256 != sealed_sha256:
                raise RuntimeError(f"Session file sha256 does not match its sealed manifest: {path}")
        mounted: dict[str, Any] = {
            "file_id": file_id,
            "source_file_id": source_file_id,
            "path": path,
            "filename": str(
                (data.get("session_file") or {}).get("filename")
                or data.get("filename")
                or resource.name
                or resource.id
            ),
            "mime_type": str(
                (data.get("session_file") or {}).get("mime_type")
                or "application/octet-stream"
            ),
            "size_bytes": declared_size,
            "sha256": expected_sha256,
            "storage_key": key,
            "read_only": bool(data.get("read_only", True)),
        }
        should_hydrate = selected_ids is None or bool(
            selected_ids.intersection({file_id, source_file_id} - {""})
        )
        if should_hydrate:
            content = await storage.download_file(key)
            if len(content) > max_bytes:
                raise RuntimeError(f"Session file exceeds the runtime limit: {data.get('mount_path') or key}")
            if isinstance(declared_size, int) and len(content) != declared_size:
                raise RuntimeError(
                    f"Session file size does not match its manifest: {data.get('mount_path') or key}"
                )
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"Session file sha256 does not match its manifest: {data.get('mount_path') or key}"
                )
            mounted.update(
                {
                    "size_bytes": len(content),
                    "sha256": actual_sha256,
                    "content": content,
                }
            )
        files.append(mounted)
    return files


async def _skill_archives_for_runtime(db, session, version: EffectiveAgentVersion) -> list[dict[str, Any]]:
    from app import storage

    archives: list[dict[str, Any]] = []
    for ref in version.skills or []:
        if not isinstance(ref, dict) or ref.get("type") == "anthropic":
            continue
        skill_id = str(ref.get("skill_id") or ref.get("id") or "")
        if not skill_id:
            continue
        skill = await res_q.get_resource(
            db,
            resource_id=skill_id,
            resource_type="skill",
            organization_id=session.organization_id,
        )
        if skill is None:
            continue
        raw_version = ref.get("version")
        if raw_version in {None, "", "latest"}:
            raw_version = (skill.data or {}).get("latest_version")
        try:
            version_number = int(raw_version)
        except (TypeError, ValueError):
            continue
        skill_version = await res_q.get_resource_version(
            db,
            resource_type="skill_version",
            parent_id=skill_id,
            version=version_number,
            organization_id=session.organization_id,
        )
        if skill_version is None or not skill_version.storage_key:
            continue
        content = await storage.download_file(skill_version.storage_key)
        if len(content) > int(get_settings().vma_max_skill_archive_bytes):
            raise RuntimeError(f"Skill archive exceeds the runtime limit: {skill_id}@{version_number}")
        archives.append(
            {
                "skill_id": skill_id,
                "version": version_number,
                "archive": content,
                "top_level_directory": (skill_version.data or {}).get("top_level_directory"),
            }
        )
    return archives


async def _subagents_for_runtime(db, session, version: EffectiveAgentVersion) -> list[dict[str, Any]]:
    roster = version.multiagent if isinstance(version.multiagent, dict) else {}
    subagents: list[dict[str, Any]] = []
    for entry in roster.get("agents") or []:
        if not isinstance(entry, dict):
            continue
        agent_id = str(entry.get("id") or "")
        try:
            agent_version = int(entry.get("version"))
        except (TypeError, ValueError):
            continue
        referenced = await agents_q.get_agent_version(
            db,
            agent_id=agent_id,
            version=agent_version,
            organization_id=session.organization_id,
        )
        if referenced is None:
            continue
        subagents.append(
            {
                "agent_id": referenced.agent_id,
                "agent_version_id": referenced.id,
                "version": referenced.version,
                "name": referenced.name,
                "description": referenced.description or f"Managed subagent {referenced.name}.",
                "system_prompt": referenced.system or "You are a helpful managed subagent.",
                "model": dict(referenced.model or {}),
                "tools": list(referenced.tools or []),
                "skills": list(referenced.skills or []),
            }
        )
    return subagents


async def _append_runtime_context_events(db, session, runtime_context: dict[str, Any]) -> bool:
    emitted = False
    mcp_auth = _mcp_context_for_run_state(runtime_context)
    for error in mcp_auth.get("errors") or []:
        await events_q.append_event(
            db,
            session,
            event_type="session.error",
            payload={
                "type": "session.error",
                "error_type": "mcp_auth_missing",
                "message": f"MCP credential not found for {error.get('mcp_server_name') or error.get('mcp_server_url')}",
                **error,
            },
        )
        emitted = True
    return emitted


def _memory_context_item(memory) -> dict[str, Any]:
    data = dict(memory.data or {})
    content = str(data.get("content") or "")
    if len(content) > MAX_MEMORY_CONTEXT_CONTENT_CHARS:
        content = f"{content[:MAX_MEMORY_CONTEXT_CONTENT_CHARS]}..."
    return {
        "memory_id": memory.id,
        "path": data.get("path"),
        "path_key": data.get("path_key"),
        "version": data.get("version"),
        "content": content,
    }


def _instructions_with_runtime_context(base: str, runtime_context: dict[str, Any] | None) -> str:
    memory_prompt = _memory_context_prompt(runtime_context)
    if not memory_prompt:
        return base
    return f"{base}\n\n{memory_prompt}"


def _memory_context_prompt(runtime_context: dict[str, Any] | None) -> str:
    memory_stores = _memory_context_for_run_state(runtime_context).get("memory_stores", [])
    if not memory_stores:
        return ""
    lines = ["Memory context:"]
    for store in memory_stores:
        label = store.get("name") or store.get("memory_store_id")
        lines.append(f"- Store {label}:")
        instructions = store.get("instructions")
        if instructions:
            lines.append(f"  Instructions: {instructions}")
        for memory in store.get("memories") or []:
            path = memory.get("path") or memory.get("path_key") or memory.get("memory_id")
            lines.append(f"  - {path}: {memory.get('content') or ''}")
    return "\n".join(lines)


def _memory_context_for_run_state(runtime_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_context, dict):
        return {"memory_stores": []}
    memory_stores = runtime_context.get("memory_stores")
    if not isinstance(memory_stores, list):
        return {"memory_stores": []}
    return {"memory_stores": memory_stores}


def _mcp_context_for_run_state(runtime_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_context, dict):
        return {"servers": [], "errors": []}
    mcp_auth = runtime_context.get("mcp_auth")
    if not isinstance(mcp_auth, dict):
        return {"servers": [], "errors": []}
    servers = mcp_auth.get("servers")
    errors = mcp_auth.get("errors")
    public_servers = []
    for server in servers if isinstance(servers, list) else []:
        if not isinstance(server, dict):
            continue
        public_servers.append(
            {
                key: value
                for key, value in server.items()
                if key not in {"headers", "authorization", "token", "access_token", "refresh_token"}
            }
        )
    return {
        "servers": public_servers,
        "errors": errors if isinstance(errors, list) else [],
    }


def _normalized_mcp_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _latest_user_action_event(history):
    for event in reversed(history):
        if not event.type.startswith("user."):
            continue
        return event if event.type in {"user.custom_tool_result", "user.tool_confirmation", "user.tool_result"} else None
    return None


def _local_action_result_text(event) -> str:
    if event.type == "user.custom_tool_result":
        result = _text_from_payload(event.payload) or "custom tool result received"
        return f"Votrix Managed Agents local runtime received custom tool result: {result}"

    if event.type == "user.tool_result":
        result = _text_from_payload(event.payload) or "tool result received"
        return f"Votrix Managed Agents local runtime received tool result: {result}"

    result = event.payload.get("result")
    if result == "deny":
        message = event.payload.get("deny_message") or "tool use denied"
        return f"Votrix Managed Agents local runtime received tool denial: {message}"
    return "Votrix Managed Agents local runtime received tool confirmation: allow"


def _first_custom_tool(tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for tool in tools or []:
        if isinstance(tool, dict) and _normalized_tool_type(tool) == "custom":
            return tool
    return None


def _first_confirmation_tool(tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = _normalized_tool_type(tool)
        if tool_type == "custom":
            continue
        if tool_type == "mcp_toolset" and _permission_policy_type(tool, default="always_ask") == "always_ask":
            return {
                "event_type": "agent.mcp_tool_use",
                "name": str(tool.get("mcp_server_name") or tool.get("name") or "mcp_tool"),
                "tool": _public_tool_summary(tool),
            }
        if tool_type == "agent_toolset_20260401":
            config_tool = _first_configured_always_ask_tool(tool)
            if config_tool is not None:
                return {
                    "event_type": "agent.tool_use",
                    "name": config_tool,
                    "tool": _public_tool_summary(tool),
                }
            if _permission_policy_type(tool, default="always_allow") == "always_ask":
                return {
                    "event_type": "agent.tool_use",
                    "name": "bash",
                    "tool": _public_tool_summary(tool),
                }
    return None


def _first_configured_always_ask_tool(tool: dict[str, Any]) -> str | None:
    configs = tool.get("configs")
    if not isinstance(configs, list):
        return None
    for config in configs:
        if not isinstance(config, dict):
            continue
        if config.get("enabled") is False:
            continue
        if _permission_policy_type(config, default="") == "always_ask":
            name = config.get("name")
            return str(name) if name else "tool"
    return None


def _permission_policy_type(config: dict[str, Any], *, default: str) -> str:
    default_config = config.get("default_config")
    candidates = [config]
    if isinstance(default_config, dict):
        candidates.insert(0, default_config)
    for candidate in candidates:
        policy = candidate.get("permission_policy") if isinstance(candidate, dict) else None
        if isinstance(policy, dict) and policy.get("type"):
            return str(policy["type"])
    return default


def _public_tool_summary(tool: dict[str, Any]) -> dict[str, Any]:
    summary = dict(tool)
    for key in ("authorization", "headers"):
        if key in summary:
            summary[key] = "redacted"
    return summary


def _normalized_tool_type(spec: dict[str, Any]) -> str | None:
    raw = spec.get("type") or spec.get("tool_type") or spec.get("name")
    if raw is None:
        return None
    return str(raw).strip().lower().replace("-", "_")


def _latest_user_text(history) -> str:
    for event in reversed(history):
        if event.type == "user.message":
            return _text_from_payload(event.payload)
        if event.type == "user.define_outcome":
            return _outcome_prompt(event.payload)
    return ""


def _latest_outcome_event(history):
    for event in reversed(history):
        if event.type == "user.define_outcome":
            return event
    return None


def _outcome_evaluation_from_history(history, result: RuntimeResult) -> dict[str, Any] | None:
    outcome_event = _latest_outcome_event(history)
    if outcome_event is None:
        return None
    outcome = _outcome_spec(outcome_event.payload)
    final_text = result.final_text or ""
    passed = bool(final_text.strip())
    return {
        "outcome": outcome,
        "result": {
            "type": "deterministic_local_grader",
            "passed": passed,
            "score": 1.0 if passed else 0.0,
            "summary": "Final response was produced." if passed else "No final response was produced.",
        },
        "grader_context": {
            "type": "local_deterministic",
            "max_iterations": outcome["max_iterations"],
            "rubric": outcome.get("rubric"),
        },
    }


def _outcome_spec(payload: dict[str, Any]) -> dict[str, Any]:
    objective = str(
        payload.get("description")
        or payload.get("objective")
        or payload.get("goal")
        or payload.get("name")
        or _text_from_payload(payload)
        or "Complete the requested outcome."
    )
    rubric = payload.get("rubric") or payload.get("criteria") or payload.get("success_criteria")
    try:
        max_iterations = int(payload.get("max_iterations") or payload.get("max_turns") or 3)
    except (TypeError, ValueError):
        max_iterations = 3
    if max_iterations < 1:
        max_iterations = 1
    if max_iterations > 20:
        max_iterations = 20
    return {
        "objective": objective,
        "rubric": rubric,
        "max_iterations": max_iterations,
    }


def _outcome_prompt(payload: dict[str, Any]) -> str:
    outcome = _outcome_spec(payload)
    rubric = outcome.get("rubric")
    if rubric:
        return f"{outcome['objective']}\nRubric: {rubric}"
    return outcome["objective"]


def _text_from_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    text = payload.get("text") or payload.get("message")
    return text if isinstance(text, str) else ""


def _is_required_action_event(
    result: RuntimeResult,
    event: dict[str, Any],
    *,
    explicit_confirmation_events: bool,
) -> bool:
    if not result.requires_action:
        return False
    if event["type"] not in {"agent.custom_tool_use", "agent.tool_use", "agent.mcp_tool_use"}:
        return False
    if explicit_confirmation_events:
        return bool(event.get("requires_confirmation"))
    return True
