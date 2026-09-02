"""What the agent's declared tools mean at runtime.

DeepAgents' native filesystem tools are always installed. VMA-owned tools are
different: they exist only when their versioned type is present in the Agent's
configuration. Toolset config also decides which declared tools must stop and
ask the user before running.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.models.llm import OPENROUTER_SLUGS
from app.utils.sandbox import WEB_CACHE_DIR, WORKDIR

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
    "describe_image_20260901": ("describe_image",),
}

AGENT_TOOLSET = "agent_toolset_20260401"
WEB_TOOLSET = "web_toolset_20260401"
DESCRIBE_IMAGE = "describe_image_20260901"

# The model that does the looking, fixed rather than configurable: `describe_image`
# is one capability with one price, and an operator swapping the vision model
# under an agent changes what its answers are worth without changing anything
# the agent can see.
DESCRIBE_IMAGE_MODEL = "gemini-3.6-flash"

# A separate vision request is still bounded even though native `read_file`
# images have their own validation and preparation path.
DESCRIBE_IMAGE_MAX_BYTES = 10 * 1024 * 1024

# OpenRouter's documented image-input formats.
DESCRIBE_IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Said to the vision model, not to the agent. The agent's question arrives
# underneath it, and the answer goes back as the tool result.
DESCRIBE_IMAGE_INSTRUCTION = (
    "Answer the question about this image by describing what is actually in it. "
    "Every statement has to be something visible in the image. If the image does "
    "not settle the question, say so plainly rather than guessing."
)

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
    """Which tools in each declared set need a decision first.

    Nothing is excluded from a set once that set is declared. DeepAgents'
    native tools stay available, while a VMA-owned set such as
    `describe_image_20260901` exists only when the Agent names it. Permission
    config controls whether a present tool needs approve/reject before it runs.
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


async def _describe_image_bytes(
    data: bytes,
    mime_type: str,
    query: str,
    *,
    api_key: str,
    session_id: str,
) -> str:
    """Ask the vision model one question about one image.

    Billed to the same Account as the turn that called the tool, so looking at
    a picture is not quietly charged somewhere else.

    Separate from the tool so a test can replace the network call and still
    exercise everything around it.
    """
    from langchain_openrouter import ChatOpenRouter

    vision = ChatOpenRouter(
        model=OPENROUTER_SLUGS[DESCRIBE_IMAGE_MODEL],
        api_key=api_key,
        # A vision-tool call is part of the conversation that requested it,
        # rather than an unrelated generation in OpenRouter's activity log.
        session_id=session_id,
    )
    encoded = base64.b64encode(data).decode("ascii")
    answer = await vision.ainvoke(
        [
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": f"{DESCRIBE_IMAGE_INSTRUCTION}\n\nQuestion: {query}",
                    },
                    # Nested rather than a bare string: the gateway speaks the
                    # OpenAI shape, where `image_url` is an object.
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    },
                ]
            )
        ]
    )
    content = answer.content
    if isinstance(content, str):
        return content.strip()
    # Some providers answer in blocks even when every block is text.
    return " ".join(
        part["text"] if isinstance(part, dict) else str(part)
        for part in content
        if not isinstance(part, dict) or part.get("type") == "text"
    ).strip()


def describe_image_tool(
    sandbox: Sandbox,
    *,
    api_key: str,
    session_id: str,
) -> StructuredTool:
    """Ask a separate vision model about an image and return text.

    `read_file` already returns an image as a content block, which is the right
    answer when the agent's own model has eyes. This deliberately does
    something different: another model looks at the picture and returns prose.
    It is installed only when the Agent declares `describe_image_20260901`.

    Every failure is returned as text. A tool that raises here would end the
    turn over a missing file, where a sentence lets the model try another path.
    """

    async def describe_image_from_path(path: str, query: str) -> str:
        target = path if path.startswith("/") else f"{WORKDIR}/{path.lstrip('./')}"
        suffix = PurePosixPath(target).suffix.lower()
        mime_type = DESCRIBE_IMAGE_TYPES.get(suffix)
        if mime_type is None:
            return (
                f"describe_image cannot open {target}: it reads "
                f"{', '.join(sorted(DESCRIBE_IMAGE_TYPES))} and this is "
                f"{suffix or 'extensionless'}."
            )
        if not api_key:
            return (
                "describe_image is unavailable: no vision model is configured "
                "on this server."
            )

        try:
            data = await sandbox.read_bytes(
                target, max_bytes=DESCRIBE_IMAGE_MAX_BYTES
            )
        except FileNotFoundError:
            return f"describe_image found no file at {target}."
        except ValueError as exc:
            return f"describe_image cannot open {target}: {exc}"
        except Exception as exc:
            return (
                f"describe_image could not read {target}: "
                f"{type(exc).__name__}: {exc}"
            )

        try:
            return await _describe_image_bytes(
                data,
                mime_type,
                query,
                api_key=api_key,
                session_id=session_id,
            )
        except Exception as exc:
            return (
                f"describe_image could not look at {target}: "
                f"{type(exc).__name__}: {exc}"
            )

    return StructuredTool.from_function(
        coroutine=describe_image_from_path,
        name="describe_image",
        description=(
            "Look at an image in the sandbox and get back a written answer about it. "
            "`path` is absolute if it starts with `/`, otherwise it is taken as "
            f"relative to `{WORKDIR}`. `query` is what you want to know — ask something "
            "specific ('is the product label legible and undistorted?') rather than "
            "'describe this', and you get a usable answer. Reads "
            f"{', '.join(sorted(DESCRIBE_IMAGE_TYPES))} up to "
            f"{DESCRIBE_IMAGE_MAX_BYTES // (1024 * 1024)}MB. This always returns "
            "text, never native image content."
        ),
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
    "DESCRIBE_IMAGE",
    "DESCRIBE_IMAGE_MAX_BYTES",
    "DESCRIBE_IMAGE_MODEL",
    "DESCRIBE_IMAGE_TYPES",
    "FIRECRAWL_RESPONSE_MAX_BYTES",
    "TOOLSET_TOOL_NAMES",
    "WEB_TOOLSET",
    "custom_tool",
    "describe_image_tool",
    "resolve_tool_interrupts",
    "web_fetch_tool",
    "web_search_tool",
]
