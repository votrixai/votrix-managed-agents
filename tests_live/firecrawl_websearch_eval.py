"""End-to-end evaluation of our web_search tool: run 10 real queries through
Firecrawl, then have Claude judge relevance and usefulness of the results.

Run directly: uv run python tests_live/firecrawl_websearch_eval.py
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from app.config import get_settings

OUTPUT_DIR = Path(__file__).parent / "firecrawl_websearch_output"
MODEL = "claude-sonnet-4-6"

QUERIES = [
    ("factual", "what is the capital of Australia"),
    ("comparison", "Python vs JavaScript for beginners"),
    ("howto", "how to set up a virtual environment in Python"),
    ("news", "latest AI model releases 2026"),
    ("definition", "what is retrieval augmented generation"),
    ("troubleshooting", "fix git merge conflict command line"),
    ("reviews", "best noise cancelling headphones 2026 reviews"),
    ("local_practical", "weather forecast New York this week"),
    ("technical", "Python asyncio gather vs wait_for"),
    ("academic", "recent research on large language model hallucination"),
]

RUBRIC_PROMPT = """You are evaluating web search results returned by Firecrawl \
for a given query, as they would be used by an AI agent deciding which link \
to open next.

QUERY: {query}

RESULTS (title, url, description for each):
{results_text}

Score these results on:
- relevance (1-5): do the results actually address the query?
- diversity (1-5): do results come from varied, reasonably authoritative \
sources rather than duplicates or low-quality sites?
- usefulness (1-5): would an agent reading just the title+description be able \
to pick the right link to open next, without fetching every one?

Also give a one-sentence reason, noting if the query type seems to matter for \
how well search did here.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"relevance": <1-5>, "diversity": <1-5>, "usefulness": <1-5>, "reason": "<one sentence>"}}
"""


def _format_results(results: list[dict]) -> str:
    if not results:
        return "(no results returned)"
    lines = []
    for i, item in enumerate(results, start=1):
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        description = (item.get("description") or "")[:200]
        lines.append(f"{i}. {title}\n   {url}\n   {description}")
    return "\n".join(lines)


async def search_one(client: httpx.AsyncClient, api_key: str, label: str, query: str) -> dict:
    start = time.monotonic()
    try:
        response = await client.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "limit": 10},
            timeout=30.0,
        )
        elapsed = time.monotonic() - start
        response.raise_for_status()
        results = (response.json().get("data") or {}).get("web") or []
        (OUTPUT_DIR / f"{label}.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {"label": label, "query": query, "status": "ok", "results": results, "seconds": round(elapsed, 2)}
    except Exception as exc:
        elapsed = time.monotonic() - start
        return {"label": label, "query": query, "status": f"FAILED: {type(exc).__name__}: {exc}", "results": [], "seconds": round(elapsed, 2)}


async def score_one(model: ChatAnthropic, query: str, results: list[dict]) -> dict:
    prompt = RUBRIC_PROMPT.format(query=query, results_text=_format_results(results))
    response = await model.ainvoke([HumanMessage(content=prompt)])
    content = response.content if isinstance(response.content, str) else str(response.content)
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        return {"relevance": None, "diversity": None, "usefulness": None,
                "reason": f"model did not return valid JSON: {content[:200]!r}"}


async def main() -> None:
    settings = get_settings()
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set in .env")
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

    OUTPUT_DIR.mkdir(exist_ok=True)
    model = ChatAnthropic(model=MODEL, api_key=settings.anthropic_api_key)
    rows = []

    async with httpx.AsyncClient() as client:
        for label, query in QUERIES:
            print(f"Searching {label} ({query!r}) ...")
            searched = await search_one(client, settings.firecrawl_api_key, label, query)
            print(f"  -> {searched['status']}, {len(searched['results'])} results, {searched['seconds']}s")

            if not searched["results"]:
                rows.append({**searched, "relevance": None, "diversity": None,
                             "usefulness": None, "reason": searched["status"]})
                continue

            print(f"  Scoring {label} ...")
            scored = await score_one(model, query, searched["results"])
            rows.append({**searched, **scored})
            print(f"  -> relevance={scored.get('relevance')}: {scored.get('reason')}")

    lines = [
        "# Firecrawl web_search evaluation (search + AI score)",
        "",
        "| # | Label | Query | Results | Time(s) | Relevance | Diversity | Usefulness | Reason |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"| {i} | {r['label']} | {r['query']} | {len(r.get('results', []))} | {r.get('seconds')} | "
            f"{r.get('relevance')} | {r.get('diversity')} | {r.get('usefulness')} | {r.get('reason')} |"
        )
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved report to {OUTPUT_DIR / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
