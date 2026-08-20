"""OpenRouter is the only token source; VMA only renames its buckets."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.models import events as event_types
from app.runtime.model_usage import OpenRouterUsageSpans, to_cma_model_usage

ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


class EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, event_type: str, payload: dict) -> SimpleNamespace:
        event_id = f"evt_{len(self.events) + 1}"
        self.events.append({"type": event_type, "id": event_id, **payload})
        return SimpleNamespace(id=event_id)


def result_with_usage(usage: dict | None) -> LLMResult:
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(content="done", usage_metadata=usage)
                )
            ]
        ]
    )


def test_openrouter_prompt_total_is_split_into_cma_input_buckets():
    mapped = to_cma_model_usage(
        {
            "input_tokens": 10_000,
            "output_tokens": 184,
            "input_token_details": {
                "cache_creation": 1_200,
                "cache_read": 8_400,
            },
        }
    )

    assert mapped == {
        "input_tokens": 400,
        "output_tokens": 184,
        "cache_creation_input_tokens": 1_200,
        "cache_read_input_tokens": 8_400,
    }
    assert (
        mapped["input_tokens"]
        + mapped["cache_creation_input_tokens"]
        + mapped["cache_read_input_tokens"]
        == 10_000
    )


def test_missing_openrouter_cache_details_are_zero_not_estimated():
    assert to_cma_model_usage({"input_tokens": 320, "output_tokens": 184}) == {
        "input_tokens": 320,
        "output_tokens": 184,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_contradictory_openrouter_buckets_are_not_silently_clamped():
    with pytest.raises(ValueError, match="exceed"):
        to_cma_model_usage(
            {
                "input_tokens": 10,
                "output_tokens": 1,
                "input_token_details": {"cache_read": 11},
            }
        )


async def test_a_completed_request_emits_the_cma_span_pair():
    log = EventLog()
    spans = OpenRouterUsageSpans(log.emit)
    run_id = uuid4()

    await spans.on_chat_model_start({}, [[]], run_id=run_id)
    await spans.on_llm_end(
        result_with_usage(
            {
                "input_tokens": 10_000,
                "output_tokens": 184,
                "total_tokens": 10_184,
                "input_token_details": {
                    "cache_creation": 1_200,
                    "cache_read": 8_400,
                },
            }
        ),
        run_id=run_id,
    )

    assert log.events == [
        {"type": "span.model_request_start", "id": "evt_1"},
        {
            "type": "span.model_request_end",
            "id": "evt_2",
            "model_request_start_id": "evt_1",
            "model_usage": {
                "input_tokens": 400,
                "output_tokens": 184,
                "cache_creation_input_tokens": 1_200,
                "cache_read_input_tokens": 8_400,
            },
            "is_error": False,
        },
    ]


@pytest.mark.parametrize("ending", ["missing_usage", "request_error"])
async def test_a_request_without_final_usage_emits_an_error_with_zeroes(ending):
    log = EventLog()
    spans = OpenRouterUsageSpans(log.emit)
    run_id = uuid4()
    await spans.on_chat_model_start({}, [[]], run_id=run_id)

    if ending == "missing_usage":
        await spans.on_llm_end(result_with_usage(None), run_id=run_id)
    else:
        await spans.on_llm_error(RuntimeError("provider failed"), run_id=run_id)

    assert log.events[-1] == {
        "type": "span.model_request_end",
        "id": "evt_2",
        "model_request_start_id": "evt_1",
        "model_usage": ZERO_USAGE,
        "is_error": True,
    }


def test_stored_span_events_read_back_as_the_cma_shapes():
    created_at = "2026-08-19T12:00:00Z"
    start = event_types.from_row(
        SimpleNamespace(
            type="span.model_request_start",
            id="evt_start",
            seq=7,
            created_at=created_at,
            payload={},
        )
    )
    end = event_types.from_row(
        SimpleNamespace(
            type="span.model_request_end",
            id="evt_end",
            seq=8,
            created_at=created_at,
            payload={
                "model_request_start_id": "evt_start",
                "model_usage": ZERO_USAGE,
                "is_error": False,
            },
        )
    )

    assert start.model_dump(mode="json") == {
        "id": "evt_start",
        "seq": 7,
        "processed_at": created_at,
        "type": "span.model_request_start",
    }
    assert end.model_dump(mode="json") == {
        "id": "evt_end",
        "seq": 8,
        "processed_at": created_at,
        "type": "span.model_request_end",
        "model_request_start_id": "evt_start",
        "model_usage": ZERO_USAGE,
        "is_error": False,
    }
