from typing import Any

import structlog
from fastapi import APIRouter

from app.models.common import ApiModel
from app.routers.deps import Db, TaskCaller
from app.services import sessions as service

logger = structlog.get_logger(__name__)

# Every route here skips the busy check and starts work directly, so the caller
# has to prove it is the queue. Under `inline` dispatch nothing legitimate calls
# these and the dependency answers 404.
router = APIRouter(
    prefix="/internal/sessions",
    include_in_schema=False,
    dependencies=[TaskCaller],
)


class ProcessSessionRequest(ApiModel):
    """What the queue hands back to us.

    The batch travels in the payload rather than being looked up, because the
    history the agent needs is in the graph checkpoint, not our tables — this
    endpoint has no reason to read the event log at all. It is the whole batch
    because a paused graph is resumed with one decision per call it stopped on,
    counted.
    """

    events: list[dict[str, Any]]


@router.post("/{session_id}/process", status_code=204)
async def process_session(
    session_id: str,
    body: ProcessSessionRequest,
    db: Db,
    generation: int | None = None,
) -> None:
    """Run one turn.

    No organization header: a worker is woken by a queue, not by a tenant, so
    it has none of its own. The session it is handed carries the organization
    everything else is scoped to.

    Answers 204 even when the turn fails. The failure is already recorded as a
    `session.error` event, and a turn that broke on this delivery will break on
    the next one — reporting it would only have the queue redeliver a turn that
    cannot succeed, against a session that has already been told it is over.
    """
    try:
        await service.process_session(
            db,
            session_id=session_id,
            events=body.events,
            expected_generation=generation,
        )
    except Exception:
        logger.exception("cloud_turn_failed", session_id=session_id)
