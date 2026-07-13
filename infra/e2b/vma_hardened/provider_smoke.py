"""Exercise VMA's real E2B provider, bootstrap seal, and reconnect path."""

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.runtime.sandbox_providers import (
    E2BSandboxProvider,
    SandboxOwner,
    SandboxPolicy,
)

from template import TEMPLATE_CANDIDATE


async def main(template_name: str) -> None:
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        raise RuntimeError("E2B_API_KEY is required")

    provider = E2BSandboxProvider(
        api_key,
        default_template=template_name,
        timeout=300,
        command_timeout=60,
        guest_user="user",
    )
    owner = SandboxOwner("wrkspc_e2b_smoke", "session_e2b_smoke")
    policy = SandboxPolicy(
        network_access="none",
        timeout_seconds=300,
        command_timeout_seconds=60,
        workdir="/workspace",
        auto_pause=True,
    )
    fixed_content = b"sealed VMA input\n"
    immutable_manifest = {
        "/mnt/session/uploads/input.txt": hashlib.sha256(fixed_content).hexdigest()
    }
    digest = "sha256:" + hashlib.sha256(b"vma-provider-smoke-v1").hexdigest()

    connection = await provider.provision(owner, policy)
    reference = connection.reference
    primary_error: BaseException | None = None

    try:
        await provider.bootstrap(
            connection,
            files=[
                ("/mnt/session/uploads/input.txt", fixed_content),
                ("/workspace/seed.txt", b"mutable seed\n"),
            ],
            read_only_paths=("/mnt/session/uploads/input.txt",),
            mutable_roots=("/workspace",),
            digest=digest,
        )
        await provider.verify_bootstrap(
            connection,
            digest=digest,
            immutable_manifest=immutable_manifest,
        )

        native = connection.native
        result = await native.commands.run(
            "set -eu; "
            "test ! -w /mnt/session; "
            "test ! -w /mnt/session/uploads/input.txt; "
            "echo persisted-by-provider > /workspace/provider-smoke",
            user="user",
            timeout=60,
        )
        if result.exit_code != 0:
            raise RuntimeError("guest provider smoke command failed")

        await provider.pause(reference, owner)
        resumed = await provider.connect(reference, owner, policy)
        await provider.verify_bootstrap(
            resumed,
            digest=digest,
            immutable_manifest=immutable_manifest,
        )

        resumed_native = resumed.native
        result = await resumed_native.commands.run(
            "set -eu; "
            "test \"$(cat /workspace/provider-smoke)\" = persisted-by-provider; "
            "test \"$(cat /mnt/session/uploads/input.txt)\" = 'sealed VMA input'",
            user="user",
            timeout=60,
        )
        if result.exit_code != 0:
            raise RuntimeError("resumed provider smoke command failed")

        print("VMA E2B provider smoke passed")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await provider.delete(reference, owner)
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            print(
                "warning: failed to delete VMA provider smoke sandbox "
                f"{reference.external_id}: {type(cleanup_error).__name__}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=TEMPLATE_CANDIDATE)
    args = parser.parse_args()
    asyncio.run(main(args.template))
