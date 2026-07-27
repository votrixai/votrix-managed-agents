# Votrix Managed Agents (VMA) Python SDK

`votrix` is the Python client for the native Votrix Managed Agents API.
`AsyncVotrix` covers the broader native resource surface; `Votrix` provides a
synchronous GA wrapper for API-key administration, model-provider discovery,
Vaults, and provider BYOK credentials. Neither client exposes internal
environment-variable names.

The package requires Python 3.10 or newer.

## Installation

The SDK has not been published to PyPI yet. Install it from this repository
during development:

```bash
python -m pip install -e ./sdks/python
```

After the first release, the published installation command will be:

```bash
python -m pip install votrix-managed-agents
```

From inside `sdks/python`, install development dependencies with
`python -m pip install -e ".[dev]"`.

This distribution installs only the `votrix` client package. It does not
install the Votrix Managed Agents server, its `app` package, or the server's
legacy compatibility namespace, so it can be installed alongside the service
source tree without a package-name collision.

## Client setup

Pass the API key and service URL explicitly:

```python
from votrix import AsyncVotrix

client = AsyncVotrix(
    api_key="vma_live_...",
    base_url="https://vma.example.com",
)
```

Alternatively, set both environment variables and construct the client with no
credentials in application code:

```bash
export VMA_API_KEY="vma_live_..."
export VMA_BASE_URL="https://vma.example.com"
```

```python
from votrix import AsyncVotrix

client = AsyncVotrix()
```

`base_url` or `VMA_BASE_URL` is always required. `VOTRIX_VMA_API_KEY` and
`VOTRIX_VMA_BASE_URL` are supported as namespaced aliases. The default
authentication scheme sends `x-api-key`. For a deployment that accepts bearer
authentication, select it explicitly:

Production keys start with `vma_live_`; staging, development, local, and test
keys start with `vma_test_`.

```python
client = AsyncVotrix(
    api_key="vma_live_...",
    base_url="https://vma.example.com",
    auth_scheme="bearer",
)
```

Use the client as an async context manager so its connection pool is closed
deterministically:

```python
import asyncio

from votrix import AsyncVotrix


async def main() -> None:
    async with AsyncVotrix() as client:
        providers = await client.model_providers.list()
        for provider in providers.data:
            print(provider.id, provider.display_name)


asyncio.run(main())
```

If a context manager does not fit the application's lifetime, call
`await client.close()` during shutdown.

## Managed Agents resources

The public-beta client exposes API keys, agents, environments, sessions, files,
skills, Memory Stores, Vaults, model Credentials, model providers, and raw usage
as typed async resources:

```python
async with AsyncVotrix() as client:
    agent = await client.agents.create(
        name="support-agent",
        model={"id": "deepseek/deepseek-v4-pro", "provider": "openrouter"},
        system="Help the end user clearly and concisely.",
    )

    environment = await client.environments.create(
        name="production-runtime",
        config={"type": "cloud"},
    )

    session = await client.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        vault_ids=["vault_end_user", "vault_organization"],
    )

    usage = await client.usage.list(session_id=session.id, metric="model_tokens")
```

Native callers may pass `SessionFundingRequest(type="platform_credits")` to
`sessions.create`; the other values are `byok` and `organization_default`.
Omission preserves the CMA-compatible request shape and uses the Organization
default. Funding selection is fixed for the lifetime of the Session.

Session creation and event submission automatically send a fresh
`Idempotency-Key`, which makes the SDK's retry policy safe. Pass
`idempotency_key=` when the key must survive a caller-level retry or process
restart.

Request and response objects accept the Votrix extensions for their resource
while retaining the shared Claude Managed Agents field names where the APIs
overlap.

Memory Stores are available through `memory_stores`, with nested `memories` and
`memory_versions` resources. Generic Vault Credentials remain deliberately
absent; provider BYOK uses the typed `vaults.model_credentials` surface below.

## Memory Stores

Create a store, manage path-addressed memories, and inspect or redact immutable
versions through the native async client:

```python
store = await client.memory_stores.create(name="Account context")
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

first_version = await client.memory_stores.memories.versions.retrieve(
    1,
    memory_store_id=store.id,
    memory_id=memory.id,
)
```

Use `memories.retrieve_by_path(path, memory_store_id=...)` (or its `by_path`
alias) for direct path lookup. A depth-limited memory list may contain
`MemoryListItem(type="memory_prefix", ...)` directory rollups. Redaction is
available at `memory_stores.memory_versions.redact(...)`; the API rejects
redaction of a live memory's current head version.

## API-key lifecycle

Both clients expose scoped Organization-key administration:

```python
created = await client.api_keys.create(
    name="production-worker",
    scopes=["api", "worker"],
)
save_once(created.secret.get_secret_value())

rotated = await client.api_keys.rotate(created.id, reason="scheduled rollover")
save_once(rotated.secret.get_secret_value())

await client.api_keys.revoke(rotated.id, reason="retired")
```

