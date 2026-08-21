"""Publish provider-neutral model usage, one event per completed model call.

Every supported LangChain chat integration translates its own wire response to
``AIMessage.usage_metadata``.  That is the boundary used here: VMA records the
standardized object unchanged and adds only enough call context to say what
produced it.  Funding and credential selection deliberately do not participate
in token normalization.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.models import events as event_types

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]
ModelUsageSource = Literal["agent", "tool.read_image"]


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    """Stable public context for every usage event emitted by one model."""

    model: str
    backend: str
    source: ModelUsageSource


def _usage_from_result(result: LLMResult) -> Mapping[str, Any] | None:
    for generations in result.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, Mapping):
                return usage
    return None


class ModelUsageSink:
    """Share one serialized event writer across every model in a turn."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        # Several tool calls may finish concurrently while all of their events
        # share one SQLAlchemy AsyncSession. Bound callbacks intentionally share
        # this lock instead of each owning one that cannot protect the others.
        self._write_lock = asyncio.Lock()

    def bind(
        self,
        *,
        model: str,
        backend: str,
        source: ModelUsageSource,
    ) -> "ModelUsageEvents":
        return ModelUsageEvents(
            self,
            ModelCallContext(model=model, backend=backend, source=source),
        )

    async def emit(self, context: ModelCallContext, usage: Mapping[str, Any]) -> None:
        async with self._write_lock:
            await self._emit(
                event_types.MODEL_USAGE,
                {
                    "model": context.model,
                    "backend": context.backend,
                    "source": context.source,
                    # LangChain has already normalized this. Do not split cache
                    # tokens, reconstruct totals, fill missing details, or price
                    # them here.
                    "usage": dict(usage),
                },
            )


class ModelUsageEvents(AsyncCallbackHandler):
    """Write one contextual event for each final LangChain usage object."""

    raise_error = True
    run_inline = True

    def __init__(self, sink: ModelUsageSink, context: ModelCallContext) -> None:
        self._sink = sink
        self._context = context

    async def on_llm_end(
        self,
        response: LLMResult,
        **kwargs: Any,
    ) -> None:
        del kwargs
        usage = _usage_from_result(response)
        if usage is None:
            return
        await self._sink.emit(self._context, usage)


__all__ = [
    "ModelCallContext",
    "ModelUsageEvents",
    "ModelUsageSink",
    "ModelUsageSource",
]
