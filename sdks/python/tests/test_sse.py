from __future__ import annotations

import asyncio

import httpx
import pytest

from votrix.managed_agents import APIStreamError, AsyncVotrix


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


@pytest.mark.asyncio
async def test_stream_parses_event_payloads_into_typed_attributes():
    """Nested payloads must be attribute-reachable, not left as raw dicts.

    A consumer that reads `event.model_usage.input_tokens` off a dict gets a
    silent zero rather than an error, so metering degrades without any signal.
    """

    sdk, http_client = make_client(
        'id: 8\nevent: span.model_request_end\n'
        'data: {"id":"evt_end","type":"span.model_request_end","session_id":"session_1","seq":8,'
        '"model_request_start_id":"evt_start","is_error":false,'
        '"model_usage":{"input_tokens":1234,"output_tokens":567,'
        '"cache_read_input_tokens":800,"cache_creation_input_tokens":0}}\n\n'
        'id: 9\nevent: session.status_idle\n'
        'data: {"id":"evt_idle","type":"session.status_idle","session_id":"session_1","seq":9,'
        '"stop_reason":{"type":"requires_action","event_ids":["evt_tool"]}}\n\n'
        'id: 10\nevent: agent.tool_result\n'
        'data: {"id":"evt_res","type":"agent.tool_result","session_id":"session_1","seq":10,'
        '"name":"bash","tool_use_id":"evt_use","is_error":false,'
        '"content":[{"type":"text","text":"ok"}]}\n\n'
        'id: 11\nevent: agent.mcp_tool_result\n'
        'data: {"id":"evt_mcp","type":"agent.mcp_tool_result","session_id":"session_1","seq":11,'
        '"name":"linear_search","mcp_tool_use_id":"evt_mcp_use","content":[{"type":"text","text":"hit"}]}\n\n'
        'id: 12\nevent: session.error\n'
        'data: {"id":"evt_err","type":"session.error","session_id":"session_1","seq":12,'
        '"error":{"type":"model_rate_limited_error","message":"slow down",'
        '"retry_status":{"type":"exhausted"}}}\n\n'
    )
    async with http_client:
        async with await sdk.sessions.events.stream("session_1") as stream:
            events = [event async for event in stream]

    span, idle, tool, mcp, error = events

    assert span.model_request_start_id == "evt_start"
    assert span.is_error is False
    assert span.model_usage is not None
    assert span.model_usage.input_tokens == 1234
    assert span.model_usage.output_tokens == 567
    assert span.model_usage.cache_read_input_tokens == 800
    assert span.model_usage.cache_creation_input_tokens == 0

    assert idle.stop_reason is not None
    assert idle.stop_reason.type == "requires_action"
    assert idle.stop_reason.event_ids == ["evt_tool"]

    # A result points at the event that opened the call, and the field name
    # differs between built-in and MCP results.
    assert tool.tool_use_id == "evt_use"
    assert tool.content is not None and not isinstance(tool.content, str)
    assert [block.text for block in tool.content] == ["ok"]
    assert mcp.mcp_tool_use_id == "evt_mcp_use"
    assert mcp.tool_use_id is None

    assert error.error is not None
    assert error.error.type == "model_rate_limited_error"
    assert error.error.retry_status is not None
    assert error.error.retry_status.type == "exhausted"


@pytest.mark.asyncio
async def test_streamed_and_listed_events_share_one_type():
    """The reconnect pattern reconciles history against the live stream, so both
    sides have to be the same shape."""

    from votrix.managed_agents import SessionEvent

    sdk, http_client = make_client(
        'id: 7\nevent: agent.message\n'
        'data: {"id":"evt_1","type":"agent.message","session_id":"session_1","seq":7,'
        '"content":[{"type":"text","text":"hi"}]}\n\n'
    )
    async with http_client:
        async with await sdk.sessions.events.stream("session_1") as stream:
            streamed = [event async for event in stream]

    assert isinstance(streamed[0], SessionEvent)
    # An unrecognized event type still parses — the surface is open.
    unknown = SessionEvent.model_validate(
        {"id": "evt_x", "type": "agent.future_event", "surprise": 1}
    )
    assert unknown.type == "agent.future_event"
    assert unknown.model_usage is None
    assert unknown.model_extra == {"surprise": 1}


@pytest.mark.asyncio
async def test_stream_and_upload_accept_a_per_call_timeout():
    """One client serves both fast CRUD and long-lived work, so the slow calls
    override the default instead of widening it for everything."""

    seen: list[httpx.Timeout] = []

    def handler(request: httpx.Request):
        seen.append(request.extensions["timeout"])
        if request.url.path.endswith("/events/stream"):
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={"id": "file_1", "type": "file", "filename": "a.txt"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncVotrix(
        api_key="vma_test_timeout",
        base_url="https://vma.test",
        max_retries=0,
        timeout=30.0,
        http_client=http_client,
    )
    async with http_client:
        await sdk.files.upload(file=("a.txt", b"x", "text/plain"), timeout=900)
        async with await sdk.sessions.events.stream(
            "session_1", timeout=httpx.Timeout(30.0, read=1800.0)
        ):
            pass
        await sdk.files.retrieve_metadata("file_1")

    upload, stream, plain = seen
    assert upload["read"] == 900
    assert stream["read"] == 1800.0
    # Calls that pass nothing keep the client default.
    assert plain["read"] == 30.0
