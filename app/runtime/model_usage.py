"""Publish the usage metadata reported by OpenRouter, unchanged."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.models import events as event_types

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _usage_from_result(result: LLMResult) -> Mapping[str, Any] | None:
    for generations in result.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, Mapping):
                return usage
    return None


class OpenRouterUsageEvents(AsyncCallbackHandler):
    """Write one event for each final usage object OpenRouter returns."""

    raise_error = True
    run_inline = True

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        # Multiple read_image calls can finish concurrently, while their
        # events share one SQLAlchemy AsyncSession.
        self._write_lock = asyncio.Lock()

    async def on_llm_end(
        self,
        response: LLMResult,
        **kwargs: Any,
    ) -> None:
        del kwargs
        usage = _usage_from_result(response)
        if usage is None:
            return
        async with self._write_lock:
            await self._emit(event_types.MODEL_USAGE, {"usage": dict(usage)})


__all__ = ["OpenRouterUsageEvents"]
