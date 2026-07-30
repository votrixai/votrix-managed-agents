"""Running a turn and reading it back.

Dispatch is `inline`, so `POST /events` does not return until the turn is over.
That removes polling from this suite entirely: send, then read everything past
the sequence number the session was at beforehand, and that is the turn.
"""

from __future__ import annotations

from typing import Any

import httpx

# Types, spelled once so a rename shows up here rather than in thirty strings.
AGENT_MESSAGE = "agent.message"
AGENT_THINKING = "agent.thinking"
AGENT_TOOL_USE = "agent.tool_use"
AGENT_CUSTOM_TOOL_USE = "agent.custom_tool_use"
AGENT_TOOL_RESULT = "agent.tool_result"
SESSION_IDLE = "session.status_idle"
SESSION_RUNNING = "session.status_running"
SESSION_ERROR = "session.error"


# --- building what a client sends -------------------------------------------


def message(text: str) -> dict[str, Any]:
    return {"type": "user.message", "content": [{"type": "text", "text": text}]}


def allow(tool_use_id: str) -> dict[str, Any]:
    return {"type": "user.tool_confirmation", "tool_use_id": tool_use_id, "result": "allow"}


def deny(tool_use_id: str, reason: str) -> dict[str, Any]:
    return {
        "type": "user.tool_confirmation",
        "tool_use_id": tool_use_id,
        "result": "deny",
        "deny_message": reason,
    }


def respond(tool_use_id: str, text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "user.custom_tool_result",
        "custom_tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
    }


# --- driving a session ------------------------------------------------------


async def last_seq(api: httpx.AsyncClient, session_id: str) -> int:
    response = await api.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()["last_event_seq"]


async def events_after(
    api: httpx.AsyncClient, session_id: str, seq: int
) -> list[dict[str, Any]]:
    """Every event past `seq`, oldest first, following pages to the end."""
    collected: list[dict[str, Any]] = []
    cursor = seq
    while True:
        response = await api.get(
            f"/v1/sessions/{session_id}/events",
            params={"after_seq": cursor, "limit": 100},
        )
        response.raise_for_status()
        page = response.json()
        collected.extend(page["data"])
        if not page["data"] or not page["has_more"]:
            return collected
        cursor = collected[-1]["seq"]


async def send(
    api: httpx.AsyncClient, session_id: str, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Send a batch and return everything the session produced because of it.

    The response body only carries the events that were accepted; what the
    agent did with them is read back from the log, which is also the only place
    a client would ever see it.
    """
    before = await last_seq(api, session_id)
    response = await api.post(f"/v1/sessions/{session_id}/events", json={"events": events})
    response.raise_for_status()
    return await events_after(api, session_id, before)


async def run_turn(api: httpx.AsyncClient, session_id: str, text: str) -> list[dict[str, Any]]:
    return await send(api, session_id, [message(text)])


async def run_to_end(
    api: httpx.AsyncClient, session_id: str, text: str, *, rounds: int = 5
) -> list[dict[str, Any]]:
    """Run a turn, approving whatever it stops to ask, until it is done.

    For scenarios that are about something else entirely — a skill, a file
    round trip — and only need the agent to finish. Which tools a model reaches
    for to do arithmetic is its own business, and a test about skills should
    not fail because it chose the shell.

    Only approvals. A custom call appearing here means the model went somewhere
    the scenario never intended, and that should fail rather than be answered.
    """
    collected = await send(api, session_id, [message(text)])
    for _ in range(rounds):
        if stop_reason(collected)["type"] != "requires_action":
            return collected
        waiting = pending(collected)
        for call in calls(collected):
            assert (
                call["tool_use_id"] not in waiting or call["type"] == AGENT_TOOL_USE
            ), f"unexpected custom call in a turn meant to run itself: {call['name']}"
        collected += await send(api, session_id, [allow(call_id) for call_id in waiting])
    raise AssertionError(f"still asking after {rounds} rounds: {stop_reason(collected)}")


async def status(api: httpx.AsyncClient, session_id: str) -> str:
    response = await api.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()["status"]


async def download(api: httpx.AsyncClient, file_id: str) -> bytes:
    """Follow the signed-URL redirect by hand.

    Deliberately not `follow_redirects`: the second request goes to object
    storage, and it should not carry this client's tenant header there.
    """
    redirect = await api.get(f"/v1/files/{file_id}/content")
    assert redirect.status_code == 307, f"{redirect.status_code}: {redirect.text}"
    async with httpx.AsyncClient(timeout=60.0) as anonymous:
        fetched = await anonymous.get(redirect.headers["location"])
    fetched.raise_for_status()
    return fetched.content


async def output_files(api: httpx.AsyncClient, session_id: str) -> dict[str, str]:
    """What the session produced, by filename."""
    response = await api.get("/v1/files", params={"scope_id": session_id, "limit": 100})
    response.raise_for_status()
    return {row["filename"]: row["id"] for row in response.json()["data"]}


async def answer(
    api: httpx.AsyncClient, session_id: str, answers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return await send(api, session_id, answers)


# --- reading a turn ---------------------------------------------------------


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def of_type(events: list[dict[str, Any]], *types: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] in types]


def calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool call announced, direct and custom, in the order announced."""
    return of_type(events, AGENT_TOOL_USE, AGENT_CUSTOM_TOOL_USE)


def call_named(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [call for call in calls(events) if call["name"] == name]
    assert found, f"no call to {name!r} among {[c['name'] for c in calls(events)]}"
    assert len(found) == 1, f"{len(found)} calls to {name!r}, expected one"
    return found[0]


def results(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Tool results keyed by the call they answer."""
    return {event["tool_use_id"]: event for event in of_type(events, AGENT_TOOL_RESULT)}


def stop_reason(events: list[dict[str, Any]]) -> dict[str, Any]:
    idle = of_type(events, SESSION_IDLE)
    assert idle, f"the turn never went idle: {kinds(events)}"
    return idle[-1]["stop_reason"]


def pending(events: list[dict[str, Any]]) -> list[str]:
    """The ids the turn is waiting on, in the order the graph wants them."""
    reason = stop_reason(events)
    assert reason["type"] == "requires_action", f"expected a pause, got {reason}"
    return reason["tool_use_ids"]


def said(events: list[dict[str, Any]]) -> str:
    """Everything the agent said this turn, as one lowercase string."""
    return " ".join(
        block["text"]
        for event in of_type(events, AGENT_MESSAGE)
        for block in event["content"]
    ).lower()


def tool_output(events: list[dict[str, Any]], tool_use_id: str) -> str:
    result = results(events).get(tool_use_id)
    assert result is not None, (
        f"no result for {tool_use_id}; results exist for {list(results(events))}"
    )
    return " ".join(block["text"] for block in result["content"])
