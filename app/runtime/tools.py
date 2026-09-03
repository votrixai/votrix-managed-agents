"""What the agent's declared tools mean at runtime.

Nothing here ever removes a tool. Whatever `create_deep_agent()` installs is
exactly what the model gets; toolset config only decides which of those tools
must stop and ask the user before running.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.utils.sandbox import WEB_CACHE_DIR

if TYPE_CHECKING:
    from app.utils.sandbox import Sandbox

TOOLSET_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "agent_toolset_20260401": (
        "execute",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "write_todos",
        # No `task`: the general-purpose subagent that carries it is disabled
        # in `runtime/engine.py`. Naming it here would promise a tool the
        # model never sees and let a stored config set a policy for it.
    ),
    "web_toolset_20260401": ("web_fetch", "web_search"),
}

AGENT_TOOLSET = "agent_toolset_20260401"
WEB_TOOLSET = "web_toolset_20260401"

FIRECRAWL_API_BASE = "https://api.firecrawl.dev/v2"
FIRECRAWL_RESPONSE_MAX_BYTES = 5 * 1024 * 1024
WEB_SEARCH_MAX_RESULTS = 10
# How much of a page comes back in the tool result. Roughly 5k tokens, which is
# an affordable slice of a turn's context and enough that most pages arrive
# whole. It is deliberately not a "short enough to always be safe" number: at
# 8000 every real page measured — a news post, a docs index, a product page —
# came back truncated, so the threshold was doing nothing except deciding which
# pages got a summary instead of their own text.
WEB_FETCH_INLINE_MAX_CHARS = 20000


class _FirecrawlResponseError(RuntimeError):
    """A successful HTTP response that does not satisfy Firecrawl's contract."""


class _FirecrawlResponseTooLarge(_FirecrawlResponseError):
    """The response crossed our memory boundary while it was being downloaded."""


async def _firecrawl_data(
    endpoint: str,
    *,
    api_key: str,
    request_body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Return one bounded, validated Firecrawl ``data`` object.

    A normal ``AsyncClient.post`` buffers the complete response before it
    returns. Firecrawl can turn documents into very large Markdown payloads,
    so the boundary has to be enforced while bytes arrive, before JSON parsing
    creates another in-memory representation of the same content.
    """

    body = bytearray()
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        async with client.stream(
            "POST",
            f"{FIRECRAWL_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_body,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > FIRECRAWL_RESPONSE_MAX_BYTES:
                    raise _FirecrawlResponseTooLarge
                body.extend(chunk)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _FirecrawlResponseError from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise _FirecrawlResponseError
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _FirecrawlResponseError
    return data


def resolve_tool_interrupts(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Which of DeepAgents' always-on native tools need a decision first.

    Nothing is ever excluded: every native tool stays available regardless of
    what the agent's config says. Config only controls whether a given tool
    needs an approve/reject before it runs.
    """
    interrupt_on: dict[str, Any] = {}
    for toolset in tools:
        if not isinstance(toolset, dict):
            continue
        names = TOOLSET_TOOL_NAMES.get(toolset.get("type"))
        if names is None:
            continue
        default_policy = _policy(dict(toolset.get("default_config") or {}), "always_allow")
        resolved = dict.fromkeys(names, default_policy)
        for entry in toolset.get("configs") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if name in names:
                resolved[name] = _policy(entry, default_policy)
        for name, policy in resolved.items():
            if policy == "always_ask":
                interrupt_on[name] = {"allowed_decisions": ["approve", "reject"]}
    return interrupt_on


def custom_tool(spec: dict[str, Any]) -> StructuredTool:
    """A tool the client runs, not us.

    The body never executes in practice — these are always paired with a
    respond-only interrupt, so the graph stops and waits for the client. It is
    here as a loud failure in case that ever stops being true.
    """
    name = str(spec["name"])

    async def _client_owned_tool(**kwargs: Any) -> str:
        return json.dumps({"error": "client_tool_result_required", "tool": name, "input": kwargs})

    return StructuredTool.from_function(
        coroutine=_client_owned_tool,
        name=name,
        description=str(spec.get("description") or f"Custom tool {name}."),
        args_schema=dict(spec.get("input_schema") or {"type": "object", "properties": {}}),
    )


def web_search_tool() -> StructuredTool:
    async def web_search(query: str) -> str:
        """Search the web and return the top results as title, URL, and snippet."""
        settings = get_settings()
        if not settings.firecrawl_api_key:
            return "web_search is unavailable: no Firecrawl API key is configured on this server."

        try:
            data = await _firecrawl_data(
                "search",
                api_key=settings.firecrawl_api_key,
                request_body={"query": query, "limit": WEB_SEARCH_MAX_RESULTS},
                timeout_seconds=30.0,
            )
            results = data.get("web") or []
            if not isinstance(results, list) or not all(
                isinstance(item, dict) for item in results
            ):
                raise _FirecrawlResponseError

            if not results:
                return f"web_search found no results for {query!r}."

            results = results[:WEB_SEARCH_MAX_RESULTS]
            lines = [f"Top {len(results)} results for {query!r}:", ""]
            for i, item in enumerate(results, start=1):
                title = item.get("title") or "(untitled)"
                url = item.get("url") or ""
                description = item.get("description") or ""
                lines.append(f"{i}. {title}\n   {url}\n   {description}")
            return "\n".join(lines)
        except httpx.HTTPStatusError as exc:
            return f"web_search failed: Firecrawl returned {exc.response.status_code} for {query!r}."
        except httpx.HTTPError as exc:
            return f"web_search failed: Firecrawl request raised {type(exc).__name__} for {query!r}."
        except _FirecrawlResponseTooLarge:
            return f"web_search failed: Firecrawl response exceeded the 5 MiB limit for {query!r}."
        except _FirecrawlResponseError:
            return f"web_search failed: Firecrawl returned an invalid response for {query!r}."
        except Exception as exc:
            return f"web_search failed while processing {query!r}: {type(exc).__name__}."

    return StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description=(
            f"Search the web and get back the top {WEB_SEARCH_MAX_RESULTS} results, each "
            "with a title, URL, and short description. Use web_fetch on a URL from these "
            "results to read the full page."
        ),
    )


