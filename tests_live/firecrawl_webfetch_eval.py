"""End-to-end evaluation of our web_fetch tool: fetch 10 real pages through
Firecrawl, then have Claude score each result's quality.

Run directly: uv run python tests_live/firecrawl_webfetch_eval.py
"""

import asyncio
import re
import time
from pathlib import Path

import httpx
from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import HumanMessage
import json

from app.config import get_settings

OUTPUT_DIR = Path(__file__).parent / "firecrawl_webfetch_output"
MODEL = "deepseek-v4-flash"

PAGES = [
    ("news_anthropic", "https://www.anthropic.com/news/claude-opus-4-8"),
    ("spa_stripe", "https://stripe.com"),
    ("table_wikipedia_population", "https://en.wikipedia.org/wiki/List_of_countries_by_population_(United_Nations)"),
    ("ecommerce_amazon_book", "https://www.amazon.com/Clean-Code-Handbook-Software-Craftsmanship/dp/0132350882"),
    ("docs_python_tutorial", "https://docs.python.org/3/tutorial/index.html"),
    ("forum_stackoverflow_yield", "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do"),
    ("paywall_nytimes", "https://www.nytimes.com/"),
    ("chinese_baidu_baike_python", "https://baike.baidu.com/item/Python/407313"),
    ("images_wikipedia_solar_system", "https://en.wikipedia.org/wiki/Solar_System"),
    ("long_wikipedia_python", "https://en.wikipedia.org/wiki/Python_(programming_language)"),
]

RUBRIC_PROMPT = """You are evaluating how well a web-scraping tool (Firecrawl) \
converted a real webpage into clean markdown for an AI agent to read.

URL: {url}

--- FIRECRAWL MARKDOWN OUTPUT ({markdown_chars} characters) ---
{markdown_excerpt}

--- NAIVE RAW HTML TEXT BASELINE ({raw_chars} characters, tags stripped, no JS \
rendering — this is what a simple HTTP fetch sees, for comparison only) ---
{raw_excerpt}

Score the Firecrawl markdown output on these 4 dimensions, 1-5 each:
- completeness: does it look like the actual page content made it through, \
without obvious missing sections?
- cleanliness: is it free of navigation/ads/boilerplate noise?
- structure: are headings, tables, lists, code blocks preserved sensibly?
- accuracy: any signs of garbled text, broken tables, duplicated content, or \
placeholder/error text instead of real content?

Also give one overall verdict: "good", "partial", or "poor", and a one-sentence \
reason. If the raw HTML baseline is empty or near-empty (a JS-rendered page), \
say so explicitly — that means the markdown content came from Firecrawl \
actually rendering the page, a meaningful positive signal if the markdown \
itself has real substantial content.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"completeness": <1-5>, "cleanliness": <1-5>, "structure": <1-5>, \
"accuracy": <1-5>, "verdict": "good|partial|poor", "reason": "<one sentence>"}}
"""


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def fetch_one(client: httpx.AsyncClient, api_key: str, label: str, url: str) -> dict:
    start = time.monotonic()
    try:
        response = await client.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=60.0,
        )
        elapsed = time.monotonic() - start
        response.raise_for_status()
        markdown = (response.json().get("data") or {}).get("markdown") or ""
        (OUTPUT_DIR / f"{label}.md").write_text(markdown, encoding="utf-8")
        return {"label": label, "url": url, "status": "ok", "markdown": markdown, "seconds": round(elapsed, 2)}
    except Exception as exc:
        elapsed = time.monotonic() - start
        return {"label": label, "url": url, "status": f"FAILED: {type(exc).__name__}: {exc}", "markdown": "", "seconds": round(elapsed, 2)}


async def fetch_raw_baseline(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url, timeout=20.0, follow_redirects=True)
        return _strip_html(response.text)[:20000]
    except Exception as exc:
        return f"(raw fetch failed: {type(exc).__name__}: {exc})"


async def score_one(model: ChatDeepSeek, url: str, markdown: str, raw_text: str) -> dict:
    prompt = RUBRIC_PROMPT.format(
        url=url, markdown_chars=len(markdown), markdown_excerpt=markdown,
        raw_chars=len(raw_text), raw_excerpt=raw_text[:3000],
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    content = response.content if isinstance(response.content, str) else str(response.content)
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        return {"completeness": None, "cleanliness": None, "structure": None, "accuracy": None,
                "verdict": "unscored", "reason": f"model did not return valid JSON: {content[:200]!r}"}

async def main() -> None:
    settings = get_settings()
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set in .env")
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set in .env")

    OUTPUT_DIR.mkdir(exist_ok=True)
    model = ChatDeepSeek(model=MODEL, api_key=settings.deepseek_api_key)
    rows = []

    async with httpx.AsyncClient() as client:
        for label, url in PAGES:
            print(f"Fetching {label} ({url}) ...")
            fetched = await fetch_one(client, settings.firecrawl_api_key, label, url)
            print(f"  -> {fetched['status']}, {len(fetched['markdown'])} chars, {fetched['seconds']}s")

            if not fetched["markdown"]:
                rows.append({**fetched, "completeness": None, "cleanliness": None,
                             "structure": None, "accuracy": None, "verdict": "failed", "reason": fetched["status"]})
                continue

            raw_text = await fetch_raw_baseline(client, url)
            print(f"  Scoring {label} ...")
            scored = await score_one(model, url, fetched["markdown"], raw_text)
            rows.append({**fetched, **scored})
            print(f"  -> {scored.get('verdict')}: {scored.get('reason')}")

    lines = [
        "# Firecrawl web_fetch evaluation (fetch + AI score)",
        "",
        "| # | Label | URL | Chars | Time(s) | Completeness | Cleanliness | Structure | Accuracy | Verdict | Reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"| {i} | {r['label']} | {r['url']} | {len(r.get('markdown', ''))} | {r.get('seconds')} | "
            f"{r.get('completeness')} | {r.get('cleanliness')} | {r.get('structure')} | "
            f"{r.get('accuracy')} | {r.get('verdict')} | {r.get('reason')} |"
        )
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved report to {OUTPUT_DIR / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
