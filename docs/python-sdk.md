---
title: Python SDK
description: Use AsyncVotrix or the synchronous provisioning client while retaining the AsyncAnthropic compatibility path.
---

VMA has two deliberately separate Python client contracts:

| Client | Purpose |
| --- | --- |
| `from votrix import AsyncVotrix` | Recommended async client for VMA resources, model-provider discovery, and provider-based BYOK. |
| `from votrix import Votrix` | Synchronous GA wrapper for API keys, model providers, Vaults, and native model Credentials. |
| `from anthropic import AsyncAnthropic` | Compatibility client for the overlapping Claude Managed Agents wire surface. |

They call the same VMA control plane, but they are not interchangeable type
systems. Native SDK tests prove the VMA contract; strict Anthropic SDK tests
prove only the covered compatibility contract. Neither proves Claude-identical
runtime behavior.

## Package boundary

The SDK lives at `sdks/python` and is built as the independent distribution
`votrix`. Its import namespace is also `votrix`:

```python
from votrix import AsyncVotrix
```

The repository root remains the VMA server distribution and continues to
export its embedding factory from `votrix_managed_agents`. Keeping those
projects separate prevents the SDK release lifecycle from changing the server
package or importing FastAPI/runtime dependencies into client applications.

The first PyPI release is still pending. For local development:

```bash
python -m pip install -e ./sdks/python
```

After publication:

```bash
python -m pip install votrix
```

## Client setup

Supply the workspace API key and the URL of your VMA deployment explicitly, or
use `VOTRIX_API_KEY` and `VOTRIX_BASE_URL`:

```python
from votrix import AsyncVotrix

client = AsyncVotrix(
    api_key="vma_...",
    base_url="https://your-vma.example.com",
)
```

The client sends `x-api-key` and the native
`votrix-managed-agents-beta` header by default. Bearer authentication is an
explicit constructor option. Use `async with` or call `await client.close()` to
close its connection pool.

## Transport behavior

Both clients default to two bounded retries for connection failures, timeouts,
`408`, `429`, selected transient `5xx` responses, and `529` when the request is
replay-safe. Reads are replay-safe by method; Session create and event
submission automatically receive an `Idempotency-Key`. Supply your own
`idempotency_key` when identity
must survive a caller-level retry or process restart. Retry timing honors
`Retry-After` before applying bounded backoff.

List methods expose cursor pages and lazy traversal. The async client also
provides reconnecting Session SSE with `Last-Event-ID` replay suppression and
true incremental file downloads with `stream=True`; callers retain explicit
ownership of stream closure.

Typed SDK exceptions expose `status_code`, `error_type`, stable `error_code`,
`request_id`, response headers, `retry_after`, and normalized rate-limit
headers where applicable. Exception strings and representations never include
request bodies or API secrets.

## Workspace API keys

Tenant administrators can create, inspect, rotate, and revoke workspace-scoped
keys through either client:

```python
created = await client.api_keys.create(
    name="production",
    scopes=["api", "api_keys:manage"],
)
persist_secret(created.secret.get_secret_value())

rotated = await client.api_keys.rotate(created.id, reason="scheduled rollover")
persist_secret(rotated.secret.get_secret_value())
```

Only create and rotate responses contain the one-time plaintext key, wrapped
as `SecretStr`. List, retrieve, and revoke return safe metadata only. The SDK
does not expose the deferred Memory Store or generic Vault Credential APIs.

## Provider discovery and end-user BYOK

Applications should never ask an end user to enter an internal name such as
`OPENROUTER_API_KEY`. Discover the public provider IDs from VMA, create a Vault
for the user, and submit the provider ID with the write-only key:

```python
async with AsyncVotrix(
    api_key="vma_...",
    base_url="https://your-vma.example.com",
) as client:
    providers = [item async for item in client.model_providers.list()]

    vault = await client.vaults.create(display_name="End-user credentials")
    credential = await client.vaults.model_credentials.create(
        vault_id=vault.id,
        provider="openrouter",
        api_key=end_user_api_key,
    )

    credential = await client.vaults.model_credentials.rotate(
        vault.id,
        credential.id,
        api_key=rotated_end_user_api_key,
    )

    page = await client.vaults.model_credentials.list(vault.id)
    credential = await client.vaults.model_credentials.retrieve(
        credential.id,
        vault_id=vault.id,
    )
```

The catalog never returns keys, environment-variable names, provider base URLs,
private model kwargs, or configured-key state. VMA resolves the provider to its
private credential slot and stores the key encrypted according to the server's
Vault configuration. Rotation keeps the same Credential ID, so already-bound
Sessions pick up the new key without changing payer.

When creating a Session, the trusted customer backend expresses preference only
through Vault order:

```python
session = await client.sessions.create(
    agent=agent.id,
    environment_id=environment.id,
    vault_ids=[end_user_vault_id, customer_shared_vault_id],
    idempotency_key=customer_operation_id,
)
```

If `idempotency_key` is omitted, the SDK generates a UUID for each Session
create call. Event submission follows the same rule.

Selection happens once at Session creation. The Session remains bound to that
specific Credential ID; rotation of the same Credential takes effect on a
later turn, while archive or deletion fails closed. VMA never silently changes
the payer inside an existing Session.

The native lifecycle also provides typed `archive()` and `delete()` methods.
Both purge the encrypted provider key before changing state. Native list and
retrieve responses expose provider identity and display metadata only, and the
public SDK has no generic credential escape hatch that could reveal an
internal `secret_name`.

For synchronous provisioning code, use the same nested surface without
`await`:

```python
from votrix import Votrix

with Votrix(api_key="vma_...", base_url="https://your-vma.example.com") as client:
    vault = client.vaults.create(display_name="End-user credentials")
    credential = client.vaults.model_credentials.create(
        vault.id,
        provider="openrouter",
        api_key=end_user_api_key,
    )
    credentials = client.vaults.model_credentials.list(vault.id)
    credential = client.vaults.model_credentials.retrieve(
        credential.id,
        vault_id=vault.id,
    )
```

`Votrix` currently covers API-key administration, Model Providers, Vault
lifecycle, and native Model Credentials. The rest of the native API remains
async-only through `AsyncVotrix`.

## Development and release

Run the SDK independently from the server project:

```bash
cd sdks/python
uv sync --extra dev
uv run pytest
uv run pyright
uv build
uv run --with twine twine check dist/*
```

`.github/workflows/python-sdk.yml` tests Python 3.10 through 3.13 and builds the
wheel and source distribution in the SDK directory. It also type-checks the
package, installs the built wheel into an isolated environment, verifies the
`py.typed` marker, and runs the native SDK against the in-process ASGI service.
A tag shaped like
`sdk-python-v0.1.0` triggers the separate publish workflow, which verifies that
the tag matches `project.version`, reruns all of those release gates, builds and
validates both artifacts, and uses PyPI Trusted Publishing.

Before the first release, a PyPI project owner must configure a Trusted
Publisher for this GitHub repository, workflow
`.github/workflows/python-sdk-publish.yml`, and environment `pypi`. The workflow
uses GitHub OIDC (`id-token: write`); no PyPI API token belongs in repository or
Actions secrets.
