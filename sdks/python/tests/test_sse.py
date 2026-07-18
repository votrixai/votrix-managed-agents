from __future__ import annotations

import asyncio

import httpx
import pytest

from votrix import APIStreamError, AsyncVotrix


def make_client(body: str):
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/sessions/session_1/events/stream"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncVotrix(
        api_key="vma_test_sse",
        base_url="https://vma.test",
        max_retries=0,
        http_client=http_client,
    )
    return sdk, http_client


@pytest.mark.asyncio
async def test_stream_exposes_cma_style_event_attributes():
    sdk, http_client = make_client(
        'id: 7\nevent: agent.message\ndata: {"id":"event_7","type":"agent.message","session_id":"session_1","seq":7,"content":"hello","future":true}\n\n'
    )
    async with await sdk.sessions.events.stream("session_1") as stream:
        events = [event async for event in stream]
    assert len(events) == 1
    event = events[0]
    assert event.type == "agent.message"
    assert event.seq == 7
    assert event.id == "event_7"
    assert event.sse_id == "7"
    assert event.event == "agent.message"
    assert event.sse_event == "agent.message"
    assert event.future is True
    assert event.data["content"] == "hello"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_accepts_structured_preview_frames():
    sdk, http_client = make_client(
        'event: event_start\ndata: {"type":"event_start","event":{"type":"agent.message","id":"event_7"}}\n\n'
        'event: event_delta\ndata: {"type":"event_delta","event_id":"event_7","delta":{"type":"content_delta","index":0,"content":{"type":"text","text":"hello"}}}\n\n'
    )
    async with await sdk.sessions.events.stream(
        "session_1",
        event_deltas=["agent.message"],
    ) as stream:
        events = [event async for event in stream]

    assert [event.type for event in events] == ["event_start", "event_delta"]
    assert events[0].event == {"type": "agent.message", "id": "event_7"}
    assert events[0].sse_event == "event_start"
    assert events[1].event == "event_delta"
    assert events[1].sse_event == "event_delta"
    assert events[1].event_id == "event_7"
    assert events[1].delta["content"]["text"] == "hello"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_flat_error_frame_raises_structured_stream_error():
    sdk, http_client = make_client(
        'id: request_1\nevent: error\ndata: {"type":"not_found_error","message":"Session not found"}\n\n'
    )
    with pytest.raises(APIStreamError, match="Session not found") as caught:
        async with await sdk.sessions.events.stream("session_1") as stream:
            async for _event in stream:
                pass
    assert caught.value.error_type == "not_found_error"
    assert caught.value.request_id == "request_1"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_reconnects_with_last_event_id_and_deduplicates():
    last_event_ids: list[str | None] = []

    def handler(request: httpx.Request):
        last_event_ids.append(request.headers.get("last-event-id"))
        if len(last_event_ids) == 1:
            body = 'id: 7\nretry: 0\nevent: agent.message\ndata: {"id":"event_7","type":"agent.message","seq":7}\n\n'
        else:
            body = (
                'id: 7\nevent: agent.message\ndata: {"id":"event_7","type":"agent.message","seq":7}\n\n'
                'id: 8\nevent: agent.message\ndata: {"id":"event_8","type":"agent.message","seq":8}\n\n'
            )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncVotrix(
        api_key="vma_test_sse",
        base_url="https://vma.test",
        max_retries=0,
        http_client=http_client,
    )
    async with await sdk.sessions.events.stream("session_1", max_reconnects=1) as stream:
        events = [event async for event in stream]
    assert [event.sse_id for event in events] == ["7", "8"]
    assert last_event_ids == [None, "7"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_the_http_response():
    class BlockingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            yield b": keepalive\n\n"
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    source = BlockingStream()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                stream=source,
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    sdk = AsyncVotrix(
        api_key="vma_test_sse",
        base_url="https://vma.test",
        max_retries=0,
        http_client=http_client,
    )

    async def consume() -> None:
        async with await sdk.sessions.events.stream("session_1") as stream:
            async for _event in stream:
                pass

    task = asyncio.create_task(consume())
    await source.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.closed is True
    await http_client.aclose()
