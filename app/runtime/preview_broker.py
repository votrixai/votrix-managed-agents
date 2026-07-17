"""Best-effort cross-instance transport for ephemeral VMA preview frames.

Local delivery always happens first through :mod:`vma_preview_bus`. Hosted API
and worker processes can additionally exchange the same frames through Postgres
``NOTIFY`` without changing the public SSE contract or its durable reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import structlog
from sqlalchemy import text

from app.config import get_settings
from app.db.engine import get_engine
from app.organization import resolve_organization_id
from app.runtime.vma_preview_bus import VmaProcessLocalPreviewBus, vma_preview_bus

logger = structlog.get_logger()

_CHANNEL = "vma_preview"
_MAX_PAYLOAD_BYTES = 7_500
_MAX_BUFFER_BYTES = 4 * 1_024
_FLUSH_AFTER_SECONDS = 0.050
_FLUSH_TICK_SECONDS = 0.025
PREVIEW_INSTANCE_ID = uuid4().hex

PreviewBrokerMode = Literal["process_local", "pg_notify"]
PreviewServiceRole = Literal["combined", "api", "worker"]
NotifySink = Callable[[str], Awaitable[None]]
Topic = tuple[str, str]


@dataclass(slots=True)
class _PendingFrame:
    frame: dict[str, Any]
    enqueued_at: float
    size_bytes: int


@dataclass(slots=True)
class _TopicState:
    pending: deque[_PendingFrame] = field(default_factory=deque)
    size_bytes: int = 0
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PreviewBroker:
    """Local-first preview publisher with optional Postgres fan-out."""

    def __init__(
        self,
        *,
        instance_id: str = PREVIEW_INSTANCE_ID,
        local_bus: VmaProcessLocalPreviewBus = vma_preview_bus,
        mode: PreviewBrokerMode | None = None,
        database_url: str | None = None,
        service_role: PreviewServiceRole | None = None,
        notify_sink: NotifySink | None = None,
        flush_after_seconds: float = _FLUSH_AFTER_SECONDS,
        flush_tick_seconds: float = _FLUSH_TICK_SECONDS,
        max_buffer_bytes: int = _MAX_BUFFER_BYTES,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        connection_check_seconds: float = 1.0,
    ) -> None:
        self.instance_id = instance_id
        self.local_bus = local_bus
        self._mode_override = mode
        self._database_url_override = database_url
        self._service_role_override = service_role
        self._notify_sink = notify_sink or self._notify_via_engine
        self._flush_after_seconds = flush_after_seconds
        self._flush_tick_seconds = flush_tick_seconds
        self._max_buffer_bytes = max_buffer_bytes
        self._max_payload_bytes = max_payload_bytes
        self._reconnect_initial_seconds = reconnect_initial_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._connection_check_seconds = connection_check_seconds

        self._topics: dict[Topic, _TopicState] = {}
        self._topics_lock = asyncio.Lock()
        self._flush_wake = asyncio.Event()
        self._flusher_stop = asyncio.Event()
        self._listener_stop = asyncio.Event()
        self._listener_ready = asyncio.Event()
        self._flusher_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_connection: Any | None = None
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self._started = False

    @property
    def listener_task(self) -> asyncio.Task[None] | None:
        """Return the owned listener task for lifecycle diagnostics and tests."""
        return self._listener_task

    def validate_configuration(self) -> None:
        """Fail fast when a Postgres-only broker is paired with another database."""
        if self._mode() == "pg_notify" and not _is_postgres_url(self._database_url()):
            raise RuntimeError("VMA_PREVIEW_BROKER=pg_notify requires a PostgreSQL DATABASE_URL")

    async def start(self) -> asyncio.Task[None] | None:
        """Start coalescing and, for API roles, the dedicated LISTEN connection."""
        self.validate_configuration()
        if self._started:
            return self._listener_task
        self._started = True
        if self._mode() != "pg_notify":
            return None
        self._ensure_flusher_started()
        if self._service_role() in {"combined", "api"}:
            self._listener_stop.clear()
            self._listener_task = asyncio.create_task(
                self._listen_forever(),
                name=f"vma-preview-listener-{self.instance_id}",
            )
        return self._listener_task

    async def close(self) -> None:
        """Flush pending frames and close every broker-owned task and connection."""
        flusher_task, self._flusher_task = self._flusher_task, None
        self._flusher_stop.set()
        self._flush_wake.set()
        if flusher_task is not None:
            with suppress(asyncio.CancelledError):
                await flusher_task
        await self.flush_pending()

        listener_task, self._listener_task = self._listener_task, None
        self._listener_stop.set()
        if listener_task is not None:
            listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await listener_task
        await self._close_listener_connection()

        notification_tasks = tuple(self._notification_tasks)
        for task in notification_tasks:
            task.cancel()
        if notification_tasks:
            await asyncio.gather(*notification_tasks, return_exceptions=True)
        self._notification_tasks.clear()
        self._listener_ready.clear()
        self._started = False

    async def publish(
        self,
        session_id: str,
        frame: Mapping[str, Any],
        *,
        organization_id: str | None,
    ) -> int:
        """Deliver locally first, then enqueue one best-effort remote preview."""
        scoped_organization_id = resolve_organization_id(organization_id)
        delivered = await self.local_bus.publish(
            session_id,
            frame,
            organization_id=scoped_organization_id,
        )
        if self._mode() != "pg_notify":
            return delivered

        self.validate_configuration()
        self._ensure_flusher_started()
        await self._enqueue_remote(
            (scoped_organization_id, session_id),
            deepcopy(dict(frame)),
        )
        return delivered

    async def flush_pending(self) -> None:
        """Flush every currently buffered topic without reordering its frames."""
        while True:
            async with self._topics_lock:
                topics = tuple(self._topics)
            if not topics:
                return
            for topic in topics:
                await self._flush_topic(topic)

    async def handle_notification(self, raw_payload: str) -> bool:
        """Republish one foreign NOTIFY payload into this process-local bus."""
        try:
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                raise TypeError("preview notification must be an object")
            if payload.get("i") == self.instance_id:
                return False
            organization_id = payload.get("o")
            session_id = payload.get("s")
            frame = payload.get("f")
            if not isinstance(organization_id, str) or not organization_id:
                raise TypeError("preview notification organization is invalid")
            if not isinstance(session_id, str) or not session_id:
                raise TypeError("preview notification session is invalid")
            if not isinstance(frame, dict):
                raise TypeError("preview notification frame is invalid")
            await self.local_bus.publish(
                session_id,
                frame,
                organization_id=organization_id,
            )
            return True
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.debug("preview_notification_invalid", payload=raw_payload)
            return False

    async def wait_until_listener_ready(self, *, timeout: float = 10.0) -> bool:
        """Wait for LISTEN registration; intended for integration health checks."""
        try:
            await asyncio.wait_for(self._listener_ready.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def _enqueue_remote(self, topic: Topic, frame: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        try:
            frame_size = len(_json_bytes(frame))
        except (TypeError, ValueError):
            logger.debug(
                "preview_notification_not_json_serializable",
                organization_id=topic[0],
                session_id=topic[1],
            )
            return
        async with self._topics_lock:
            state = self._topics.setdefault(topic, _TopicState())
            state.pending.append(
                _PendingFrame(
                    frame=frame,
                    enqueued_at=loop.time(),
                    size_bytes=frame_size,
                )
            )
            state.size_bytes += frame_size
            flush_now = frame.get("type") == "event_start" or state.size_bytes >= self._max_buffer_bytes
        self._flush_wake.set()
        if flush_now:
            await self._flush_topic(topic)

    def _ensure_flusher_started(self) -> None:
        if self._flusher_task is not None and not self._flusher_task.done():
            return
        self._flusher_stop.clear()
        self._flusher_task = asyncio.create_task(
            self._flush_loop(),
            name=f"vma-preview-flusher-{self.instance_id}",
        )

    async def _flush_loop(self) -> None:
        while not self._flusher_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._flush_wake.wait(),
                    timeout=self._flush_tick_seconds,
                )
            except TimeoutError:
                pass
            self._flush_wake.clear()
            if self._flusher_stop.is_set():
                return
            try:
                await self._flush_due_topics()
            except Exception:
                logger.exception("preview_coalescer_flush_failed")

    async def _flush_due_topics(self) -> None:
        now = asyncio.get_running_loop().time()
        async with self._topics_lock:
            due = tuple(
                topic
                for topic, state in self._topics.items()
                if state.pending
                and (
                    now - state.pending[0].enqueued_at >= self._flush_after_seconds
                    or state.size_bytes >= self._max_buffer_bytes
                )
            )
        for topic in due:
            await self._flush_topic(topic)

    async def _flush_topic(self, topic: Topic) -> None:
        async with self._topics_lock:
            state = self._topics.get(topic)
        if state is None:
            return

        async with state.flush_lock:
            async with self._topics_lock:
                if self._topics.get(topic) is not state or not state.pending:
                    return
                pending = tuple(state.pending)
                state.pending.clear()
                state.size_bytes = 0

            organization_id, session_id = topic
            for frame in _merge_adjacent_text_deltas(item.frame for item in pending):
                await self._publish_remote_frame(
                    organization_id=organization_id,
                    session_id=session_id,
                    frame=frame,
                )

            async with self._topics_lock:
                if self._topics.get(topic) is state and not state.pending:
                    self._topics.pop(topic, None)

    async def _publish_remote_frame(
        self,
        *,
        organization_id: str,
        session_id: str,
        frame: dict[str, Any],
    ) -> None:
        envelope = {
            "i": self.instance_id,
            "o": organization_id,
            "s": session_id,
            "f": frame,
        }
        try:
            raw_payload = _json_bytes(envelope)
        except (TypeError, ValueError):
            logger.debug(
                "preview_notification_not_json_serializable",
                organization_id=organization_id,
                session_id=session_id,
            )
            return
        if len(raw_payload) > self._max_payload_bytes:
            logger.debug(
                "preview_notification_oversized",
                organization_id=organization_id,
                session_id=session_id,
                payload_bytes=len(raw_payload),
                max_payload_bytes=self._max_payload_bytes,
            )
            return
        try:
            await self._notify_sink(raw_payload.decode("utf-8"))
        except Exception:
            logger.exception(
                "preview_notification_publish_failed",
                organization_id=organization_id,
                session_id=session_id,
            )

    async def _notify_via_engine(self, raw_payload: str) -> None:
        async with get_engine().begin() as connection:
            await connection.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": _CHANNEL, "payload": raw_payload},
            )

    async def _listen_forever(self) -> None:
        import asyncpg

        backoff = self._reconnect_initial_seconds
        while not self._listener_stop.is_set():
            connection = None
            try:
                connection = await asyncpg.connect(
                    _asyncpg_dsn(self._database_url()),
                    statement_cache_size=0,
                    command_timeout=30,
                    timeout=10,
                )
                self._listener_connection = connection
                await connection.add_listener(_CHANNEL, self._listener_callback)
                self._listener_ready.set()
                backoff = self._reconnect_initial_seconds
                logger.info("preview_listener_connected", instance_id=self.instance_id)
                while not self._listener_stop.is_set() and not connection.is_closed():
                    try:
                        await asyncio.wait_for(
                            self._listener_stop.wait(),
                            timeout=self._connection_check_seconds,
                        )
                    except TimeoutError:
                        pass
                if not self._listener_stop.is_set():
                    raise ConnectionError("PostgreSQL preview listener connection closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "preview_listener_connection_failed",
                    instance_id=self.instance_id,
                    retry_in_seconds=backoff,
                )
            finally:
                self._listener_ready.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.remove_listener(_CHANNEL, self._listener_callback)
                    with suppress(Exception):
                        await connection.close()
                if self._listener_connection is connection:
                    self._listener_connection = None

            if self._listener_stop.is_set():
                return
            try:
                await asyncio.wait_for(self._listener_stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self._reconnect_max_seconds, max(backoff * 2, self._reconnect_initial_seconds))

    def _listener_callback(
        self,
        _connection: Any,
        _process_id: int,
        _channel: str,
        raw_payload: str,
    ) -> None:
        task = asyncio.create_task(
            self.handle_notification(raw_payload),
            name=f"vma-preview-notification-{self.instance_id}",
        )
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_task_done)

    def _notification_task_done(self, task: asyncio.Task[Any]) -> None:
        self._notification_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "preview_notification_handler_failed",
                instance_id=self.instance_id,
                error=str(exception),
            )

    async def _close_listener_connection(self) -> None:
        connection, self._listener_connection = self._listener_connection, None
        if connection is not None and not connection.is_closed():
            with suppress(Exception):
                await connection.close()

    def _mode(self) -> PreviewBrokerMode:
        return self._mode_override or get_settings().vma_preview_broker

    def _database_url(self) -> str:
        return self._database_url_override or get_settings().database_url

    def _service_role(self) -> PreviewServiceRole:
        return self._service_role_override or get_settings().vma_service_role


def _merge_adjacent_text_deltas(frames: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source_frame in frames:
        frame = deepcopy(dict(source_frame))
        if merged and _can_merge_text_delta(merged[-1], frame):
            previous_text = merged[-1]["delta"]["content"]["text"]
            merged[-1]["delta"]["content"]["text"] = previous_text + frame["delta"]["content"]["text"]
        else:
            merged.append(frame)
    return merged


def _can_merge_text_delta(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    if previous.get("type") != "event_delta" or current.get("type") != "event_delta":
        return False
    if previous.get("event_id") != current.get("event_id"):
        return False
    previous_delta = previous.get("delta")
    current_delta = current.get("delta")
    if not isinstance(previous_delta, Mapping) or not isinstance(current_delta, Mapping):
        return False
    if previous_delta.get("index") != current_delta.get("index"):
        return False
    previous_content = previous_delta.get("content")
    current_content = current_delta.get("content")
    if not isinstance(previous_content, Mapping) or not isinstance(current_content, Mapping):
        return False
    return isinstance(previous_content.get("text"), str) and isinstance(current_content.get("text"), str)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_postgres_url(value: str) -> bool:
    return value.startswith(("postgres://", "postgresql://", "postgresql+"))


def _asyncpg_dsn(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


preview_broker = PreviewBroker()


async def publish_preview(
    session_id: str,
    frame: Mapping[str, Any],
    *,
    organization_id: str | None = None,
) -> int:
    """Publish through the process singleton used by the runtime."""
    return await preview_broker.publish(
        session_id,
        frame,
        organization_id=organization_id,
    )


async def start_preview_broker() -> asyncio.Task[None] | None:
    """Start the process singleton; called once from the application lifespan."""
    return await preview_broker.start()


async def close_preview_broker() -> None:
    """Flush and stop the process singleton; called once during lifespan shutdown."""
    await preview_broker.close()


def validate_preview_broker_configuration() -> None:
    """Validate the process singleton without starting background tasks."""
    preview_broker.validate_configuration()


__all__ = [
    "PREVIEW_INSTANCE_ID",
    "PreviewBroker",
    "close_preview_broker",
    "preview_broker",
    "publish_preview",
    "start_preview_broker",
    "validate_preview_broker_configuration",
]
