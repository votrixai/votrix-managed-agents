"""Deterministic coverage for the Firecrawl-backed agent tools.

The live suite proves the third-party services can work together. These tests
prove our boundaries without spending credits or depending on either service,
which is what makes them suitable for every CI run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app.config import get_settings
from app.runtime import tools as tools_module
from app.runtime.tools import (
    WEB_FETCH_INLINE_MAX_CHARS,
    web_fetch_tool,
    web_search_tool,
)


class FakeSandbox:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.writes: list[tuple[str, bytes]] = []

    async def write_bytes(self, path: str, data: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.writes.append((path, data))


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


@pytest.fixture(autouse=True)
def firecrawl_key(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_transport(
    monkeypatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Put a MockTransport behind the production AsyncClient call."""

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(tools_module.httpx, "AsyncClient", client)


def _success(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


async def test_web_search_returns_validated_results(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _success(
            {
                "web": [
                    {
                        "title": "Votrix",
                        "url": "https://votrix.ai",
                        "description": "Managed agents.",
                    }
                ]
            }
        )

    _use_transport(monkeypatch, handler)

    result = await web_search_tool().ainvoke({"query": "votrix"})

    assert "https://votrix.ai" in result
    assert requests[0].headers["authorization"] == "Bearer fc-test"
    assert json.loads(requests[0].content) == {"query": "votrix", "limit": 10}


async def test_an_http_failure_is_a_tool_result(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(503, request=request),
    )

    result = await web_fetch_tool(FakeSandbox()).ainvoke(
        {"url": "https://example.com"}
    )

    assert "Firecrawl returned 503" in result


async def test_a_transport_failure_is_a_tool_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    _use_transport(monkeypatch, handler)

    result = await web_search_tool().ainvoke({"query": "anything"})

    assert "ConnectError" in result


async def test_invalid_json_is_a_tool_result(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"not json", request=request),
    )

    result = await web_fetch_tool(FakeSandbox()).ainvoke(
        {"url": "https://example.com"}
    )

    assert "invalid response" in result


@pytest.mark.parametrize(
    "data",
    [
        ["not", "an", "object"],
        {"web": "not a result list"},
    ],
)
async def test_an_invalid_data_shape_is_a_tool_result(monkeypatch, data):
    _use_transport(monkeypatch, lambda request: _success(data))

    result = await web_search_tool().ainvoke({"query": "anything"})

    assert "invalid response" in result


async def test_an_oversized_response_is_stopped_before_json_parsing(monkeypatch):
    monkeypatch.setattr(tools_module, "FIRECRAWL_RESPONSE_MAX_BYTES", 64)
    stream = CountingStream([b"x" * 32, b"y" * 33, b"z" * 32])
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(200, stream=stream, request=request),
    )
    sandbox = FakeSandbox()

    result = await web_fetch_tool(sandbox).ainvoke({"url": "https://example.com"})

    assert "response exceeded" in result
    assert sandbox.writes == []
    assert stream.yielded == 2


async def test_a_short_page_is_returned_inline(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda request: _success({"markdown": "# Small page"}),
    )
    sandbox = FakeSandbox()

    result = await web_fetch_tool(sandbox).ainvoke({"url": "https://example.com"})

    assert result == "# Small page"
    assert sandbox.writes == []


async def test_a_long_page_is_saved_whole_and_returned_truncated(monkeypatch):
    """The overflow path hands back the page's own text, not a summary of it.

    A summary rides along in the fake response to catch the obvious
    regression: nothing may reach the model that Firecrawl wrote *about* the
    page rather than took *from* it.
    """

    url = "https://example.com/long"
    markdown = "m" * (WEB_FETCH_INLINE_MAX_CHARS + 1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _success({"markdown": markdown, "summary": "s" * 500})

    _use_transport(monkeypatch, handler)
    sandbox = FakeSandbox()

    result = await web_fetch_tool(sandbox).ainvoke({"url": url})

    # Whole page on disk, truncated page in the result.
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    assert sandbox.writes == [
        (f"/home/user/.web_cache/{digest}.md", markdown.encode())
    ]
    assert result.rsplit("\n\n---\n\n", 1)[1] == "m" * WEB_FETCH_INLINE_MAX_CHARS
    assert "s" * 500 not in result

    # The summary is not even asked for, so it costs nothing to ignore it.
    assert json.loads(requests[0].content) == {"url": url, "formats": ["markdown"]}

    # The path is only useful with a way to use it, and on a page this size
    # that way is `grep` — reading from the top is what truncation already did.
    assert f"/home/user/.web_cache/{digest}.md" in result
    assert "grep" in result


async def test_a_sandbox_write_failure_is_a_tool_result(monkeypatch):
    markdown = "m" * (WEB_FETCH_INLINE_MAX_CHARS + 1)
    _use_transport(monkeypatch, lambda request: _success({"markdown": markdown}))

    result = await web_fetch_tool(
        FakeSandbox(error=RuntimeError("sandbox unavailable"))
    ).ainvoke({"url": "https://example.com/long"})

    assert "failed while processing" in result
    assert "RuntimeError" in result