def web_fetch_tool(sandbox: Sandbox) -> StructuredTool:
    async def web_fetch(url: str) -> str:
        """Fetch a public HTTP(S) URL and return its content as clean markdown."""
        settings = get_settings()
        if not settings.firecrawl_api_key:
            return "web_fetch is unavailable: no Firecrawl API key is configured on this server."

        try:
            data = await _firecrawl_data(
                "scrape",
                api_key=settings.firecrawl_api_key,
                request_body={"url": url, "formats": ["markdown"]},
                timeout_seconds=60.0,
            )
            markdown = data.get("markdown") or ""
            if not isinstance(markdown, str):
                raise _FirecrawlResponseError
            if not markdown:
                return f"web_fetch got no readable content from {url!r}."

            if len(markdown) <= WEB_FETCH_INLINE_MAX_CHARS:
                return markdown

            # The page itself, truncated — not a summary of it. Firecrawl can
            # produce one, and it reads well, but a summariser that cannot see
            # the question drops whatever it judged unimportant, and that is
            # exactly as likely to be the answer as anything else it kept.
            # Truncation is lossy too, but transparently so: what comes back is
            # the page's own words, and the rest is a `grep` away rather than
            # gone.
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            cache_path = f"{WEB_CACHE_DIR}/{digest}.md"
            await sandbox.write_bytes(cache_path, markdown.encode("utf-8"))
            return (
                f"{url} is {len(markdown)} characters. The first "
                f"{WEB_FETCH_INLINE_MAX_CHARS} are below, and the whole page was saved "
                f"to `{cache_path}`. If what you need is not below, grep that path for "
                f"it rather than reading from the top — on a page this size searching "
                f"finds the answer in one call where paging through it takes many. "
                f"read_file with an offset also works when you want to keep "
                f"reading in order.\n\n---\n\n{markdown[:WEB_FETCH_INLINE_MAX_CHARS]}"
            )
        except httpx.HTTPStatusError as exc:
            return f"web_fetch failed: Firecrawl returned {exc.response.status_code} for {url!r}."
        except httpx.HTTPError as exc:
            return f"web_fetch failed: Firecrawl request raised {type(exc).__name__} for {url!r}."
        except _FirecrawlResponseTooLarge:
            return f"web_fetch failed: Firecrawl response exceeded the 5 MiB limit for {url!r}."
        except _FirecrawlResponseError:
            return f"web_fetch failed: Firecrawl returned an invalid response for {url!r}."
        except Exception as exc:
            return f"web_fetch failed while processing {url!r}: {type(exc).__name__}."

    return StructuredTool.from_function(
        coroutine=web_fetch,
        name="web_fetch",
        description=(
            "Fetch a public HTTP(S) URL and get back its content as clean markdown. "
            f"Pages up to {WEB_FETCH_INLINE_MAX_CHARS} characters come back whole; "
            "longer ones are saved to a file in the sandbox and come back truncated "
            "to that length, with the file's path. The text is always the page's "
            "own — nothing here summarises it. Use grep on that path to find what "
            "the truncated part cut off, or read_file with an offset to keep reading."
        ),
    )

def _policy(config: dict[str, Any], default: str) -> str:
    policy = config.get("permission_policy")
    if isinstance(policy, dict) and policy.get("type") in {"always_allow", "always_ask"}:
        return str(policy["type"])
    return default


__all__ = [
    "AGENT_TOOLSET",
    "FIRECRAWL_RESPONSE_MAX_BYTES",
    "TOOLSET_TOOL_NAMES",
    "WEB_TOOLSET",
    "custom_tool",
    "resolve_tool_interrupts",
    "web_fetch_tool",
    "web_search_tool",
]
