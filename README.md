# Votrix Managed Agents (VMA)

Votrix Managed Agents (VMA) is an open-source, self-hosted, multi-tenant control plane for long-running agents. It targets the public resource, lifecycle, and SDK shape of [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) while running agents with [Deep Agents 0.6.12](https://github.com/langchain-ai/deepagents) and LangGraph.

> **Pre-launch breaking reset:** Organization is the only tenant entity. There
> are no legacy tenant compatibility aliases or implicit tenant fallback.
> Existing databases, R2 objects, API keys, and E2B Sessions created with the
> pre-launch tenant schema are unsupported; recreate each environment and
> bootstrap an explicit `org_*` Organization before running it.

VMA is not an Anthropic service and is not a drop-in behavioral replacement.
The current release is a **public-beta foundation**: its GA schema is deliberately
smaller than the repository's experimental route inventory, and it is suitable
for a controlled BYOK-first beta with optional operator-provisioned platform
funding rather than a high-availability or enterprise launch. Start with the
[compatibility matrix](docs/compatibility-matrix.md) and
[known incompatibilities](docs/known-incompatibilities.md) before deploying it
for real workloads.

## What is here

Implemented in the Votrix core:

- A public-beta `/v1` surface for API keys, agents and immutable versions,
  environments, sessions and events, files, Skills, Vaults, native model
  Credentials, the model-provider catalog, and raw usage. Additional repository
  routes remain explicitly deferred when `VMA_PUBLIC_GA_ONLY=true`.
- Postgres-backed Organization API keys with one-time secrets, independent
  create/list/retrieve/revoke/rotate lifecycle, expiration, and the `api`,
  `api_keys:manage`, and `worker` scopes.
- Request correlation through echoed/generated `request-id` and `x-request-id`
  headers, plus stable machine-readable `error.code` values.
- Contract tests that exercise covered routes through the official Anthropic Python SDK with strict response validation.
- A separately packaged native Python SDK, `votrix`, with `AsyncVotrix` for the
  full GA client and `Votrix` for synchronous API-key, provider, Vault, and
  native model-Credential administration. The SDK includes cursor pagination,
  true streamed downloads, reconnecting SSE, bounded retries, and typed errors.
- Append-only session events, monotonic sequence numbers, SSE replay, and
  durable work records with unique leases, generations, heartbeats, expired
  lease recovery, and stale-worker fencing.
- A deployment-selectable preview transport: local development defaults to the
  in-process bus, while the checked-in Cloud Run services use PostgreSQL
  `pg_notify` to preserve live token/tool deltas across API and worker instances.
- Tenant request-rate, active-work, daily model-token, and stored-byte quotas;
  append-only audit and raw-usage ledgers; and tenant idempotency used by
  Session creation. Event submission retains its dedicated transactional
  idempotency record.
- A Deep Agents runtime adapter with server-controlled Anthropic, OpenAI,
  DeepSeek, and custom model-provider routing configuration. Each Session fixes
  either an Organization Vault BYOK credential or an Organization platform key.
- LangGraph checkpoint selection for Postgres, SQLite, and explicit in-memory development mode.
- Private S3-compatible object storage for files and skill archives.
- An optional E2B sandbox provider for isolated session command and filesystem execution.
- An optional self-hosted work-queue worker and an importable deployment scheduler tick.

Important partial areas:

- Streaming previews are intentionally best-effort. PostgreSQL `NOTIFY` preserves
  hosted cross-instance typewriter delivery but does not make ephemeral frames
  replayable; clients always reconcile against durable Session events.
- The safe default has checkpointed file state but no shell execution. Isolated execution requires the optional E2B provider or an operator-supplied `VMA_SANDBOX_FACTORY`.
- MCP connections, restart-safe custom-tool/approval resume, skills, seeded memory files, and synchronous subagents are mapped to Deep Agents but do not yet reproduce every Claude semantic.
- Organization RBAC/SSO, Postgres RLS, exactly-once external side effects,
  enterprise audit export/retention, deployment scheduling, webhook delivery,
  and OAuth refresh remain deferred.
- Raw provider/model token usage is recorded per Organization and Session for
  quota enforcement and analysis. Operator-provisioned platform keys can power
  trials, but price books, authoritative monetary balances/reservations,
  top-ups, refunds, Stripe, invoices, and paid plans are not part of this release.

## Architecture

```text
AsyncVotrix native SDK / AsyncAnthropic compatibility / HTTP client
                              |
                              v
FastAPI API/control plane ----> Postgres resources, events, work, quotas, ledgers
         ^                                      |
         |                                      v
         +---- pg_notify previews ---- Deep Agents worker + LangGraph checkpoints
                                              |       |       |
                                              v       v       v
                                           model   remote   sandbox
                                          provider   MCP    backend

Private S3-compatible storage supplies Files and Skill archives to the control
plane and one-time Session sandbox bootstrap.
```

The control plane owns public IDs, Organization isolation, immutable revisions, session state, durable events, and compatibility translation. Deep Agents owns the in-process agent loop, model/tool middleware, compaction, checkpoint integration, and synchronous delegation. The sandbox—not the model or middleware—is the security boundary for tenant code.

See [Claude alignment](docs/claude-managed-agents-alignment.md), [sandbox runtime](docs/sandbox-runtime.md), and [Votrix core architecture](docs/votrix-core-architecture.md) for the full contracts.

## Quick start

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer with npm, for the local documentation site

Set up a local SQLite instance:

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run python -m scripts.bootstrap_api_key \
  --organization-id org_votrix \
  --organization-slug votrix \
  --organization-name "Votrix"
uv run uvicorn votrix_managed_agents:create_app --factory --reload
```

Alternatively, start the API and Fumadocs site together. The script installs
missing documentation dependencies, chooses an available docs port starting at
`4180`, and prints the home, guide, and API Playground links:

```bash
bash run.sh --migrate
```

Local, development, staging, and production environments all require a
database-backed Organization API key. They fail closed until the first
administrator key is created with `python -m scripts.bootstrap_api_key`; there
is no process-global or anonymous authentication mode. Production Vault and
platform-provider credentials also require `VMA_ENCRYPTION_KEY` unless an
injected secret provider replaces database-backed secrets.

After migrations, create the first key from a trusted administrator environment
if the quick-start command above has not already done so:

```bash
uv run python -m scripts.bootstrap_api_key \
  --organization-id org_votrix \
  --organization-slug votrix \
  --organization-name "Votrix"
```

The command writes one JSON object containing the plaintext secret exactly
once; send it directly to the intended secret manager. Local and development
clients may supply that Organization secret as `VMA_API_KEY` (or the namespaced
alias `VOTRIX_VMA_API_KEY`), while the VMA service itself reads authentication
keys only from the database. Subsequent key creation, rotation, and revocation
should use the authenticated `/v1/api_keys` lifecycle.

New production credentials use the `vma_live_` prefix. Staging, development,
local, and test credentials use `vma_test_`.

For the local SDK or pilot script, place the returned plaintext in the client
environment as `VMA_API_KEY` (not in the VMA service `.env`). In the API
Playground, enter the same value in the `x-api-key` authentication field. Raw
HTTP clients may use either supported header:

```bash
export VMA_API_KEY='<secret from bootstrap output>'
curl http://127.0.0.1:8080/v1/capabilities \
  --header "x-api-key: $VMA_API_KEY" \
  --header "votrix-managed-agents-beta: votrix-managed-agents-2026-04-01"

# Equivalent authentication header:
curl http://127.0.0.1:8080/v1/capabilities \
  --header "Authorization: Bearer $VMA_API_KEY" \
  --header "votrix-managed-agents-beta: votrix-managed-agents-2026-04-01"
```

Check the service:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/health/db
```

The machine-readable OpenAPI schema is available at
`http://127.0.0.1:8080/openapi.json`. The statically exported Fumadocs
application under `website/` combines the guides and interactive API reference.

## Configure a model provider

Agent definitions choose a provider and model. VMA operators control connection
URLs and routing policy. An Organization can supply model API keys as Vault
model Credentials, or a trusted service operator can provision an encrypted,
Organization-scoped platform key. VMA does not load model API keys from its
process environment or provider configuration.

Session creation also accepts Claude-compatible `agent_with_overrides` for a
one-Session replacement of model, system, tools, MCP servers, or Skills. Native
clients discover the server-approved registry through `model_providers` and
create BYOK credentials with a stable provider ID such as `openrouter`; callers
never need to know an internal Vault credential-slot name. At Session creation,
VMA reads the requested `vault_ids` in order and fixes the first matching model
Credential as that Session's payer. Later turns use the same Credential and fail
closed if it is revoked rather than switching to another Vault. If none of the
Session's `vault_ids` contains a matching model Credential, Session creation
returns `422 model_credential_required` under a BYOK-only policy.
The key is used only by the control-plane model client and is never copied into
E2B.

Native Session creation may also set `funding.type` to `byok`,
`platform_credits`, or `organization_default`. Omission is equivalent to
`organization_default` and remains compatible with existing CMA callers. With
no Organization billing account, the default remains BYOK. The Organization
policy is evaluated only while creating the Session; the resulting source and
exact key row are immutable, and a later revocation never falls back to another
source.

Platform keys are provisioned only from a trusted operator environment. The
provider key must already have the desired hard limit at the upstream provider;
`spending_limit_usd_micros` is retained as metadata and is not a VMA balance:

```bash
export VMA_FUNDING_PROVIDER_API_KEY='<hard-limited provider sub-key>'
uv run python -m scripts.provision_organization_funding \
  --organization-id org_votrix \
  --provider openrouter \
  --policy platform_only \
  --trial-expires-at 2026-08-01T00:00:00Z \
  --spending-limit-usd-micros 5000000
unset VMA_FUNDING_PROVIDER_API_KEY
```

The maintained local and Cloud Run profile uses OpenRouter's native LangChain
integration, pins `deepseek/deepseek-v4-pro` to Fireworks with Together as its
only fallback, requires tool parameters, and denies data collection:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openrouter
VMA_MODEL_PROVIDERS={"openrouter":{"adapter":"openrouter","api_key_env":"OPENROUTER_API_KEY","base_url":"https://openrouter.ai/api/v1","default_model":"deepseek/deepseek-v4-pro","model_kwargs":{"openrouter_provider":{"order":["fireworks","together"],"only":["fireworks","together"],"allow_fallbacks":true,"require_parameters":true,"data_collection":"deny"}},"capabilities":{"streaming":true,"tool_calls":true,"multimodal_input":false,"reasoning":true,"native_structured_output":false}}}
```

VMA constructs `langchain_openrouter.ChatOpenRouter`; it does not route this
profile through the generic OpenAI-compatible adapter. Despite its legacy
name, `api_key_env` is only the private Vault credential-slot identifier. VMA
does not read an environment variable with that name.

Anthropic remains available as an explicit provider:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=anthropic
VMA_DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-6
```

OpenAI:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openai
VMA_DEFAULT_OPENAI_MODEL=gpt-5.5
OPENAI_USE_RESPONSES=false
```

DeepSeek:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=deepseek
VMA_DEFAULT_DEEPSEEK_MODEL=deepseek-chat
```

Use `deepseek-chat` with the current runtime. VMA marks `deepseek-reasoner` as not supporting tool calls, and the Deep Agents harness requires a tool-calling model even when the public agent disables its tools, so the reasoner model is rejected before execution. Additional server-approved providers are configured through `VMA_MODEL_PROVIDERS`; this registry contains routing metadata and a private credential-slot name, never a model API key. Keyless local adapters such as `fake` and `ollama` bind with credential source `none`. See [model providers](docs/openai-compatible-providers.md).

## Run with Postgres and object storage

For a durable hosted Supavisor deployment, configure at least:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@pooler-host:6543/votrix_managed_agents
VMA_CHECKPOINT_DATABASE_URL=postgresql+asyncpg://user:password@pooler-host:6543/votrix_managed_agents
VMA_LISTEN_DATABASE_URL=postgresql+asyncpg://user:password@pooler-host:5432/votrix_managed_agents
S3_ENDPOINT_URL=https://...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...
```

Keep the bucket private. VMA reads and writes it with server credentials and
serves downloads through the authenticated Files API; neither
`S3_PUBLIC_URL` nor an R2 public/custom domain is required. Direct
presign/complete upload routes remain outside the public GA surface, so public
beta clients use the bounded authenticated upload route.

For hosted Supavisor deployments, main and LangGraph checkpoint traffic use the
transaction pooler on port `6543`; the dedicated preview listener and janitor
advisory lock use `VMA_LISTEN_DATABASE_URL` on session-mode port `5432`. The
migration Job receives a separate session/direct URL through the deployment
pipeline. Local or direct-Postgres installations may omit both VMA-specific
URLs and let them fall back to `DATABASE_URL`.

Run migrations once per release. Production must use a VMA-owned Postgres database or schema rather than sharing the `votrix-backend` schema. Google Cloud Run is the only maintained hosted deployment target; follow the [Cloud Run deployment guide](scripts/gcloud/README.md) and the [deployment topology notes](docs/deployment-platforms.md).

## Public-beta governance

Governance is enabled by default and can be tuned globally with:

```dotenv
VMA_GOVERNANCE_ENABLED=true
VMA_REQUESTS_PER_MINUTE=120
VMA_MAX_ACTIVE_WORK=5
VMA_DAILY_MODEL_TOKENS=1000000
VMA_ORGANIZATION_STORAGE_BYTES=5368709120
```

Organization overrides are stored in Postgres. Request limits return
`X-RateLimit-*`; resource quotas return `X-Quota-*`; denials are `429` errors
with stable codes. Provider-reported model usage is appended exactly once to the raw usage
ledger. Because provider usage is only known after a turn, a turn admitted
below the daily limit may cross it; all of that turn's tokens are recorded and
later turns are denied until the UTC-day window resets. This deliberate
one-turn overrun is a quota semantic, not billing or a monetary balance.

The audit and usage ledgers are append-only at both ORM and database-trigger
layers. They provide a beta operational record, not enterprise retention,
export, legal hold, tamper-evident external anchoring, or an authoritative
priced billing ledger.

## Deploy to Google Cloud Run

The checked-in hosted configuration targets GCP Cloud Run exclusively. It provides production and staging service manifests, a Cloud Build pipeline, Artifact Registry setup, Secret Manager integration, and release scripts under [`scripts/gcloud`](scripts/gcloud/README.md). Other hosted platforms are not maintained.

The checked-in Cloud Run topology separates HTTP/SSE API instances from a
private worker fleet. Production allows one to three API instances and one to
eight workers; staging allows one to two of each. API instances scale from
request load. Cloud Tasks sends private per-turn push requests so the worker
service scales independently from queued Agent work. A single-consumer
PostgreSQL reconciler remains active in every worker as the durable fallback
when task dispatch fails.

Each process uses one Uvicorn worker, bounded PostgreSQL pools, one-second
durable-event polling, and a 64 MiB aggregate Session-input cap. API instances
use a 4+2 application pool; workers use `containerConcurrency=5`, a five-turn
limiter, a 4+1 application pool, and a three-connection checkpoint pool. The
fallback reconciler polls every 20 seconds with concurrency one. Hosted
services set `VMA_PREVIEW_BROKER=pg_notify`: workers publish coalesced preview
frames and each API process holds one dedicated PostgreSQL `LISTEN` connection.
Main and checkpoint traffic uses the Supavisor transaction pooler on port
`6543`; only the listener and janitor advisory lock use the session-mode URL on
port `5432`. Delivery remains best-effort, and complete durable events are the
source of truth after reconnect or frame loss. Local development keeps the
`process_local` default.
Run the ten-Session performance smoke documented in the Cloud Run guide before
promoting staging. Hosted Organization defaults admit 20 queued/running turns;
production starts with one warm worker and five warm turn slots, then can scale
to eight worker instances while excess work remains durable in the queue.

Each release runs Alembic once through a dedicated Cloud Run migration Job
before replacing either service with the same immutable image. API and worker
services connect to durable Postgres and object storage; neither may rely on
Cloud Run's ephemeral filesystem for control-plane state. When E2B is enabled,
the sandboxes remain external E2B resources—Cloud Run hosts only the VMA control
plane and Deep Agents workers.

The optional worker remains part of the product protocol for `self_hosted` environments. That environment type is independent of VMA's own hosted deployment platform and is not removed by the GCP-only decision. Scheduled Deployment resources and the idempotent scheduler tick also remain available, but the repository does not yet operate a production scheduler that invokes the tick.

## Sandbox configuration

Unless a provider or factory is selected, VMA uses Deep Agents' `StateBackend`: files can be checkpointed, but the agent cannot execute shell commands. That remains the intentional safe default.

For isolated E2B execution, install the pinned optional extra and configure the server-owned API key:

```bash
uv sync --extra sandbox-e2b
```

```dotenv
VMA_SANDBOX_PROVIDER=e2b
E2B_API_KEY=...
VMA_E2B_TEMPLATE=vma-hardened
VMA_E2B_GUEST_USER=user
VMA_E2B_TEMPLATE_RESOURCES={"cpu":2,"memory_mb":2048}
```

The extra pins `langchain-e2b==0.0.5` and `e2b==2.31.0`. Creating a Session immediately provisions exactly one E2B sandbox, uploads its fixed Skills, read-only inputs, and initial memory seed once, seals revision 0, and pauses the sandbox. An active idle Session may later append one new read-only file at `/mnt/session/uploads/<filename>` through `sessions.resources.add`; existing inputs, Skills, and Memory seeds remain immutable. Every turn reconnects the same private `external_sandbox_id`, verifies the latest committed digest, manifest, and revision, and gives Deep Agents an `AsyncE2BSandbox`; the control plane neither re-uploads existing inputs nor re-downloads every Session file and Skill archive from object storage. A file explicitly referenced in the current model message is hydrated selectively; a path-only request lets the Agent read the persistent sandbox copy directly.

`VMA_E2B_TEMPLATE` is required and must name an operator-owned hardened template. At bootstrap and before every turn, VMA checks that the template's default execution user matches `VMA_E2B_GUEST_USER`, is not root, cannot complete `sudo -n true`, and cannot modify the trusted `/usr/bin` and `/usr/lib` roots used by VMA. Root bootstrap and seal verification use isolated `/usr/bin/python3 -I -S`, never the E2B guest-writable `/usr/local` tree. All remaining Linux filesystem and privilege hardening belongs to the trusted template.

`/workspace` and read-write memory remain mutable and persist with that sandbox, but edits are not written back to VMA Memory Store versions or exported. Eligible direct regular files written under `/mnt/session/outputs` are snapshotted into object storage as downloadable Session-scoped Files. Read-only uploads live below `/mnt/session/uploads` and cannot overlap mutable workdir or memory roots. The seal protects VMA-owned immutable files. `/tmp`, `/var/tmp`, and E2B provider-managed paths such as `/usr/local`, `/code`, and `/home/user` are untrusted Session-local storage rather than VMA trust roots; durable Agent work belongs under `/workspace` or read-write memory.

Provider auto-resume is disabled, secure access is enabled, public traffic is disabled, and turn exit uses full-memory pause. Archive preserves the sandbox, deletion kills it, and the in-process janitor provides best-effort cleanup after 30 days by default. Limited networking is passed through E2B's `allow_out` setting without a separate deny-all rule. Packages and CPU/memory/disk sizing must be built into and match the operator-owned template. VMA has no sandbox generations, provider snapshots, Daytona migration, operation leases/heartbeats, durable lifecycle outbox, orphan recovery, or managed-file sync. VMA does not return the external sandbox ID in its public API, although E2B may expose runtime identifiers inside the sandbox itself. See the detailed [sandbox runtime](docs/sandbox-runtime.md) and [E2B persistence](https://e2b.dev/docs/sandbox/persistence).

To use a different isolated container or VM provider, configure a factory using `module:attribute` syntax:

```dotenv
VMA_SANDBOX_FACTORY=my_service.sandboxes:create_backend
```

The factory receives `organization_id`, `session_id`, and `environment_config`, and must return a Deep Agents backend or async context manager. It is responsible for enforcing filesystem, process, network, package, resource, secret, and lifecycle policy. See [sandbox runtime](docs/sandbox-runtime.md).

`VMA_ALLOW_UNSAFE_LOCAL_SANDBOX=true` enables a host-local shell only for deliberate development. Never enable it for untrusted users or multi-tenant production.

## API compatibility

Compatibility requests use:

```text
anthropic-beta: managed-agents-2026-04-01
anthropic-version: 2023-06-01
```

Native clients may instead send:

```text
votrix-managed-agents-beta: votrix-managed-agents-2026-04-01
```

Authentication accepts `x-api-key` or a bearer token. API keys resolve to
Organizations without putting Organization IDs into public paths, and callers cannot
select a tenant with an untrusted Organization header. Database-backed keys are
hashed at rest, return plaintext only on create/rotate, and are independently
revocable in every environment.
Every response carries its request ID; errors expose a stable code suitable for
programmatic handling. The official SDK contract suite demonstrates how to
point `AsyncAnthropic` at the VMA base URL; route coverage is documented in
[Managed Agents API coverage](docs/managed-agents-api-coverage.md).

For new VMA integrations, use a native SDK. Python's `AsyncVotrix` preserves
the familiar resource-oriented Managed Agents shape, while `Votrix` provides
the synchronous administrative subset. The server-side TypeScript package
provides the same public resource families with Anthropic-style promises,
automatic pagination, SSE, uploads, and streaming downloads.

After the first PyPI release:

```bash
pip install votrix
```

Until then, install the project directly from `sdks/python`.

```python
from votrix import AsyncVotrix

client = AsyncVotrix(
    api_key="vma_live_...",
    base_url="https://api.vma.votrixai.com",
)

providers = [provider async for provider in client.model_providers.list()]
credential = await client.vaults.model_credentials.create(
    vault_id="vault_...",
    provider="openrouter",
    api_key="sk-...",
)
```

`AsyncAnthropic` remains the compatibility channel for overlapping Claude
Managed Agents calls. `AsyncVotrix` is the recommended interface for native
multi-provider and BYOK functionality. The SDK is an independent project under
[`sdks/python`](sdks/python/README.md); it does not replace the server's existing
`votrix_managed_agents` embedding package.

The TypeScript SDK is also pre-release and can currently be installed from
this repository:

```bash
cd sdks/typescript
npm ci
npm run build

# Then run this from the consuming Node.js project:
cd /path/to/your-node-project
npm install /absolute/path/to/votrix-managed-agents/sdks/typescript
```

```ts
import Votrix from "@votrix/managed-agents";

const client = new Votrix({
  apiKey: process.env.VMA_API_KEY,
  baseURL: "https://api.vma.votrixai.com",
});

const providers = await client.modelProviders.list();
```

## Tests

```bash
uv run pytest
uv run pytest -m contract
./scripts/test-backend-contract-matrix.sh
cd sdks/python && uv run pytest && uv run pyright && uv build
cd sdks/typescript && npm run check && npm run attw
```

The contract extra installs the official client:

```bash
uv sync --extra contract
```

Strict SDK parsing proves the covered response shapes are accepted by the pinned client version. It does not prove behavioral equivalence with Anthropic's managed infrastructure.

## Documentation

- [Compatibility matrix](docs/compatibility-matrix.md)
- [Known incompatibilities](docs/known-incompatibilities.md)
- [Claude Managed Agents alignment](docs/claude-managed-agents-alignment.md)
- [Managed Agents API coverage](docs/managed-agents-api-coverage.md)
- [Python SDK](docs/python-sdk.md)
- [TypeScript SDK](docs/typescript-sdk.md)
- [Agent versioning](docs/agent-versioning.md)
- [Sandbox runtime](docs/sandbox-runtime.md)
- [Model providers](docs/openai-compatible-providers.md)
- [Work queue](docs/work-queue.md)
- [Google Cloud Run deployment](docs/deployment-platforms.md)
- [Memory stores](docs/memory-stores.md)
- [Deployments](docs/deployments.md)
- [Webhooks](docs/webhooks.md)
- [Votrix core architecture](docs/votrix-core-architecture.md)
- [Public-beta readiness handoff](docs/handoffs/2026-07-15-public-beta-readiness.md)
- [Changelog](CHANGELOG.md)

## Votrix core embedding

Hosted or enterprise code can compose the application in-process:

```python
from votrix_managed_agents import create_app

app = create_app(auth_provider=HostedAuthProvider())
```

The Votrix core intentionally stops at Organization-scoped resources. It includes
the baseline tenant quotas and append-only raw audit/usage ledgers needed for a
public beta. Organization membership, SSO, RBAC, Postgres RLS, paid billing and
pricing, enterprise audit export/retention, managed sandbox fleets, and hosted
secret policy still belong in an injected private layer. See
[Votrix core architecture](docs/votrix-core-architecture.md).

## Security and maturity

This is an early `0.1.0` project. Deep Agents tools, skills, memory, MCP output, and subagent output all enter an LLM-controlled execution loop and may contain prompt injection. Tool permissions are not a substitute for sandbox isolation. Review [known incompatibilities](docs/known-incompatibilities.md) before exposing VMA to untrusted tenants.

## License

Proprietary. Copyright Votrix. All rights reserved. This repository is not open source.
