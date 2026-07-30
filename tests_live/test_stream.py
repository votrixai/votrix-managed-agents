"""Scenarios 33–35: watching a session instead of polling it.

The stream is the only part of the API a browser holds open, and the only part
where "nothing lost, nothing repeated" is a property of a *reconnect* rather
than of a single response. That is what these three are for.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from tests_live.helpers import answer, events_after, message, said, status

pytestmark = pytest.mark.asyncio(loop_scope="session")

STREAM_TIMEOUT = 300.0


def _frame(raw: str) -> dict[str, Any] | None:
    """One SSE frame, or None for a keep-alive comment."""
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if "data" not in fields:
        return None
    return {
        "seq": int(fields["id"]),
        "type": fields["event"],
        "data": json.loads(fields["data"]),
    }


async def watch(
    api: httpx.AsyncClient,
    session_id: str,
    *,
    stop: Callable[[list[dict[str, Any]]], bool],
    after_seq: int | None = None,
    last_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read the stream until `stop` says so.

    The connection never closes by itself while a session is alive, so the
    reader decides when it has seen enough — the same thing a real client does.
    """
    params = {} if after_seq is None else {"after_seq": after_seq}
    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    seen: list[dict[str, Any]] = []

    async def _read() -> None:
        async with api.stream(
            "GET",
            f"/v1/sessions/{session_id}/events/stream",
            params=params,
            headers=headers,
        ) as response:
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    frame = _frame(raw)
                    if frame is None:
                        continue
                    seen.append(frame)
                    if stop(seen):
                        return

    await asyncio.wait_for(_read(), timeout=STREAM_TIMEOUT)
    return seen


def until_idle(frames: list[dict[str, Any]]) -> bool:
    return frames[-1]["type"] == "session.status_idle"


async def test_33_a_whole_turn_arrives_on_one_connection(api, session):
    """What the stream delivers has to be what the log holds — same events,
    same order, same sequence numbers."""
    watching = asyncio.create_task(watch(api, session, stop=until_idle))
    await asyncio.sleep(0.5)  # let the connection establish before anything happens

    sent = await api.post(
        f"/v1/sessions/{session}/events",
        json={"events": [message("Read uploads/notes.txt and quote its first line.")]},
    )
    sent.raise_for_status()

    streamed = await watching
    logged = await events_after(api, session, 0)

    assert [frame["seq"] for frame in streamed] == [event["seq"] for event in logged]
    assert [frame["type"] for frame in streamed] == [event["type"] for event in logged]
    assert [frame["data"] for frame in streamed] == logged

    seqs = [frame["seq"] for frame in streamed]
    assert seqs == sorted(seqs), seqs
    assert len(set(seqs)) == len(seqs), seqs


async def test_34_a_reconnect_loses_nothing_and_repeats_nothing(api, session):
    """`Last-Event-ID` is what a browser sends by itself after a drop, so it is
    what decides whether a refreshed page shows a gap."""
    await api.post(
        f"/v1/sessions/{session}/events",
        json={"events": [message("Read uploads/readme.md and quote its heading.")]},
    )
    logged = await events_after(api, session, 0)
    assert len(logged) >= 4, logged
    cut = len(logged) // 2

    first = await watch(api, session, after_seq=0, stop=lambda seen: len(seen) == cut)
    assert [frame["seq"] for frame in first] == [event["seq"] for event in logged[:cut]]

    # Dropped, and picked up again with the id it last saw.
    resumed = await watch(
        api,
        session,
        last_event_id=str(first[-1]["seq"]),
        stop=lambda seen: seen[-1]["seq"] == logged[-1]["seq"],
    )

    assert [frame["seq"] for frame in first + resumed] == [event["seq"] for event in logged]


async def test_35_a_pause_is_announced_on_the_stream(api, session):
    """A client watching the stream learns it is being asked something, without
    polling for it. `requires_action` has to arrive with the ids attached."""
    watching = asyncio.create_task(watch(api, session, stop=until_idle))
    await asyncio.sleep(0.5)

    sent = await api.post(
        f"/v1/sessions/{session}/events",
        json={
            "events": [message("Use the get_crm_record tool to look up customer C001.")]
        },
    )
    sent.raise_for_status()
    streamed = await watching

    idle = streamed[-1]["data"]
    assert idle["type"] == "session.status_idle"
    assert idle["stop_reason"]["type"] == "requires_action", idle
    ids = idle["stop_reason"]["tool_use_ids"]
    assert len(ids) == 1

    announced = [
        frame["data"] for frame in streamed if frame["type"] == "agent.custom_tool_use"
    ]
    assert [call["tool_use_id"] for call in announced] == ids

    # Answer it so the session is not left holding a pause at teardown.
    after = await answer(api, session, [{
        "type": "user.custom_tool_result",
        "custom_tool_use_id": ids[0],
        "content": [{"type": "text", "text": "C001 = Northwind Traders"}],
    }])
    assert "northwind" in said(after), said(after)
    assert await status(api, session) == "idle"
