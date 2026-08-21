"""Every chat backend publishes one provider-neutral usage event shape."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.models import events as event_types
from app.runtime.model_usage import ModelUsageSink


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


@pytest.mark.parametrize(
    ("backend", "model", "reported"),
    [
        (
            "openrouter",
            "claude-sonnet-5",
            {
                "input_tokens": 10_000,
                "output_tokens": 184,
                "total_tokens": 10_184,
                "input_token_details": {
                    "cache_creation": 1_200,
                    "cache_read": 8_400,
                },
            },
        ),
        (
            "anthropic",
            "claude-sonnet-5",
            {
                "input_tokens": 10_000,
                "output_tokens": 184,
                "total_tokens": 10_184,
                "input_token_details": {
                    "cache_creation": 0,
                    "cache_read": 8_400,
                    "ephemeral_5m_input_tokens": 1_200,
                },
            },
        ),
        (
            "google",
            "gemini-3.6-flash",
            {
                "input_tokens": 320,
                "output_tokens": 264,
                "total_tokens": 584,
                "input_token_details": {"cache_read": 200},
                "output_token_details": {"reasoning": 80},
            },
        ),
        (
            "openai",
            "gpt-5.6-sol",
            {
                "input_tokens": 320,
                "output_tokens": 184,
                "total_tokens": 504,
            },
        ),
        (
            "deepseek",
            "deepseek-v4-pro",
            {
                "input_tokens": 640,
                "output_tokens": 96,
                "total_tokens": 736,
                "output_token_details": {"reasoning": 40},
            },
        ),
    ],
    ids=["openrouter", "anthropic", "google", "openai", "deepseek"],
)
async def test_langchain_usage_from_each_backend_is_emitted_unchanged(
    backend, model, reported
):
    """The fixtures are adapter-normalized UsageMetadata, not raw wire bodies."""

    log = EventLog()
    events = ModelUsageSink(log.emit).bind(
        model=model,
        backend=backend,
        source="agent",
    )

    await events.on_llm_end(result_with_usage(reported), run_id=uuid4())

    assert log.events == [
        {
            "type": "model.usage",
            "id": "evt_1",
            "model": model,
            "backend": backend,
            "source": "agent",
            "usage": reported,
        }
    ]


async def test_no_final_usage_means_no_invented_usage_event():
    log = EventLog()
    events = ModelUsageSink(log.emit).bind(
        model="claude-sonnet-5",
        backend="anthropic",
        source="agent",
    )

    await events.on_llm_end(result_with_usage(None), run_id=uuid4())
    await events.on_llm_error(RuntimeError("provider failed"), run_id=uuid4())

    assert log.events == []


async def test_bound_callbacks_share_one_serialized_writer():
    active_writes = 0
    most_active_writes = 0

    async def emit(_event_type: str, _payload: dict) -> None:
        nonlocal active_writes, most_active_writes
        active_writes += 1
        most_active_writes = max(most_active_writes, active_writes)
        await asyncio.sleep(0)
        active_writes -= 1

    sink = ModelUsageSink(emit)
    main = sink.bind(model="claude-sonnet-5", backend="anthropic", source="agent")
    image = sink.bind(
        model="gemini-3.6-flash",
        backend="google",
        source="tool.read_image",
    )

    await asyncio.gather(
        main.on_llm_end(
            result_with_usage(
                {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
            ),
            run_id=uuid4(),
        ),
        image.on_llm_end(
            result_with_usage(
                {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23}
            ),
            run_id=uuid4(),
        ),
    )

    assert most_active_writes == 1


def test_stored_usage_reads_back_with_the_same_payload():
    reported = {
        "input_tokens": 320,
        "output_tokens": 184,
        "total_tokens": 504,
    }
    event = event_types.from_row(
        SimpleNamespace(
            type="model.usage",
            id="evt_usage",
            seq=7,
            created_at="2026-08-19T12:00:00Z",
            payload={
                "model": "gemini-3.6-flash",
                "backend": "google",
                "source": "tool.read_image",
                "usage": reported,
            },
        )
    )

    assert event.model_dump(mode="json") == {
        "id": "evt_usage",
        "seq": 7,
        "processed_at": "2026-08-19T12:00:00Z",
        "type": "model.usage",
        "model": "gemini-3.6-flash",
        "backend": "google",
        "source": "tool.read_image",
        "usage": reported,
    }


def test_usage_rows_from_before_backend_attribution_remain_readable():
    event = event_types.from_row(
        SimpleNamespace(
            type="model.usage",
            id="evt_legacy_usage",
            seq=3,
            created_at="2026-08-19T12:00:00Z",
            payload={
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "total_tokens": 16,
                }
            },
        )
    )

    assert event.model == "unknown"
    assert event.backend == "openrouter"
    assert event.source == "legacy"
