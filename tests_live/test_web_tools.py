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
# Python article is ~240k characters; `web_fetch` returns the first 20k. The
# name of Python 2's final release manager is well past that, so an agent that
# stops at the tool result has to say it does not know. Getting it right means
# it used the sandbox path — which is the entire point of saving the file.


@pytest.mark.asyncio(loop_scope="session")
async def test_an_agent_recovers_what_the_truncation_cut_off(api, session):
    from tests_live.helpers import calls, run_to_end, said

    events = await run_to_end(
        api,
        session,
        "Fetch https://en.wikipedia.org/wiki/Python_(programming_language) and tell me "
        "which Python version the article says was the last release of Python 2, and "
        "the exact date it was released. Answer from the page, not from memory.",
    )

    used = [call.get("name") for call in calls(events)]
    reply = said(events)
    print("\n--- tools the agent used ---\n", used)
    print("\n--- what it answered ---\n", reply)

    assert "web_fetch" in used, f"the agent never fetched the page; it used {used}"
    # Reading past the truncation is the behaviour under test — whether it
    # greps or reads with an offset is the agent's call, but it has to do one.
    assert any(name in used for name in ("grep", "read_file")), (
        f"the agent answered from the truncated head alone; it used {used}"
    )
    assert "2.7.18" in reply, f"wrong or missing answer: {reply!r}"
