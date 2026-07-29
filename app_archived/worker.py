from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Any

import structlog

from app.config import get_settings
from app.db.engine import session_scope
from app.logging import setup as setup_logging
from app.runtime.turn_limiter import TurnLimiter
from app.runtime.work_queue import execute_work_item, lease_next_work_for_worker

logger = structlog.get_logger()


async def run_worker(
    *,
    environment_id: str | None,
    poll_interval_seconds: float,
    once: bool,
    worker_id: str = "vma-worker",
    lease_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
    dispatch_mode: str | None = None,
    turn_limiter: TurnLimiter | None = None,
) -> None:
    owns_stop_event = stop_event is None
    stop_event = stop_event or asyncio.Event()
    effective_dispatch_mode = dispatch_mode or get_settings().vma_work_dispatch_mode
    if effective_dispatch_mode not in {"poll", "hybrid"}:
        raise ValueError(f"Unsupported work dispatch mode: {effective_dispatch_mode}")
    if effective_dispatch_mode == "hybrid" and turn_limiter is None:
        turn_limiter = TurnLimiter(get_settings().vma_worker_turn_limit)
    if owns_stop_event:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

    while not stop_event.is_set():
        limiter_acquired = False
        try:
            if effective_dispatch_mode == "hybrid":
                assert turn_limiter is not None
                limiter_acquired = turn_limiter.acquire_nowait()
                if not limiter_acquired:
                    if once:
                        return
                    await asyncio.sleep(poll_interval_seconds)
                    continue
            work = await _next_runnable_work(
                environment_id=environment_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if work is None:
                if limiter_acquired:
                    assert turn_limiter is not None
                    turn_limiter.release()
                    limiter_acquired = False
                if once:
                    return
                await asyncio.sleep(poll_interval_seconds)
                continue
            logger.info("worker_executing_work", work_id=work["id"], session_id=work.get("session_id"))
            await execute_work_item(
                work["id"],
                worker_id=worker_id,
                lease_id=str(work["lease"]["lease_id"]),
                lease_generation=int(work["lease"]["generation"]),
                lease_seconds=lease_seconds,
            )
            if once:
                return
        except Exception:
            if limiter_acquired:
                assert turn_limiter is not None
                turn_limiter.release()
                limiter_acquired = False
            if once:
                raise
            logger.exception(
                "worker_loop_iteration_failed",
                worker_id=worker_id,
                environment_id=environment_id,
            )
            await asyncio.sleep(poll_interval_seconds)
            continue
        finally:
            if limiter_acquired:
                assert turn_limiter is not None
                turn_limiter.release()


async def _next_runnable_work(
    *,
    environment_id: str | None,
    worker_id: str = "vma-worker",
    lease_seconds: int = 60,
) -> dict[str, Any] | None:
    async with session_scope() as db:
        work = await lease_next_work_for_worker(
            db,
            environment_id=environment_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        await db.commit()
        return _work_dict(work)


def _work_dict(work) -> dict[str, Any] | None:
    if work is None:
        return None
    return {"id": work.id, "status": work.status, **(work.data or {})}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Votrix Managed Agents Postgres queue worker.")
    parser.add_argument("--environment-id", default=None)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--worker-id", default="vma-worker")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    setup_logging(app_env="worker", sentry_dsn="", log_level="INFO")
    asyncio.run(
        run_worker(
            environment_id=args.environment_id,
            poll_interval_seconds=args.poll_interval,
            once=args.once,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
        )
    )


if __name__ == "__main__":
    main()
