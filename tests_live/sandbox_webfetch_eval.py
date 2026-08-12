"""Run our actual production web_fetch_tool (with a real E2B sandbox) against
10 real pages, verify each result by reading the sandbox back directly, then
leave the sandbox alive for 5 minutes so it can be inspected by hand in the
E2B dashboard before it's automatically killed.
"""

import asyncio
from pathlib import Path

from e2b import AsyncSandbox

from app.config import get_settings
from app.runtime.tools import WEB_FETCH_INLINE_MAX_CHARS, web_fetch_tool
from app.utils.sandbox import Sandbox

OUTPUT_DIR = Path(__file__).parent / "sandbox_webfetch_output"
INSPECT_WINDOW_SECONDS = 5 * 60

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


def _extract_path(result: str) -> str | None:
    marker = "saved to `"
    if marker not in result:
        return None
    start = result.index(marker) + len(marker)
    end = result.index("`", start)
    return result[start:end]


async def main() -> None:
    settings = get_settings()
    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set in .env")
    if not settings.e2b_api_key:
        raise RuntimeError("E2B_API_KEY is not set in .env")

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Long enough timeout that E2B's own idle timeout doesn't race our
    # deliberate 5-minute inspection window.
    native = await AsyncSandbox.create(
        template="base", timeout=INSPECT_WINDOW_SECONDS + 300, api_key=settings.e2b_api_key
    )
    sandbox = Sandbox(native.sandbox_id, "sandbox-eval-session", "org-sandbox-eval", native=native)
    print(f"Opened sandbox {native.sandbox_id}")

    tool = web_fetch_tool(sandbox)
    rows = []

    for label, url in PAGES:
        print(f"Fetching {label} ({url}) ...")
        result = await tool.ainvoke({"url": url})
        (OUTPUT_DIR / f"{label}_tool_result.txt").write_text(result, encoding="utf-8")

        if "no readable content" in result or "unavailable" in result or "failed" in result.lower():
            mode = "failed"
            verified = "n/a"
            path = None
        elif "too long to return directly" in result:
            mode = "sandboxed"
            path = _extract_path(result)
            if path:
                try:
                    saved_bytes = await sandbox.read_bytes(path, max_bytes=10 * 1024 * 1024)
                    verified = f"ok, {len(saved_bytes)} bytes in sandbox"
                except Exception as exc:
                    verified = f"VERIFY FAILED: {type(exc).__name__}: {exc}"
            else:
                verified = "VERIFY FAILED: no path found in tool result"
        else:
            mode = "inline"
            path = None
            verified = f"ok, {len(result)} chars returned inline"

        rows.append({
            "label": label, "url": url, "mode": mode,
            "result_chars": len(result), "path": path, "verified": verified,
        })
        print(f"  -> mode={mode}, {verified}")

    lines = [
        "# web_fetch_tool sandbox-path evaluation",
        "",
        f"*Inline threshold: {WEB_FETCH_INLINE_MAX_CHARS} characters*",
        "",
        "| # | Label | URL | Mode | Result chars | Sandbox path | Verified |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"| {i} | {r['label']} | {r['url']} | {r['mode']} | {r['result_chars']} | "
            f"{r['path'] or '-'} | {r['verified']} |"
        )
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved report to {OUTPUT_DIR / 'report.md'}")

    print("\n" + "=" * 70)
    print(f"SANDBOX LEFT RUNNING FOR {INSPECT_WINDOW_SECONDS // 60} MINUTES FOR INSPECTION")
    print(f"sandbox_id: {native.sandbox_id}")
    print("Open https://e2b.dev/dashboard and find this sandbox to browse")
    print(f".web_cache/ under the filesystem inspector, or run:")
    print(f"  uv run python tests_live/peek.py {native.sandbox_id}")
    print("=" * 70 + "\n")

    for remaining in range(INSPECT_WINDOW_SECONDS, 0, -30):
        print(f"  auto-killing in {remaining}s ...")
        await asyncio.sleep(min(30, remaining))

    await sandbox.kill()
    print(f"Killed sandbox {native.sandbox_id}")


if __name__ == "__main__":
    asyncio.run(main())
