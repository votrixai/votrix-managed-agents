"""Turn OpenRouter's reported usage into CMA model-request span events.

There is deliberately no tokenizer here. ``langchain-openrouter`` copies the
gateway's final usage object onto ``AIMessage.usage_metadata``; this module
only names those reported buckets the way CMA does. A request with no final
usage is recorded as an errored span with zeroes, never estimated locally.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from app.models import events as event_types

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _empty_model_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _reported_count(value: Any, *, field: str, required: bool = False) -> int:
    """Accept a count already reported by OpenRouter, without filling one in."""
    if value is None:
        if required:
            raise ValueError(f"OpenRouter usage omitted {field}")
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"OpenRouter usage returned an invalid {field}: {value!r}")
    return value


def to_cma_model_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    """Map one final OpenRouter usage report to CMA's four token buckets.

    OpenRouter's ``prompt_tokens`` becomes LangChain's ``input_tokens`` and is
    inclusive of both cache buckets. CMA's ``input_tokens`` is the uncached,
    newly processed remainder, so that is the one deterministic subtraction
    in the adapter. If the gateway ever reports contradictory buckets, fail
    the mapping instead of silently clamping or inventing a count.
    """
    input_total = _reported_count(
        usage.get("input_tokens"), field="input_tokens", required=True
    )
    output_tokens = _reported_count(
        usage.get("output_tokens"), field="output_tokens", required=True
    )
    details = usage.get("input_token_details") or {}
    if not isinstance(details, Mapping):
        raise ValueError("OpenRouter usage returned invalid input_token_details")

    cache_read = _reported_count(
        details.get("cache_read"), field="input_token_details.cache_read"
    )
    cache_creation = _reported_count(
        details.get("cache_creation"), field="input_token_details.cache_creation"
    )
    input_tokens = input_total - cache_read - cache_creation
    if input_tokens < 0:
        raise ValueError(
            "OpenRouter cache token buckets exceed its reported input token total"
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def _usage_from_result(result: LLMResult) -> Mapping[str, Any] | None:
    """Read the standardized usage from the first returned chat generation."""
    for generations in result.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, Mapping):
                return usage
    return None


class OpenRouterUsageSpans(AsyncCallbackHandler):
    """Persist one CMA start/end pair for every OpenRouter chat-model call."""

    # An event-write failure is part of the turn, especially when ``emit`` is
    # refusing a stale worker after an interrupt. LangChain otherwise logs and
    # swallows callback failures, which would leave an apparently complete but
    # unmetered model call in the event log.
    raise_error = True
    run_inline = True

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._starts: dict[UUID, str] = {}
        # Tool nodes may execute two read_image calls concurrently. SQLAlchemy
        # AsyncSession is not safe for concurrent operations, so their two
        # pairs share the same short event-write lane.
        self._write_lock = asyncio.Lock()

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        async with self._write_lock:
            event = await self._emit(event_types.SPAN_MODEL_REQUEST_START, {})
            self._starts[run_id] = str(event.id)

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        usage = _usage_from_result(response)
        try:
            model_usage = None if usage is None else to_cma_model_usage(usage)
        except (TypeError, ValueError):
            model_usage = None
        await self._finish(
            run_id,
            model_usage=model_usage or _empty_model_usage(),
            is_error=model_usage is None,
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        await self._finish(
            run_id,
            model_usage=_empty_model_usage(),
            is_error=True,
        )

    async def _finish(
        self,
        run_id: UUID,
        *,
        model_usage: dict[str, int],
        is_error: bool,
    ) -> None:
        async with self._write_lock:
            start_id = self._starts.pop(run_id, None)
            if start_id is None:
                # Defensive only: LangChain normally guarantees start before
                # end. Keeping a valid CMA pair is safer than an uncorrelated
                # end if a future callback implementation changes that order.
                start = await self._emit(event_types.SPAN_MODEL_REQUEST_START, {})
                start_id = str(start.id)
            await self._emit(
                event_types.SPAN_MODEL_REQUEST_END,
                {
                    "model_request_start_id": start_id,
                    "model_usage": model_usage,
                    "is_error": is_error,
                },
            )


__all__ = ["OpenRouterUsageSpans", "to_cma_model_usage"]
