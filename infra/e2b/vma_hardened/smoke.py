"""Exercise the candidate with VMA's real E2B lifecycle and security options."""

import argparse
import asyncio
import os
import sys
from typing import Any

from e2b import AsyncSandbox

from template import TEMPLATE_CANDIDATE


_GUEST_ATTEST = r"""
set -eu
PATH=/usr/bin:/bin
export PATH
test "$(/usr/bin/id -un)" = user
test "$(/usr/bin/id -u)" -ne 0
test "$(pwd)" = /workspace
test -x /usr/bin/python3
test ! -w /usr/bin
test ! -w /usr/lib
/usr/bin/python3 -I -S -c 'import json, os, pwd, stat, sys'
if test -x /usr/bin/sudo && /usr/bin/sudo -n true >/dev/null 2>&1; then
  exit 41
fi
test -w /workspace
test ! -w /mnt/session
test ! -w /mnt/memory
test ! -w /skills/custom
test ! -w /var/lib/vma
test -w /tmp
test -k /tmp
test -w /var/tmp
test -k /var/tmp
"""


_FORBIDDEN_WRITES = r"""
set -eu
for path in \
  /etc/vma-smoke \
  /usr/bin/vma-smoke \
  /usr/lib/vma-smoke \
  /mnt/session/vma-smoke \
  /mnt/memory/vma-unauthorized \
  /skills/custom/vma-smoke \
  /var/lib/vma/vma-smoke
do
  if touch "$path" 2>/dev/null; then
    echo "unexpected writable path: $path" >&2
    exit 42
  fi
done
"""


_EGRESS_BLOCKED = r"""
set -eu
if curl --connect-timeout 3 --max-time 5 https://example.com >/dev/null 2>&1; then
  echo "unexpected outbound network access" >&2
  exit 43
fi
"""


async def _checked(
    sandbox: Any,
    command: str,
    *,
    user: str | None = None,
) -> str:
    result = await sandbox.commands.run(command, user=user, timeout=60)
    if result.exit_code != 0:
        raise RuntimeError(
            f"command failed with exit code {result.exit_code}:\n"
            f"{command}\n{result.stderr}"
        )
    return result.stdout.strip()


async def _attest_running_sandbox(sandbox: Any) -> None:
    info = await sandbox.get_info()
    if info.allow_internet_access is not False:
        raise RuntimeError("E2B did not enforce blocked internet access")
    if info.network.get("allow_public_traffic") is not False:
        raise RuntimeError("E2B did not enforce private sandbox traffic")
    if info.lifecycle.get("on_timeout") != "pause":
        raise RuntimeError("E2B did not enforce pause-on-timeout")
    if info.lifecycle.get("auto_resume") is not False:
        raise RuntimeError("E2B unexpectedly enabled auto-resume")
    await _checked(sandbox, _GUEST_ATTEST)
    await _checked(sandbox, _FORBIDDEN_WRITES)
    await _checked(sandbox, _EGRESS_BLOCKED)


async def main(template_name: str) -> None:
    if not os.environ.get("E2B_API_KEY"):
        raise RuntimeError("E2B_API_KEY is required")

    current = await AsyncSandbox.create(
        template=template_name,
        timeout=300,
        secure=True,
        allow_internet_access=False,
        network={"allow_public_traffic": False},
        lifecycle={
            "on_timeout": {"action": "pause", "keep_memory": True},
            "auto_resume": False,
        },
    )
    sandbox_id = current.sandbox_id
    primary_error: BaseException | None = None

    try:
        await _attest_running_sandbox(current)
        await _checked(current, "echo persisted > /workspace/vma-smoke")

        # VMA bootstraps mutable memory and its private seal through the E2B
        # root control channel, never through guest sudo.
        await _checked(
            current,
            "install -d -o user -g user -m 0700 /mnt/memory/vma-smoke",
            user="root",
        )
        await _checked(
            current,
            "echo memory > /mnt/memory/vma-smoke/value",
        )
        await _checked(
            current,
            "touch /var/lib/vma/root-control-smoke",
            user="root",
        )

        await current.pause(keep_memory=True)
        current = await AsyncSandbox.connect(sandbox_id, timeout=300)

        await _attest_running_sandbox(current)
        await _checked(
            current,
            "test \"$(cat /workspace/vma-smoke)\" = persisted",
        )
        await _checked(
            current,
            "test \"$(cat /mnt/memory/vma-smoke/value)\" = memory",
        )
        await _checked(
            current,
            "test -f /var/lib/vma/root-control-smoke",
            user="root",
        )
        print("vma-hardened smoke passed")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            deleted = await current.kill()
            if deleted is not True:
                deleted = await AsyncSandbox.kill(sandbox_id)
            if deleted is not True:
                raise RuntimeError("E2B smoke sandbox was not deleted")
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            print(
                "warning: failed to delete E2B smoke sandbox "
                f"{sandbox_id}: {type(cleanup_error).__name__}",
                file=sys.stderr,
            )
            try:
                await AsyncSandbox.kill(sandbox_id)
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=TEMPLATE_CANDIDATE)
    args = parser.parse_args()
    asyncio.run(main(args.template))
