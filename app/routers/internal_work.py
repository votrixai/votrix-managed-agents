from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import structlog

from app.config import get_settings
from app.runtime.turn_limiter import TurnLimiter
from app.runtime.work_queue import execute_work_item


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal/work", include_in_schema=False)

_SUCCESS_OUTCOMES = frozenset(
    {
        "completed",
        "error",
        "stopped",
        "exhausted",
        "rescheduling",
        "already_running",
        "superseded",
        "missing",
        "not_runnable",
    }
)


@router.post("/{work_id}/execute")
async def execute_internal_work(work_id: str, request: Request) -> JSONResponse:
    settings = get_settings()
    limiter: TurnLimiter = request.app.state.turn_limiter
    worker_id = f"push-{uuid4().hex[:8]}"

    async with limiter.acquire():
        try:
            outcome = await execute_work_item(
                work_id,
                worker_id=worker_id,
                lease_seconds=settings.vma_worker_lease_seconds,
            )
        except Exception as exc:
            logger.exception(
                "push_work_execution_failed",
                work_id=work_id,
                worker_id=worker_id,
                exc_info=exc,
            )
            return JSONResponse(
                status_code=500,
                content={"outcome": "internal_error"},
            )

    if outcome in _SUCCESS_OUTCOMES:
        return JSONResponse(status_code=200, content={"outcome": outcome})
    if outcome == "deferred":
        return JSONResponse(
            status_code=503,
            content={"outcome": outcome},
            headers={"Retry-After": "15"},
        )

    logger.error(
        "push_work_execution_returned_unknown_outcome",
        work_id=work_id,
        worker_id=worker_id,
        outcome=outcome,
    )
    return JSONResponse(
        status_code=500,
        content={"outcome": "internal_error"},
    )
