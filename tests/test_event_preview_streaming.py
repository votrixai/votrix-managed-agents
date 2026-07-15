import asyncio
from types import SimpleNamespace

import pytest

from app.routers.sessions import _requested_event_deltas, _resume_after_seq, _stream_response
from app.runtime.vma_preview_bus import VmaProcessLocalPreviewBus, vma_preview_bus
from app.workspace import default_workspace
from tests.conftest import TEST_HEADERS


async def _create_session(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Preview Stream Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "preview-stream", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={"agent": {"id": agent["id"], "version": 1}, "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_stream_schema_accepts_both_event_delta_query_spellings():
    from app.factory import create_app

    operation = create_app().openapi()["paths"]["/v1/sessions/{session_id}/events/stream"]["get"]
    parameter_names = {parameter["name"] for parameter in operation["parameters"]}

    assert {"event_deltas", "event_deltas[]", "Last-Event-ID"} <= parameter_names
    assert _requested_event_deltas(["agent.message"], ["agent.thinking"]) == {
        "agent.message",
        "agent.thinking",
    }


@pytest.mark.parametrize("query", ["event_deltas=invalid", "event_deltas%5B%5D=invalid"])
async def test_stream_rejects_unknown_preview_types(client, query):
    response = await client.get(
        f"/v1/sessions/missing/events/stream?{query}",
        headers=TEST_HEADERS,
    )

    assert response.status_code == 400
    assert "agent.message or agent.thinking" in response.json()["error"]["message"]


async def test_thread_stream_rejects_event_deltas(client):
    response = await client.get(
        "/v1/sessions/missing/threads/missing/stream?event_deltas=agent.message",
        headers=TEST_HEADERS,
    )

    assert response.status_code == 400
    assert "only supported on session event streams" in response.json()["error"]["message"]


def test_last_event_id_is_a_numeric_durable_sequence():
    assert _resume_after_seq(None, None) is None
    assert _resume_after_seq(None, "12") == 12
    assert _resume_after_seq(0, "12") == 12
    assert _resume_after_seq(15, "12") == 15

    with pytest.raises(Exception) as exc_info:
        _resume_after_seq(0, "evt_not_a_sequence")
    assert getattr(exc_info.value, "status_code", None) == 400


async def test_last_event_id_replays_only_newer_durable_events(client):
    session = await _create_session(client)
    response = await client.patch(
        f"/v1/sessions/{session['id']}",
        headers=TEST_HEADERS,
        json={"title": "updated"},
    )
    assert response.status_code == 200, response.text

    request = _ConnectedRequest()
    response = await _stream_response(
        session["id"],
        request,
        _resume_after_seq(0, "1"),
    )
    iterator = response.body_iterator.__aiter__()
    try:
        frame = await asyncio.wait_for(anext(iterator), timeout=1)
    finally:
        await iterator.aclose()

    assert frame.startswith("id: 2\nevent: session.updated\n")
    assert '"seq":2' in frame


async def test_stream_without_cursor_starts_after_connection_head(client):
    session = await _create_session(client)
    request = _ConnectedRequest()
    response = await _stream_response(session["id"], request, after_seq=None)

    update_response = await client.patch(
        f"/v1/sessions/{session['id']}",
        headers=TEST_HEADERS,
        json={"title": "connected after initial idle"},
    )
    assert update_response.status_code == 200, update_response.text

    iterator = response.body_iterator.__aiter__()
    try:
        frame = await asyncio.wait_for(anext(iterator), timeout=1)
    finally:
        await iterator.aclose()

    assert frame.startswith("id: 2\nevent: session.updated\n")
    assert '"seq":2' in frame


async def test_process_local_bus_preserves_exact_runtime_frame():
    bus = VmaProcessLocalPreviewBus()
    frame = {
        "type": "event_delta",
        "event_id": "sevt_exact",
        "delta": {
            "type": "content_delta",
            "index": 0,
            "content": {"type": "text", "text": "hello"},
        },
    }

    async with bus.subscribe("sess_exact") as queue:
        delivered = await bus.publish("sess_exact", frame)
        received = await asyncio.wait_for(queue.get(), timeout=1)

    assert delivered == 1
    assert received == frame
    assert received is not frame


async def test_session_stream_forwards_exact_preview_sse_and_does_not_persist_it(client):
    session = await _create_session(client)
    request = _ConnectedRequest()
    response = await _stream_response(
        session["id"],
        request,
        after_seq=1,
        event_deltas=frozenset({"agent.message"}),
    )
    iterator = response.body_iterator.__aiter__()
    first_frame_task = asyncio.create_task(anext(iterator))
    try:
        await _wait_for_subscriber(session["id"])
        start = {
            "type": "event_start",
            "event": {"type": "agent.message", "id": "sevt_preview"},
        }
        delta = {
            "type": "event_delta",
            "event_id": "sevt_preview",
            "delta": {
                "type": "content_delta",
                "index": 0,
                "content": {"type": "text", "text": "Hello"},
            },
        }
        await vma_preview_bus.publish(session["id"], start)
        await vma_preview_bus.publish(session["id"], delta)

        start_sse = await asyncio.wait_for(first_frame_task, timeout=1)
        delta_sse = await asyncio.wait_for(anext(iterator), timeout=1)
    finally:
        if not first_frame_task.done():
            first_frame_task.cancel()
        await iterator.aclose()

    assert start_sse == (
        "event: event_start\n"
        'data: {"type":"event_start","event":{"type":"agent.message","id":"sevt_preview"}}\n\n'
    )
    assert delta_sse == (
        "event: event_delta\n"
        'data: {"type":"event_delta","event_id":"sevt_preview","delta":{"type":"content_delta",'
        '"index":0,"content":{"type":"text","text":"Hello"}}}\n\n'
    )
    assert not start_sse.startswith("id:")
    assert not delta_sse.startswith("id:")

    response = await client.get(f"/v1/sessions/{session['id']}/events", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert {event["type"] for event in response.json()["data"]}.isdisjoint({"event_start", "event_delta"})


async def _wait_for_subscriber(session_id: str) -> None:
    for _ in range(100):
        if await vma_preview_bus.subscriber_count(session_id):
            return
        await asyncio.sleep(0.001)
    raise AssertionError("preview stream did not subscribe")


class _ConnectedRequest:
    def __init__(self) -> None:
        self.state = SimpleNamespace(current_workspace=default_workspace())

    async def is_disconnected(self) -> bool:
        return False
