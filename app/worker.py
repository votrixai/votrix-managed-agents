"""Recovering sessions whose worker never came back.

Turns are run by the API process — inline, or by Cloud Tasks calling
`/internal/sessions/{id}/process`. Neither of those can clean up after itself
if the process dies mid-turn, which is what this is for.

Run it with `python -m app.worker`, or set `VMA_RUN_SWEEPER=true` and the app
starts `run_forever` on its own loop. Hosted worker instances enable it whenever
Cloud Tasks wakes them; scale-to-zero makes the sweep best-effort while idle.

Running it at all is optional: a stranded session already frees itself the
moment its lease lapses, so all this adds is not having to wait for the next
message to notice. What the wait costs is the conversation reading as though
the agent were still typing, for as long as nobody sends anything.
"""

from __future__ import annotations

import asyncio

import structlog

from app.db.engine import session_scope
from app.db.queries import sessions as sessions_q
from app.models.sessions import IDLE
from app.services.sessions import LEASE_SECONDS

logger = structlog.get_logger(__name__)

SWEEP_INTERVAL_SECONDS = 60


async def sweep_once() -> int:
    """Put expired sessions back to idle. Returns how many were swept."""
    async with session_scope() as db:
        stranded = await sessions_q.list_stuck_sessions(db)
        for session in stranded:
            logger.warning("session_lease_expired", session_id=session.id)
            await sessions_q.append_event(
                db,
                session,
                type="session.error",
                source="system",
                payload={"error": {"type": "worker_lost"}},
            )
            await sessions_q.release_session(
                db,
                session,
                status=IDLE,
                stop_reason={"type": "worker_lost"},
            )
        await db.commit()
        return len(stranded)


async def run_forever() -> None:
    logger.info("sweeper_started", interval=SWEEP_INTERVAL_SECONDS, lease=LEASE_SECONDS)
    while True:
        try:
            swept = await sweep_once()
            if swept:
                logger.info("sessions_swept", count=swept)
        except Exception:
            # One bad pass must not take the loop down with it.
            logger.exception("sweep_failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())
