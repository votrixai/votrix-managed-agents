"""Layer 4 live verification: build the hf-full recipe on real E2B and prove
Node 22 + hyperframes + Chrome are actually inside the container.

Faithful to the production path: it reuses the exact per-step dispatch from
`Image.build` (`_install` + PACKAGE_MANAGERS, `run` -> run_cmd as root), so a
green run here means the build loop I changed works end to end on E2B.

Only needs E2B_API_KEY (image build + a bare sandbox). It does not start a VMA
session, so it needs no OpenRouter/encryption keys.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from e2b import AsyncSandbox, Template, default_build_logger

# Import the real dispatch so this tests the shipped code, not a copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.utils.sandbox import PACKAGE_MANAGERS, _install  # noqa: E402


# The exact recipe declared in backend tool_environments.py -> "hf-full".
STEPS = [
    {"run": "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"},
    {
        "apt": [
            "nodejs", "ffmpeg", "fonts-liberation", "unzip", "zip",
            "libnss3", "libatk-bridge2.0-0", "libatk1.0-0", "libcups2",
            "libdrm2", "libgbm1", "libasound2", "libxkbcommon0",
            "libxcomposite1", "libxdamage1", "libxfixes3", "libxrandr2",
            "libpangocairo-1.0-0",
        ]
    },
    # The base image ships Node v20 at /usr/local/bin (ahead of NodeSource's
    # /usr/bin on PATH), so make the v22 just installed win before anything runs
    # under it — otherwise hyperframes' own >=22 guard rejects the CLI.
    {
        "run": (
            "ln -sf /usr/bin/node /usr/local/bin/node && "
            "ln -sf /usr/bin/npm /usr/local/bin/npm && "
            "ln -sf /usr/bin/npx /usr/local/bin/npx && node --version"
        )
    },
    {"npm": ["hyperframes@0.8.30"]},
    # browser ensure runs as root at build, but sessions run as `user`
    # (HOME=/home/user). Cache Chrome into that home and hand it over, or the
    # runtime user's doctor/check looks in an empty ~/.cache and re-downloads.
    {
        "run": (
            "mkdir -p /home/user/.cache && "
            "HOME=/home/user hyperframes browser ensure && "
            "chown -R user:user /home/user/.cache"
        )
    },
]


def _load_env() -> None:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _build_template() -> str:
    builder = Template().from_base_image()
    for step in STEPS:  # mirrors Image.build exactly
        command = step.get("run")
        if command is not None:
            builder = builder.run_cmd(command, user="root")
            continue
        for manager in PACKAGE_MANAGERS:
            entries = step.get(manager)
            if entries:
                builder = _install(builder, manager, entries)
                break

    name = f"hf-full-verify:v{int(time.time())}"
    print(f"[build] starting {name} ...", flush=True)
    info = Template.build(
        builder, name, cpu_count=2, memory_mb=3072, on_build_logs=default_build_logger()
    )
    print(f"[build] done template_id={info.template_id}", flush=True)
    return name


async def _checked(sandbox, command: str) -> str:
    result = await sandbox.commands.run(command, timeout=120)
    status = "OK " if result.exit_code == 0 else "FAIL"
    print(f"  [{status}] {command}\n       -> {result.stdout.strip() or result.stderr.strip()}", flush=True)
    return result.stdout.strip() if result.exit_code == 0 else ""


async def _smoke(template_name: str) -> bool:
    sandbox = await AsyncSandbox.create(template=template_name, timeout=300)
    try:
        node = await _checked(sandbox, "node --version")
        hf = await _checked(sandbox, "npx --yes hyperframes --version")
        chrome = await _checked(
            sandbox,
            "npx --yes hyperframes doctor --json | node -e \"let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{const c=JSON.parse(s).checks.find(x=>x.name==='Chrome');console.log(c&&c.ok?'chrome-ok':'chrome-missing')})\"",
        )
        ok = node.startswith("v22") and "0.8.30" in hf and chrome == "chrome-ok"
        print(f"\nRESULT: node22={node.startswith('v22')} hyperframes={('0.8.30' in hf)} chrome={(chrome=='chrome-ok')}", flush=True)
        return ok
    finally:
        await sandbox.kill()


def main() -> int:
    _load_env()
    if not os.environ.get("E2B_API_KEY"):
        print("E2B_API_KEY missing from .env", file=sys.stderr)
        return 2
    name = _build_template()
    ok = asyncio.run(_smoke(name))
    print("\n" + ("hf-full LIVE VERIFY PASSED" if ok else "hf-full LIVE VERIFY FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
