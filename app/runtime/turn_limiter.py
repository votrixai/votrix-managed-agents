from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class TurnLimiter:
    """Bound concurrent turns shared by push requests and the reconciler."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("turn limit must be at least one")
        self._limit = limit
        self._semaphore = asyncio.BoundedSemaphore(limit)

    @property
    def limit(self) -> int:
        return self._limit

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Wait for capacity and hold it for the duration of the context."""

        await self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()

    def acquire_nowait(self) -> bool:
        """Reserve capacity without waiting.

        asyncio.Semaphore does not expose acquire_nowait. This synchronous
        check-and-decrement is atomic with respect to other tasks on the same
        event loop because it contains no suspension point.
        """

        if self._semaphore.locked():
            return False
        self._semaphore._value -= 1
        return True

    def release(self) -> None:
        self._semaphore.release()
