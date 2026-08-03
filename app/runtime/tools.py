"""What the agent's declared tools mean at runtime.

Nothing here ever removes a tool. Whatever `create_deep_agent()` installs is
exactly what the model gets; toolset config only decides which of those tools
must stop and ask the user before running.
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
from app.utils.sandbox import WEB_CACHE_DIR, WORKDIR

if TYPE_CHECKING:
    from app.utils.sandbox import Sandbox

TOOLSET_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "agent_toolset_20260401": (
        "execute",
        "ls",
        "read_file",
        "read_image",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "write_todos",
        "task",
    ),
    "web_toolset_20260401": ("web_fetch", "web_search"),
}

AGENT_TOOLSET = "agent_toolset_20260401"
WEB_TOOLSET = "web_toolset_20260401"

# The model that does the looking, fixed rather than configurable: `read_image`
# is one capability with one price, and an operator swapping the vision model
# under an agent changes what its answers are worth without changing anything
# the agent can see.
READ_IMAGE_MODEL = "gemini-3.6-flash"

# Ten megabytes, against DeepAgents' 500KB ceiling on binary `read_file`. That
# ceiling is why this tool reads the bytes itself: a 1024x1024 PNG out of
# `image_generate` is routinely past it, so the images this platform makes are
# exactly the ones `read_file` refuses.
READ_IMAGE_MAX_BYTES = 10 * 1024 * 1024

# The formats Gemini takes, which is also DeepAgents' own image list.
READ_IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# Said to the vision model, not to the agent. The agent's question arrives
# underneath it, and the answer goes back as the tool result.
READ_IMAGE_INSTRUCTION = (
    "Answer the question about this image by describing what is actually in it. "
    "Every statement has to be something visible in the image. If the image does "
    "not settle the question, say so plainly rather than guessing."
)

FIRECRAWL_API_BASE = "https://api.firecrawl.dev/v2"
WEB_SEARCH_MAX_RESULTS = 10
WEB_FETCH_INLINE_MAX_CHARS = 8000

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


async def describe_image(data: bytes, mime_type: str, query: str) -> str:
    """Ask the vision model one question about one image.

    Separate from the tool so a test can replace the network call and still
    exercise everything around it.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    vision = ChatGoogleGenerativeAI(
        model=READ_IMAGE_MODEL, google_api_key=get_settings().gemini_api_key
    )
    encoded = base64.b64encode(data).decode("ascii")
    answer = await vision.ainvoke(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": f"{READ_IMAGE_INSTRUCTION}\n\nQuestion: {query}"},
                    {"type": "image_url", "image_url": f"data:{mime_type};base64,{encoded}"},
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


def read_image_tool(sandbox: Sandbox) -> StructuredTool:
    """Looking at an image, for a model that cannot.

    `read_file` already returns an image as a content block, which is the right
    answer when the agent's own model has eyes. This is the other case: the
    picture is looked at by a vision model and what comes back is prose, so a
    text-only model gets an answer rather than a block it has to drop.

    Every failure is returned as text. A tool that raises here would end the
    turn over a missing file, where a sentence lets the model try another path.
    """

    async def read_image(path: str, query: str) -> str:
        target = path if path.startswith("/") else f"{WORKDIR}/{path.lstrip('./')}"
        suffix = PurePosixPath(target).suffix.lower()
        mime_type = READ_IMAGE_TYPES.get(suffix)
        if mime_type is None:
            return (
                f"read_image cannot open {target}: it reads "
                f"{', '.join(sorted(READ_IMAGE_TYPES))} and this is {suffix or 'extensionless'}."
            )
        if not get_settings().gemini_api_key:
            return "read_image is unavailable: no vision model is configured on this server."

        try:
            data = await sandbox.read_bytes(target, max_bytes=READ_IMAGE_MAX_BYTES)
        except FileNotFoundError:
            return f"read_image found no file at {target}."
        except ValueError as exc:
            return f"read_image cannot open {target}: {exc}"
        except Exception as exc:
            return f"read_image could not read {target}: {type(exc).__name__}: {exc}"

        try:
            return await describe_image(data, mime_type, query)
        except Exception as exc:
            return f"read_image could not look at {target}: {type(exc).__name__}: {exc}"

    return StructuredTool.from_function(
        coroutine=read_image,
        name="read_image",
        description=(
            "Look at an image in the sandbox and get back a written answer about it. "
            "`path` is absolute if it starts with `/`, otherwise it is taken as "
            f"relative to `{WORKDIR}`. `query` is what you want to know — ask something "
            "specific ('is the product label legible and undistorted?') rather than "
            "'describe this', and you get a usable answer. Reads "
            f"{', '.join(sorted(READ_IMAGE_TYPES))} up to "
            f"{READ_IMAGE_MAX_BYTES // (1024 * 1024)}MB. Use this rather than read_file "
            "when you need to know what an image shows."
        ),
    )


def web_search_tool() -> StructuredTool:
    async def web_search(query: str) -> str:
        """Search the web and return the top results as title, URL, and snippet."""
        settings = get_settings()
        if not settings.firecrawl_api_key:
            return "web_search is unavailable: no Firecrawl API key is configured on this server."

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{FIRECRAWL_API_BASE}/search",
                    headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
                    json={"query": query, "limit": WEB_SEARCH_MAX_RESULTS},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            return f"web_search failed: Firecrawl returned {exc.response.status_code} for {query!r}."
        except httpx.HTTPError as exc:
            return f"web_search failed: {type(exc).__name__}: {exc}"

        results = (payload.get("data") or {}).get("web") or []
        if not results:
            return f"web_search found no results for {query!r}."

        lines = [f"Top {len(results)} results for {query!r}:", ""]
        for i, item in enumerate(results, start=1):
            title = item.get("title") or "(untitled)"
            url = item.get("url") or ""
            description = item.get("description") or ""
            lines.append(f"{i}. {title}\n   {url}\n   {description}")
        return "\n".join(lines)

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
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{FIRECRAWL_API_BASE}/scrape",
                    headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
                    json={"url": url, "formats": ["markdown"]},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            return f"web_fetch failed: Firecrawl returned {exc.response.status_code} for {url!r}."
        except httpx.HTTPError as exc:
            return f"web_fetch failed: {type(exc).__name__}: {exc}"

        data = payload.get("data") or {}
        markdown = data.get("markdown") or ""
        if not markdown:
            return f"web_fetch got no readable content from {url!r}."

        if len(markdown) <= WEB_FETCH_INLINE_MAX_CHARS:
            return markdown

        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        cache_path = f"{WEB_CACHE_DIR}/{digest}.md"
        await sandbox.write_bytes(cache_path, markdown.encode("utf-8"))
        summary = markdown[:WEB_FETCH_INLINE_MAX_CHARS]
        return (
            f"{url} is {len(markdown)} characters, too long to return directly. "
            f"The full page was saved to `{cache_path}` — use read_file to read more of it "
            f"if this summary is not enough.\n\n---\n\n{summary}"
        )

    return StructuredTool.from_function(
        coroutine=web_fetch,
        name="web_fetch",
        description=(
            "Fetch a public HTTP(S) URL and get back its content as clean markdown. "
            f"Pages up to {WEB_FETCH_INLINE_MAX_CHARS} characters come back directly; "
            "longer pages are saved to a file in the sandbox and a summary plus that "
            "file's path come back instead — use read_file on that path for the rest."
        ),
    )

def _policy(config: dict[str, Any], default: str) -> str:
    policy = config.get("permission_policy")
    if isinstance(policy, dict) and policy.get("type") in {"always_allow", "always_ask"}:
        return str(policy["type"])
    return default


__all__ = [
    "AGENT_TOOLSET",
    "READ_IMAGE_MAX_BYTES",
    "READ_IMAGE_MODEL",
    "READ_IMAGE_TYPES",
    "TOOLSET_TOOL_NAMES",
    "WEB_TOOLSET",
    "custom_tool",
    "describe_image",
    "read_image_tool",
    "resolve_tool_interrupts",
    "web_fetch_tool",
    "web_search_tool",
]
