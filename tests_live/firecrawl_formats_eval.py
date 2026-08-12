"""Exploratory tests against real Firecrawl endpoints, answering four
concrete questions about how the service behaves:

1. Does `formats` actually control what comes back (markdown vs html)?
2. Does `scrapeOptions` on /v2/search actually attach full page markdown
   to each result?
3. Do PDF/DOCX/XLSX URLs go through the same /scrape call as a normal
   webpage, with no branching needed on our side?
4. Is Firecrawl's `summary` format a real AI-condensed summary, or just
   the beginning of the markdown?

Not a pass/fail test suite — this is investigative. Findings go into
firecrawl_formats_output/report.md for a human to read and judge.
"""

import asyncio
import json
from pathlib import Path

import httpx

from app.config import get_settings

OUTPUT_DIR = Path(__file__).parent / "firecrawl_formats_output"


async def scrape(client: httpx.AsyncClient, api_key: str, **kwargs) -> dict:
    response = await client.post(
        "https://api.firecrawl.dev/v2/scrape",
        headers={"Authorization": f"Bearer {api_key}"},
        json=kwargs,
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


async def search(client: httpx.AsyncClient, api_key: str, **kwargs) -> dict:
    response = await client.post(
        "https://api.firecrawl.dev/v2/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=kwargs,
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


# --- Module 1: formats controls output shape --------------------------------

async def module_1(client: httpx.AsyncClient, api_key: str, report: list[str]) -> None:
    report.append("## Module 1: does `formats` control the output shape?\n")
    url = "https://www.anthropic.com/news/claude-opus-4-8"

    for label, formats in [
        ("markdown_only", ["markdown"]),
        ("html_only", ["html"]),
        ("both", ["markdown", "html"]),
    ]:
        print(f"[1] scraping {url} with formats={formats} ...")
        try:
            payload = await scrape(client, api_key, url=url, formats=formats)
            data = payload.get("data") or {}
            keys = sorted(data.keys())
            (OUTPUT_DIR / f"module1_{label}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            has_markdown = "markdown" in data
            has_html = "html" in data
            report.append(
                f"- `formats={formats}` → data keys: {keys} "
                f"(has markdown: {has_markdown}, has html: {has_html})"
            )
            print(f"  -> keys={keys}")
        except Exception as exc:
            report.append(f"- `formats={formats}` → FAILED: {type(exc).__name__}: {exc}")
            print(f"  -> FAILED: {exc}")
    report.append("")


# --- Module 2: scrapeOptions on search ---------------------------------------

async def module_2(client: httpx.AsyncClient, api_key: str, report: list[str]) -> None:
    report.append("## Module 2: does `scrapeOptions` attach full markdown to search results?\n")
    query = "Firecrawl web scraping"

    print(f"[2] searching {query!r} WITHOUT scrapeOptions ...")
    try:
        payload_plain = await search(client, api_key, query=query, limit=3)
        results_plain = (payload_plain.get("data") or {}).get("web") or []
        (OUTPUT_DIR / "module2_without_scrapeoptions.json").write_text(
            json.dumps(results_plain, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        keys_plain = sorted(results_plain[0].keys()) if results_plain else []
        has_markdown_marker = any(
            c in (results_plain[0].get("description") or "") for c in ("#", "**", "\n")
        ) if results_plain else False
        report.append(f"- WITHOUT scrapeOptions → first result keys: {keys_plain}")
        report.append(
            f"  - description looks markdown-ish (has #/**/newline): {has_markdown_marker}"
        )
        print(f"  -> keys={keys_plain}")
    except Exception as exc:
        report.append(f"- WITHOUT scrapeOptions → FAILED: {type(exc).__name__}: {exc}")
        print(f"  -> FAILED: {exc}")

    print(f"[2] searching {query!r} WITH scrapeOptions ...")
    try:
        payload_scraped = await search(
            client, api_key, query=query, limit=3,
            scrapeOptions={"formats": ["markdown"]},
        )
        results_scraped = (payload_scraped.get("data") or {}).get("web") or []
        (OUTPUT_DIR / "module2_with_scrapeoptions.json").write_text(
            json.dumps(results_scraped, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        keys_scraped = sorted(results_scraped[0].keys()) if results_scraped else []
        markdown_len = len(results_scraped[0].get("markdown") or "") if results_scraped else 0
        report.append(f"- WITH scrapeOptions → first result keys: {keys_scraped}")
        report.append(f"  - first result markdown length: {markdown_len} chars")
        print(f"  -> keys={keys_scraped}, markdown_len={markdown_len}")
    except Exception as exc:
        report.append(f"- WITH scrapeOptions → FAILED: {type(exc).__name__}: {exc}")
        print(f"  -> FAILED: {exc}")
    report.append("")


# --- Module 3: PDF/DOCX/XLSX go through the same call ------------------------

MODULE_3_TARGETS = [
    ("webpage", "https://stripe.com"),
    ("pdf", "https://arxiv.org/pdf/1706.03762"),
    ("docx", "https://samplefile.com/samples/download/document/docx/docx_meeting_notes_sample.docx/"),
    ("xlsx", "https://samplefile.com/samples/download/document/xlsx/xlsx_formula_recalc_sample.xlsx/"),
]


async def module_3(client: httpx.AsyncClient, api_key: str, report: list[str]) -> None:
    report.append("## Module 3: do PDF/DOCX/XLSX go through the same /scrape call, unbranched?\n")
    for label, url in MODULE_3_TARGETS:
        print(f"[3] scraping {label} ({url}) ...")
        try:
            payload = await scrape(client, api_key, url=url, formats=["markdown"])
            data = payload.get("data") or {}
            markdown = data.get("markdown") or ""
            (OUTPUT_DIR / f"module3_{label}.md").write_text(markdown, encoding="utf-8")
            preview = " ".join(markdown[:300].split())
            report.append(
                f"- **{label}** (`{url}`) → {len(markdown)} chars. Preview: {preview!r}"
            )
            print(f"  -> ok, {len(markdown)} chars")
        except Exception as exc:
            report.append(f"- **{label}** (`{url}`) → FAILED: {type(exc).__name__}: {exc}")
            print(f"  -> FAILED: {exc}")
    report.append("")


# --- Module 4: is `summary` a real summary or just truncation? ---------------

async def module_4(client: httpx.AsyncClient, api_key: str, report: list[str]) -> None:
    report.append("## Module 4: is `summary` a real AI summary, or just truncated markdown?\n")
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"

    print(f"[4] scraping {url} with formats=['markdown','summary'] ...")
    try:
        payload = await scrape(client, api_key, url=url, formats=["markdown", "summary"])
        data = payload.get("data") or {}
        markdown = data.get("markdown") or ""
        summary = data.get("summary") or ""
        (OUTPUT_DIR / "module4_markdown.md").write_text(markdown, encoding="utf-8")
        (OUTPUT_DIR / "module4_summary.md").write_text(summary, encoding="utf-8")

        first_n = markdown[: len(summary)] if summary else ""
        identical_to_head = summary.strip() == first_n.strip() and bool(summary)

        report.append(f"- markdown length: {len(markdown)} chars")
        report.append(f"- summary length: {len(summary)} chars")
        report.append(
            f"- summary is byte-identical to markdown's first N chars "
            f"(would indicate fake/truncated summary): {identical_to_head}"
        )
        report.append(f"- summary content:\n\n> {summary}\n")
        print(f"  -> markdown={len(markdown)} chars, summary={len(summary)} chars")
        print(f"  -> identical to head truncation: {identical_to_head}")
    except Exception as exc:
        report.append(f"- FAILED: {type(exc).__name__}: {exc}")
        print(f"  -> FAILED: {exc}")
    report.append("")


async def main() -> None:
    settings = get_settings()
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set in .env")

    OUTPUT_DIR.mkdir(exist_ok=True)
    report: list[str] = ["# Firecrawl formats/behavior investigation\n"]

    async with httpx.AsyncClient() as client:
        await module_1(client, settings.firecrawl_api_key, report)
        await module_2(client, settings.firecrawl_api_key, report)
        await module_3(client, settings.firecrawl_api_key, report)
        await module_4(client, settings.firecrawl_api_key, report)

    report_path = OUTPUT_DIR / "report.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"\nSaved report to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())