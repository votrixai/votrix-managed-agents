"""Exercise the real container, the real shell, and the real rules.

`tests/test_sandbox_api.py` stubs E2B, so everything the launcher and the
collector actually depend on — redirection, `timeout`, `base64`, a detached
process outliving the call that started it — is untested there by
construction. This runs those against a real container.

    cd infra/e2b/hf_lint
    set -a && . ../../../.env && set +a
    ../../../.venv/bin/python smoke.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.services.sandboxes import _collector, _parse, _start  # noqa: E402
from app.models.sandbox import MAX_OUTPUT_CHARS  # noqa: E402
from template import TEMPLATE_CANDIDATE, EXPECTED_RULE_COUNT, LINT_ENTRYPOINT  # noqa: E402

from e2b import AsyncSandbox  # noqa: E402

WORKDIR = "/home/user"
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + str(detail)) if detail else ''}")


async def run_exec(
    sandbox: AsyncSandbox,
    command: str,
    *,
    exec_id: str,
    timeout_seconds: int = 60,
    wait_seconds: int = 60,
    cwd: str | None = None,
) -> dict:
    """Exactly what the service does, against a real container."""
    directory = f"{WORKDIR}/execs/{exec_id}"
    workdir = cwd or directory

    # One call, exactly as the service issues it.
    result = await sandbox.commands.run(
        _start(directory, workdir, command, timeout_seconds)
        + _collector(directory, wait_seconds),
        timeout=wait_seconds + 30,
    )
    report = _parse(result.stdout or "")
    import base64

    return {
        "rc": report.get("RC"),
        "outlen": int(report.get("OUTLEN") or 0),
        "stdout": base64.b64decode(report.get("OUT") or "").decode("utf-8", "replace"),
        "stderr": base64.b64decode(report.get("ERR") or "").decode("utf-8", "replace"),
        "dir": directory,
    }


def composition_zip() -> bytes:
    """The smallest composition the rules pass, plus one they do not."""
    html = """<!doctype html><html><head><script>
window.__timelines = window.__timelines || {};
window.__timelines["main"] = { paused: true };
</script></head><body>
<div data-composition-id="main" data-start="0" data-duration="5"
     data-width="1920" data-height="1080"></div>
</body></html>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
    return buffer.getvalue()


def broken_zip() -> bytes:
    """A composition referencing an asset that is not there.

    This is the case the whole thing exists for: HeyGen renders it, bills for
    it, and returns a video with a hole where the image should be.
    """
    html = """<!doctype html><html><head><script>
window.__timelines = window.__timelines || {};
window.__timelines["main"] = { paused: true };
</script></head><body>
<div data-composition-id="main" data-start="0" data-duration="5"
     data-width="1920" data-height="1080">
  <img id="shot" class="clip" src="assets/missing.png"
       data-start="0" data-duration="5" data-track-index="1" />
</div>
</body></html>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
    return buffer.getvalue()


async def main() -> None:
    if not os.environ.get("E2B_API_KEY"):
        sys.exit("E2B_API_KEY is required")

    print(f"starting {TEMPLATE_CANDIDATE}")
    began = time.perf_counter()
    sandbox = await AsyncSandbox.create(template=TEMPLATE_CANDIDATE, timeout=300)
    print(f"  started in {(time.perf_counter() - began) * 1000:.0f} ms\n")

    try:
        print("the image")
        probe = await sandbox.commands.run(f"node {LINT_ENTRYPOINT} --selftest")
        check(
            "the bundled linter runs and reports its rule count",
            f"{EXPECTED_RULE_COUNT} rules" in probe.stdout,
            probe.stdout.strip(),
        )
        modules = await sandbox.commands.run("ls /opt/hflint")
        check(
            "node_modules is not in the image",
            "node_modules" not in modules.stdout,
            modules.stdout.split(),
        )

        print("\nthe shell the service relies on")
        result = await run_exec(sandbox, "echo hello", exec_id="e-basic")
        check("a command's output comes back", result["stdout"].strip() == "hello")
        check("its exit code comes back", result["rc"] == "0")

        result = await run_exec(sandbox, "exit 3", exec_id="e-code")
        check("a non-zero exit is reported as itself", result["rc"] == "3")

        began = time.perf_counter()
        result = await run_exec(
            sandbox, "sleep 30", exec_id="e-timeout", timeout_seconds=2, wait_seconds=20
        )
        elapsed = time.perf_counter() - began
        check(
            "a command past its timeout is killed and reports 124",
            result["rc"] == "124",
            f"{elapsed:.1f}s",
        )

        result = await run_exec(
            sandbox,
            "head -c 200000000 /dev/zero | tr '\\0' 'x'",
            exec_id="e-flood",
            wait_seconds=60,
        )
        check(
            "a flood of output is truncated in the container, not here",
            len(result["stdout"]) == MAX_OUTPUT_CHARS
            and result["outlen"] == 200_000_000,
            f"container held {result['outlen'] / 1e6:.0f} MB, "
            f"{len(result['stdout']) / 1024:.0f} KB came back",
        )

        nasty = "printf '%s' \"$(echo inner)\"; echo \"quotes ' and \\\" survive\""
        result = await run_exec(sandbox, nasty, exec_id="e-quoting")
        check(
            "a command full of quoting runs as itself",
            "inner" in result["stdout"] and "quotes" in result["stdout"],
            result["stdout"].strip(),
        )

        result = await run_exec(sandbox, "sleep 8; echo late", exec_id="e-async",
                                wait_seconds=1)
        check("a command outliving the wait is still running", result["rc"] == "")
        await asyncio.sleep(9)
        later = await sandbox.commands.run(_collector(result["dir"], 0), timeout=30)
        report = _parse(later.stdout or "")
        import base64

        check(
            "and is found afterwards, with its output",
            report.get("RC") == "0"
            and "late" in base64.b64decode(report.get("OUT") or "").decode(),
        )

        print("\nthe rules")
        await sandbox.files.write("/home/user/good.zip", composition_zip())
        result = await run_exec(
            sandbox,
            f"mkdir -p p && unzip -oq /home/user/good.zip -d p "
            f"&& node {LINT_ENTRYPOINT} p",
            exec_id="e-lint-ok",
        )
        good = json.loads(result["stdout"] or "{}")
        check(
            "a clean composition passes",
            good.get("errors") == 0 and good.get("rule_count") == EXPECTED_RULE_COUNT,
            f"{good.get('rule_count')} rules, {good.get('errors')} errors",
        )

        await sandbox.files.write("/home/user/bad.zip", broken_zip())
        result = await run_exec(
            sandbox,
            f"mkdir -p q && unzip -oq /home/user/bad.zip -d q "
            f"&& node {LINT_ENTRYPOINT} q",
            exec_id="e-lint-bad",
        )
        bad = json.loads(result["stdout"] or "{}")
        codes = {finding.get("code") for finding in bad.get("findings") or []}
        check(
            "a composition referencing a missing asset is caught",
            bad.get("errors", 0) > 0,
            f"{bad.get('errors')} errors: {sorted(codes)}",
        )
        check(
            "and every finding carries the hint an agent acts on",
            all(
                finding.get("fixHint")
                for finding in bad.get("findings") or []
                if finding.get("severity") == "error"
            ),
        )
    finally:
        await sandbox.kill()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
