---
title: Python SDK
description: Use AsyncVotrix or the synchronous provisioning client while retaining the AsyncAnthropic compatibility path.
---

VMA has two deliberately separate Python client contracts:

| Client | Purpose |
| --- | --- |
| `from votrix import AsyncVotrix` | Recommended async client for VMA resources, including Memory Stores, model-provider discovery, Session funding, and raw usage. |
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

Supply the Organization API key and the URL of your VMA deployment explicitly,
or use `VMA_API_KEY` and `VMA_BASE_URL`. The namespaced aliases
`VOTRIX_VMA_API_KEY` and `VOTRIX_VMA_BASE_URL` are also supported:

```python
from votrix import AsyncVotrix

client = AsyncVotrix(
    api_key="vma_live_...",
    base_url="https://your-vma.example.com",
)
```

The client sends `x-api-key` and the native
`votrix-managed-agents-beta` header by default. Bearer authentication is an
explicit constructor option. Use `async with` or call `await client.close()` to
close its connection pool.

Production keys start with `vma_live_`; non-production keys start with
`vma_test_`. The SDK forwards credentials without inferring the target from the
prefix.

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

## Organization API keys

Tenant administrators can create, inspect, rotate, and revoke Organization-scoped
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
exposes Memory Stores as a public typed async resource; generic Vault
Credentials remain deferred.

## Memory Stores

The async client covers store create/retrieve/update/list/archive/delete,
memory create/retrieve/update/list/delete and path lookup, plus immutable
version list/retrieve/redact:

```python
store = await client.memory_stores.create(
    name="Account context",
    description="Support memories",
)
memory = await client.memory_stores.memories.create(
    store.id,
    path="/accounts/acme.md",
    content="ACME prefers email.",
    view="full",
)

memory = await client.memory_stores.memories.update(
    memory.id,
    memory_store_id=store.id,
    content="ACME prefers chat.",
    precondition={
        "type": "content_sha256",
        "content_sha256": memory.content_sha256,
    },
    view="full",
)

versions = await client.memory_stores.memory_versions.list(
    store.id,
    memory_id=memory.id,
    view="full",
)

history = await client.memory_stores.memories.versions.list(
    memory.id,
    memory_store_id=store.id,
)
```

`memories.retrieve_by_path(path, memory_store_id=...)` and its `by_path` alias
perform direct path lookup. `memories.versions` addresses one memory's history
by numeric version, while `memory_stores.memory_versions` lists store-wide
history and addresses versions by ID. With `depth=`, memory lists can also
return typed `memory_prefix` rollups. Version redaction is permanent and cannot
target a live memory's current head version. Memory Stores are async-only; the
synchronous provisioning client does not expose them.

## Provider discovery and Organization BYOK

Applications should never expose an internal name such as
`OPENROUTER_API_KEY`. Discover the public provider IDs from VMA, create an
Organization Vault, and submit the provider ID with the write-only key:

```python
async with AsyncVotrix(
    api_key="vma_live_...",
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

The catalog never returns keys, internal credential-slot names, provider base
URLs, private model kwargs, or Session-specific credential availability. VMA
resolves the provider to its private credential slot and stores the key
encrypted according to the server's Vault configuration. Rotation keeps the
same Credential ID, so already-bound Sessions pick up the new key without
changing payer.

Model Credentials use the explicit `vaults.model_credentials` surface shown
above. Generic Vault Credentials remain for MCP servers and other integrations;
callers do not classify a raw generic secret by inventing a string type.

When creating a BYOK Session, the trusted Organization backend expresses Vault
preference through order:

```python
session = await client.sessions.create(
    agent=agent.id,
    environment_id=environment.id,
    vault_ids=[end_user_vault_id, organization_shared_vault_id],
    idempotency_key=organization_operation_id,
)
```

If `idempotency_key` is omitted, the SDK generates a UUID for each Session
create call. Event submission follows the same rule.

Selection happens once at Session creation. The Session remains bound to that
specific Credential ID; rotation of the same Credential takes effect on a
later turn, while archive or deletion fails closed. VMA never silently changes
the payer inside an existing Session. If none of the submitted `vault_ids`
contains a Credential for the Session model provider, Session creation returns
HTTP `422` with code `model_credential_required` under a BYOK-only policy.

The native SDK also supports an explicit Organization funding source:

```python
from votrix import SessionFundingRequest

session = await client.sessions.create(
    agent=agent.id,
    environment_id=environment.id,
    funding=SessionFundingRequest(type="platform_credits"),
)
```

Valid values are `byok`, `platform_credits`, and `organization_default`.
Omitting `funding` preserves the CMA-compatible request and behaves as
`organization_default`; an Organization with no billing account remains
BYOK-only. The default policy is evaluated once at Session creation. The
selected Vault Credential or exact platform-provider key row stays fixed for
the Session, so revocation fails closed instead of changing funding sources.
Platform funding here means an operator-provisioned provider key; it is not a
prepaid monetary balance or an invoice claim.

Organization backends can read the raw usage facts they need for their own
Session mapping and downstream accounting:

```python
page = await client.usage.list(
    session_id=session.id,
    metric="model_tokens",
    limit=100,
)
for fact in page.data:
    print(fact.quantity, fact.unit, fact.provider, fact.model)
```

The API also supports opaque pagination and time filters. It returns recorded
provider facts only; it does not infer an end user or fabricate monetary cost.

The current multiagent MVP has one funding binding per Session, so the
coordinator and every pinned subagent must use the same provider. Create
separate Sessions when different providers are required.

The native lifecycle also provides typed `archive()` and `delete()` methods.
Both purge the encrypted provider key before changing state. Native list and
retrieve responses expose provider identity and display metadata only, and the
public SDK has no generic credential escape hatch that could reveal an
internal `secret_name`.

For synchronous provisioning code, use the same nested surface without
`await`:

```python
from votrix import Votrix

with Votrix(api_key="vma_live_...", base_url="https://your-vma.example.com") as client:
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
