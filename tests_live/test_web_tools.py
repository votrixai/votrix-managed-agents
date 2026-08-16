"""Real, live tests: actually calls Firecrawl and actually opens an E2B sandbox.

No mocks. Requires FIRECRAWL_API_KEY and E2B_API_KEY to be set (via .env).
"""

import pytest
from e2b import AsyncSandbox

from app.config import get_settings
from app.runtime.tools import web_fetch_tool, web_search_tool
from app.utils.sandbox import Sandbox


@pytest.mark.asyncio
async def test_web_search_live():
    settings = get_settings()
    assert settings.firecrawl_api_key, "FIRECRAWL_API_KEY is not set in .env"

    tool = web_search_tool()
    result = await tool.ainvoke({"query": "Anthropic Claude latest release"})
    print("\n--- web_search result ---\n", result)

    assert "no Firecrawl API key" not in result
    assert "http" in result  # at least one URL came back


@pytest.mark.asyncio
async def test_web_fetch_live_short_page():
    settings = get_settings()
    assert settings.firecrawl_api_key, "FIRECRAWL_API_KEY is not set in .env"
    assert settings.e2b_api_key, "E2B_API_KEY is not set in .env"

    # A real E2B sandbox, not a fake one.
    native = await AsyncSandbox.create(
        template="base",
        timeout=120,
        api_key=settings.e2b_api_key,
    )
    sandbox = Sandbox(native.sandbox_id, "live-test-session", "org-live-test", native=native)

    try:
        tool = web_fetch_tool(sandbox)
        result = await tool.ainvoke({"url": "https://example.com"})
        print("\n--- web_fetch result ---\n", result)
        assert "no Firecrawl API key" not in result
        assert result  # got something back
    finally:
        await sandbox.kill()


@pytest.mark.asyncio
async def test_web_fetch_live_long_page_writes_to_sandbox():
    """A long real page should get stored in the sandbox, not dumped inline."""
    settings = get_settings()
    assert settings.firecrawl_api_key, "FIRECRAWL_API_KEY is not set in .env"
    assert settings.e2b_api_key, "E2B_API_KEY is not set in .env"

    native = await AsyncSandbox.create(
        template="base",
        timeout=120,
        api_key=settings.e2b_api_key,
    )
    sandbox = Sandbox(native.sandbox_id, "live-test-session", "org-live-test", native=native)

    try:
        tool = web_fetch_tool(sandbox)
        # Wikipedia's Python article is comfortably over 8000 characters.
        result = await tool.ainvoke({"url": "https://en.wikipedia.org/wiki/Python_(programming_language)"})
        print("\n--- web_fetch (long page) result ---\n", result)
        assert "too long" in result
        assert ".web_cache" in result
    finally:
        await sandbox.kill()


# --- the whole loop, through a real agent ------------------------------------
#
# The tests above drive `web_fetch` directly. This one drives a model that has
# it, which is the only way to answer the question the tool's design is really
# betting on: when a page comes back truncated, does the agent go and get the
# rest?
#
# The question is chosen so it cannot be answered any other way. Wikipedia's
# Python article is ~240k characters and `web_fetch` returns the first 20k;
# Python 2's final release sits well past that. An agent that stops at the tool
# result has to say it does not know, so a right answer means it used the
# sandbox path — which is the whole reason the file is saved.


async def _run_to_idle(api, session_id: str, text: str, *, timeout: float = 600.0):
    """Send one message and collect the turn, approving what it stops to ask.

    The suite's own `send` helper reads the log once, immediately. That was
    right when the request stayed open for the turn; dispatch returns as soon
    as the message is accepted now, so a single read sees the message and
    nothing else. This waits instead.
    """

    import asyncio
    import time

    from tests_live.helpers import allow, events_after, last_seq

    cursor = await last_seq(api, session_id)
    response = await api.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]},
    )
    response.raise_for_status()

    collected: list[dict] = []
    approved: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = await events_after(api, session_id, cursor)
        if fresh:
            collected += fresh
            cursor = fresh[-1]["seq"]

        idle = [e for e in collected if e["type"] == "session.status_idle"]
        if idle:
            stop = idle[-1].get("stop_reason") or {}
            if stop.get("type") != "requires_action":
                return collected
            waiting = [i for i in (stop.get("event_ids") or []) if i not in approved]
            if waiting:
                approved.update(waiting)
                await api.post(
                    f"/v1/sessions/{session_id}/events",
                    json={"events": [allow(i) for i in waiting]},
                )
        await asyncio.sleep(1.0)

    raise AssertionError(f"turn never finished: {[e['type'] for e in collected]}")


@pytest.mark.asyncio(loop_scope="session")
async def test_an_agent_recovers_what_the_truncation_cut_off(api, session):
    from tests_live.helpers import calls, said

    events = await _run_to_idle(
        api,
        session,
        "Fetch https://en.wikipedia.org/wiki/Python_(programming_language) and tell me "
        "which Python version the article says was the final release of Python 2, and "
        "the date it was released. Answer from the page, not from memory.",
    )

    used = [call.get("name") for call in calls(events)]
    reply = said(events)
    print("\n--- tools the agent used ---\n", used)
    print("\n--- what it answered ---\n", reply)

    assert "web_fetch" in used, f"the agent never fetched the page; it used {used}"
    # Reading past the truncation is the behaviour under test. Whether it greps
    # or reads with an offset is the agent's call, but it has to do one.
    assert any(name in used for name in ("grep", "read_file", "execute")), (
        f"the agent answered from the truncated head alone; it used {used}"
    )
    assert "2.7.18" in reply, f"wrong or missing answer: {reply!r}"
