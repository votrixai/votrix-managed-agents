"""Timing probes.

One helper, used everywhere, so that every latency line in the logs has the
same shape and a turn can be added up from them:

    event            what was being done
    duration_ms      how long it took
    outcome          ok | error
    session_id       what ties one turn's lines together

Everything is logged unconditionally rather than above a threshold. These
exist to establish a baseline, and a threshold hides exactly the thing a
baseline is for — how long the normal case takes. Once the normal case is
known, the ones that turn out to be uninteresting can be dropped or raised to
a threshold; guessing which those are before measuring is how the service
ended up with one latency log, on its cheapest component.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog

logger = structlog.get_logger("app.timing")


def elapsed_ms(started_at: float) -> float:
    """Milliseconds since a `time.monotonic()` reading, one decimal place."""
    return round((time.monotonic() - started_at) * 1000, 1)


def report(event: str, started_at: float, **fields: Any) -> None:
    """Log one finished interval. For places that cannot hold a `with` block —
    a pair of callbacks, say, where the start and the end are different
    functions."""
    logger.info(event, duration_ms=elapsed_ms(started_at), **fields)


@asynccontextmanager
async def timed(event: str, **fields: Any) -> AsyncIterator[dict[str, Any]]:
    """Time a block and log it, whether it returns or raises.

    Yields a dict for anything only knowable once the work is done — how many
    rows came back, whether a connection was reused. Fields set on it are
    merged into the log line:

        async with timed("sandbox_connected", session_id=x) as span:
            span["reused"] = ...
    """
    span: dict[str, Any] = {}
    started_at = time.monotonic()
    try:
        yield span
    except BaseException as exc:
        logger.warning(
            event,
            duration_ms=elapsed_ms(started_at),
            outcome="error",
            error_type=type(exc).__name__,
            **{**fields, **span},
        )
        raise
    logger.info(
        event,
        duration_ms=elapsed_ms(started_at),
        outcome="ok",
        **{**fields, **span},
    )


__all__ = ["elapsed_ms", "report", "timed"]
