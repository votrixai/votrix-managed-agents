"""OpenRouter usage is published without VMA reclassifying it."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.models import events as event_types
from app.runtime.model_usage import OpenRouterUsageEvents


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


async def test_openrouter_usage_is_emitted_unchanged():
    reported = {
        "input_tokens": 10_000,
        "output_tokens": 184,
        "total_tokens": 10_184,
        "input_token_details": {
            "cache_creation": 1_200,
            "cache_read": 8_400,
        },
    }
    log = EventLog()
    events = OpenRouterUsageEvents(log.emit)

    await events.on_llm_end(result_with_usage(reported), run_id=uuid4())

    assert log.events == [
        {
            "type": "model.usage",
            "id": "evt_1",
            "usage": reported,
        }
    ]
    # In particular, VMA does not turn the reported 10,000 into 400.
    assert log.events[0]["usage"]["input_tokens"] == 10_000


async def test_no_final_usage_means_no_invented_usage_event():
    log = EventLog()
    events = OpenRouterUsageEvents(log.emit)

    await events.on_llm_end(result_with_usage(None), run_id=uuid4())
    await events.on_llm_error(RuntimeError("provider failed"), run_id=uuid4())

    assert log.events == []


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
            payload={"usage": reported},
        )
    )

    assert event.model_dump(mode="json") == {
        "id": "evt_usage",
        "seq": 7,
        "processed_at": "2026-08-19T12:00:00Z",
        "type": "model.usage",
        "usage": reported,
    }
