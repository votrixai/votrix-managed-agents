"""Best-effort, ephemeral preview delivery for VMA session streams.

This bus is deliberately process-local: it connects a runtime and SSE clients only
when they live in the same Python process. A production deployment with separate web
and worker processes still needs a tenant-scoped cross-process preview broker (for
example Redis Streams or NATS). Durable session events remain the source of truth and
are replayed from the database independently of this bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

from app.workspace import workspace_id_or_default

VMA_PREVIEW_FRAME_TYPES = frozenset({"event_start", "event_delta"})


class VmaProcessLocalPreviewBus:
    """Fan out transient preview frames to live subscribers in this process."""

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        session_id: str,
        frame: Mapping[str, Any],
        *,
        workspace_id: str | None = None,
    ) -> int:
        """Publish one exact runtime frame and return the subscriber delivery count."""
        payload = _validated_frame(frame)
        topic = (workspace_id_or_default(workspace_id), session_id)
        async with self._lock:
            subscribers = tuple(self._subscribers.get(topic, ()))

        delivered = 0
        for queue in subscribers:
            try:
                queue.put_nowait(deepcopy(payload))
            except asyncio.QueueFull:
                # Previews are explicitly best-effort. The durable buffered event is
                # still replayed from the database and reconciles any dropped preview.
                continue
            delivered += 1
        return delivered

    @asynccontextmanager
    async def subscribe(
        self,
        session_id: str,
        *,
        workspace_id: str | None = None,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Subscribe to frames published after this context is entered."""
        topic = (workspace_id_or_default(workspace_id), session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            self._subscribers.setdefault(topic, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(topic)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(topic, None)

    async def subscriber_count(
        self,
        session_id: str,
        *,
        workspace_id: str | None = None,
    ) -> int:
        """Return the number of live subscribers for diagnostics and tests."""
        topic = (workspace_id_or_default(workspace_id), session_id)
        async with self._lock:
            return len(self._subscribers.get(topic, ()))


def _validated_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(frame)
    frame_type = payload.get("type")
    if frame_type not in VMA_PREVIEW_FRAME_TYPES:
        raise ValueError("VMA preview frames must have type event_start or event_delta")
    return deepcopy(payload)


vma_preview_bus = VmaProcessLocalPreviewBus()


async def publish_vma_preview(
    session_id: str,
    frame: Mapping[str, Any],
    *,
    workspace_id: str | None = None,
) -> int:
    """Publish a runtime preview through the shared process-local VMA bus."""
    return await vma_preview_bus.publish(session_id, frame, workspace_id=workspace_id)


__all__ = [
    "VMA_PREVIEW_FRAME_TYPES",
    "VmaProcessLocalPreviewBus",
    "publish_vma_preview",
    "vma_preview_bus",
]