The plaintext key is represented as Pydantic `SecretStr` and is present only
in `create()` and `rotate()` results. List, retrieve, and revoke use the safe
`ApiKey` metadata model, which ignores any unexpected secret field returned by
a misconfigured intermediary. API-key lifecycle methods are also available on
the synchronous `Votrix` client without `await`.

## Model-provider credentials

Discover provider IDs from the service instead of hard-coding provider secret
names:

```python
providers = await client.model_providers.list()
openrouter = next(item for item in providers.data if item.id == "openrouter")

vault = await client.vaults.create(display_name="End-user credentials")
credential = await client.vaults.model_credentials.create(
    vault.id,
    provider=openrouter.id,
    api_key=end_user_api_key,
    display_name="Personal OpenRouter key",
)

# Rotation keeps the same credential binding and still hides internal names.
credential = await client.vaults.model_credentials.rotate(
    vault.id,
    credential.id,
    api_key=rotated_end_user_api_key,
)

credentials = await client.vaults.model_credentials.list(vault.id)
credential = await client.vaults.model_credentials.retrieve(
    credential.id,
    vault_id=vault.id,
)

# Archive permanently purges the stored provider key.
credential = await client.vaults.model_credentials.archive(
    credential.id,
    vault_id=vault.id,
)
```

The caller supplies only a stable provider ID and the write-only API key. The
service maps that provider to its internal credential representation; callers
do not send or need to know a `secret_name` such as `OPENROUTER_API_KEY`.

`create()` and `rotate()` accept a plaintext `api_key` as a write-only request
field. List, retrieve, rotate, and archive responses never contain that value,
`auth`, or `secret_name`. Archive and delete purge the encrypted secret before
changing lifecycle state, so an archived Credential cannot be rotated or used
again. Use `delete(credential.id, vault_id=vault.id)` when no tombstoned
Credential metadata is needed.

The same BYOK lifecycle is available synchronously:

```python
from votrix import Votrix

with Votrix(
    api_key="vma_live_...",
    base_url="https://vma.example.com",
) as client:
    provider = client.model_providers.retrieve("openrouter")
    vault = client.vaults.create(display_name="End-user credentials")
    credential = client.vaults.model_credentials.create(
        vault.id,
        provider=provider.id,
        api_key=end_user_api_key,
    )
    credentials = client.vaults.model_credentials.list(vault.id)
    credential = client.vaults.model_credentials.retrieve(
        credential.id,
        vault_id=vault.id,
    )
```

The synchronous client intentionally exposes provisioning surfaces only: API
keys, model providers, Vaults, and model Credentials. Use `AsyncVotrix` for
Agents, Sessions, Files, and Skills.

## Pagination

List methods return an awaitable paginator. Await it to inspect one page and
advance manually:

```python
page = await client.agents.list(limit=25)
for agent in page.data:
    print(agent.id)

next_page = await page.get_next_page()
```

Or iterate over the paginator to traverse all pages lazily:

```python
async for agent in client.agents.list(limit=100):
    print(agent.id)
```

## Session event streams

Session streams use server-sent events. Open them with the returned async
context manager so the HTTP response is always closed:

```python
async with await client.sessions.events.stream(session.id) as stream:
    async for event in stream:
        print(event.type, event.seq)
```

The stream object deliberately requires the `async with await ...stream(...)`
form before iteration. If the connection ends unexpectedly, it reconnects up
to the client's `max_retries`, sends the last received SSE ID as
`Last-Event-ID`, and suppresses replayed event IDs. Set `max_reconnects` on
`stream()` to override that limit for one stream. Cancelling the consuming task
or leaving the context closes the active HTTP response.

## File downloads

Downloads return a binary response wrapper. Use `read()` to obtain the bytes:

```python
download = await client.files.download(file_id)
contents = await download.read()

print(download.filename, download.content_type, len(contents))

for chunk in download.iter_bytes(64 * 1024):
    process(chunk)

await download.write_to_file("./result.bin")
```

`read()` buffers the response for compatibility. For large files, consume a
fresh download incrementally instead:

```python
download = await client.files.download(file_id, stream=True)
async for chunk in download.aiter_bytes(64 * 1024):
    process(chunk)
```

`stream=False` is the backwards-compatible default, so existing synchronous
`iter_bytes()` calls still operate on buffered content. With `stream=True`,
`write_to_file()` also writes incrementally when the response has not already
been buffered. The SDK never interprets or writes downloaded bytes unless
`write_to_file()` is called explicitly. Consuming a streaming response closes
it; if one is abandoned before consumption, call `await download.aclose()` or
use it as an async context manager.

## Development

```bash
pytest
pyright
python -m build
```

The package ships inline annotations and a `py.typed` marker. Stable resource
methods and response fields are typed; intentionally forward-compatible API
metadata and extension fields may remain `Any`.
