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
