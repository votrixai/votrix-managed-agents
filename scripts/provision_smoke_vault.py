"""Idempotently provision the BYOK Vault used by hosted acceptance smokes.

The operator and model keys are accepted through environment variables only.
Output contains resource identifiers and creation state, never credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SDK_SOURCE = Path(__file__).resolve().parents[1] / "sdks" / "python" / "src"
if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from votrix import AsyncVotrix  # noqa: E402


PROVISIONER = "scripts/provision_smoke_vault.py"


@dataclass(frozen=True)
class ProvisionedVault:
    vault_id: str
    credential_id: str
    provider: str
    vault_created: bool
    credential_created: bool


async def provision_smoke_vault(
    client: Any,
    *,
    provider: str,
    model_api_key: str,
    display_name: str,
) -> ProvisionedVault:
    provider = provider.strip()
    model_api_key = model_api_key.strip()
    display_name = display_name.strip()
    if not provider:
        raise ValueError("provider must not be blank")
    if not model_api_key:
        raise ValueError("model API key must not be blank")
    if not display_name:
        raise ValueError("display name must not be blank")

    available_providers = {item.id async for item in client.model_providers.list()}
    if provider not in available_providers:
        raise ValueError(f"model provider is not enabled by the server: {provider}")

    vault = None
    async for candidate in client.vaults.list():
        if candidate.metadata.get("provisioned_by") == PROVISIONER:
            vault = candidate
            break

    vault_created = vault is None
    if vault is None:
        vault = await client.vaults.create(
            display_name=display_name,
            metadata={"provisioned_by": PROVISIONER, "purpose": "hosted_acceptance"},
        )

    credential = None
    async for candidate in client.vaults.model_credentials.list(vault.id):
        if candidate.model_provider == provider:
            credential = candidate
            break

    credential_created = credential is None
    if credential is None:
        credential = await client.vaults.model_credentials.create(
            vault.id,
            provider=provider,
            api_key=model_api_key,
            display_name=f"{provider} acceptance credential",
            metadata={"provisioned_by": PROVISIONER, "purpose": "hosted_acceptance"},
        )

    return ProvisionedVault(
        vault_id=vault.id,
        credential_id=credential.id,
        provider=provider,
        vault_created=vault_created,
        credential_created=credential_created,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("VMA_SMOKE_BASE_URL"))
    parser.add_argument("--provider", default=os.environ.get("VMA_SMOKE_PROVIDER", "openrouter"))
    parser.add_argument(
        "--display-name",
        default=os.environ.get("VMA_SMOKE_VAULT_NAME", "Hosted acceptance BYOK"),
    )
    return parser


async def _main_async(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    operator_api_key = os.environ.get("VMA_SMOKE_API_KEY", "").strip()
    model_api_key = os.environ.get("VMA_SMOKE_MODEL_API_KEY", "").strip()
    if not args.base_url:
        parser.error("--base-url or VMA_SMOKE_BASE_URL is required")
    if not operator_api_key:
        parser.error("VMA_SMOKE_API_KEY is required")
    if not model_api_key:
        parser.error("VMA_SMOKE_MODEL_API_KEY is required")

    async with AsyncVotrix(
        api_key=operator_api_key,
        base_url=args.base_url,
        max_retries=2,
    ) as client:
        result = await provision_smoke_vault(
            client,
            provider=args.provider,
            model_api_key=model_api_key,
            display_name=args.display_name,
        )
    print(json.dumps(asdict(result), separators=(",", ":")))


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    asyncio.run(_main_async(args, parser))


if __name__ == "__main__":
    main()
