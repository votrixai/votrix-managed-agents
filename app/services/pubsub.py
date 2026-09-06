"""Pub/Sub handoff and a bounded StreamingPull worker on one asyncio loop."""

from __future__ import annotations

import asyncio
import json
import signal
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import structlog
from google.cloud import pubsub_v1
from google.cloud.pubsub_v1.subscriber.message import Message
from google.cloud.pubsub_v1.subscriber.scheduler import ThreadScheduler

from app.config import get_settings

logger = structlog.get_logger(__name__)


@lru_cache
def publisher() -> pubsub_v1.PublisherClient:
    return pubsub_v1.PublisherClient()


async def publish_turn(*, session_id: str, generation: int) -> None:
    settings = get_settings()
    client = publisher()
    future = client.publish(
        client.topic_path(settings.pubsub_project, settings.pubsub_topic),
        json.dumps({"session_id": session_id, "generation": generation}).encode(),
    )
    await asyncio.to_thread(future.result, timeout=15)


async def handle_message(message: Message) -> None:
    from app.services.turn_execution import run_turn

    try:
        payload = json.loads(message.data)
        session_id, generation = payload["session_id"], payload["generation"]
        if (
            not isinstance(session_id, str)
            or not 1 <= len(session_id) <= 64
            or type(generation) is not int
            or generation < 0
        ):
            raise ValueError("invalid turn key")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        logger.warning("invalid_turn_message", message_id=message.message_id)
        message.ack()
        return
    try:
        await run_turn(session_id, generation)
    except BaseException:
        message.nack()
        raise
    else:
        # An active duplicate is safe to ack: the durable turn and recovery
        # scan, rather than this particular delivery, own retry responsibility.
        message.ack()


async def run_forever() -> None:
    from app.runtime.engine import aclose_checkpoint_pools
    from app.services.turn_execution import RECOVERY_SECONDS, recover_once
    from app.services.worker_pool import reconcile

    settings = get_settings()
    if settings.turn_dispatch != "pubsub":
        raise RuntimeError("StreamingPull worker requires TURN_DISPATCH=pubsub")
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    active: set[asyncio.Task] = set()
    slots = asyncio.Semaphore(settings.vma_worker_concurrency)

    async def handle(message):
        task = asyncio.current_task()
        active.add(task)
        try:
            async with slots:
                await handle_message(message)
        finally:
            active.discard(task)

    def callback(message):
        if stopped.is_set():
            message.nack()
            return
        try:
            asyncio.run_coroutine_threadsafe(handle(message), loop).result()
        except Exception:
            logger.exception("turn_delivery_failed", message_id=message.message_id)

    async def recover():
        while True:
            delay = RECOVERY_SECONDS
            try:
                if await reconcile(wake_only=True) in {"ready", "disabled"}:
                    await recover_once()
                else:
                    # Finish the cold-start handshake as soon as Cloud Run is
                    # ready; do not add a Scheduler minute to first output.
                    delay = 2
            except Exception:
                logger.exception("turn_recovery_scan_failed")
            await asyncio.sleep(delay)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    executor = ThreadPoolExecutor(max_workers=settings.vma_worker_concurrency)
    subscriber = pubsub_v1.SubscriberClient()
    stream = subscriber.subscribe(
        subscriber.subscription_path(
            settings.pubsub_project, settings.pubsub_subscription
        ),
        callback=callback,
        scheduler=ThreadScheduler(executor),
        flow_control=pubsub_v1.types.FlowControl(
            max_messages=settings.vma_worker_concurrency,
            max_bytes=1024 * 1024,
            max_lease_duration=settings.pubsub_max_lease_seconds,
            max_duration_per_lease_extension=60,
        ),
    )
    # A permanently failed pull stream must restart the container, not leave
    # an apparently healthy process that never consumes another message.
    stream.add_done_callback(lambda _: loop.call_soon_threadsafe(stopped.set))
    recovery = asyncio.create_task(recover())
    logger.info("pubsub_worker_started", concurrency=settings.vma_worker_concurrency)
    try:
        await stopped.wait()
        if stream.done():
            stream.result()
    finally:
        stopped.set()
        stream.cancel()
        recovery.cancel()
        for task in list(active):
            task.cancel()
        pending = list(active) + [recovery]
        if pending:
            await asyncio.wait(pending, timeout=7)
        subscriber.close()
        executor.shutdown(wait=False, cancel_futures=True)
        await aclose_checkpoint_pools()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
