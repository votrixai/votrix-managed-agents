# Votrix Managed Agents

Votrix Managed Agents (VMA) is an open-source, self-hosted, multi-tenant control plane for long-running agents. It targets the public resource, lifecycle, and SDK shape of [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) while running agents with [Deep Agents 0.6.12](https://github.com/langchain-ai/deepagents) and LangGraph.

VMA is not an Anthropic service and is not yet a drop-in behavioral replacement. The REST surface is substantially broader than the production execution surface. Start with the [compatibility matrix](docs/compatibility-matrix.md) and [known incompatibilities](docs/known-incompatibilities.md) before deploying it for real workloads.

## What is here

Implemented in the open core:

- FastAPI routes under `/v1` for agents, immutable agent versions, environments, sessions, events, files, skills, vaults, credentials, memory stores, deployments, deployment runs, and user profiles.
- Workspace-scoped API-key authentication and database queries.
- Contract tests that exercise covered routes through the official Anthropic Python SDK with strict response validation.
- Append-only session events, monotonic sequence numbers, SSE replay, and durable work records.
- A Deep Agents runtime adapter with server-controlled Anthropic, OpenAI, DeepSeek, and custom model-provider configuration.
- LangGraph checkpoint selection for Postgres, SQLite, and explicit in-memory development mode.
- S3-compatible object storage for files and skill archives.
- An optional E2B sandbox provider for isolated session command and filesystem execution.
- An optional self-hosted work-queue worker and an importable deployment scheduler tick.

Important partial areas:

- Streaming previews are process-local; separate web and worker processes need Redis Streams, NATS, or another tenant-scoped broker.
- The safe default has checkpointed file state but no shell execution. Isolated execution requires the optional E2B provider or an operator-supplied `VMA_SANDBOX_FACTORY`.
- MCP connections, restart-safe custom-tool/approval resume, skills, seeded memory files, and synchronous subagents are mapped to Deep Agents but do not yet reproduce every Claude semantic.
- Deployment scheduling, webhook delivery, OAuth token refresh, distributed run locking, and enterprise controls need additional production services.

## Architecture

```text
Anthropic SDK or HTTP client
            |
            v
FastAPI compatibility and control plane
  |         |             |
  |         |             +-- S3-compatible files and skill archives
  |         +---------------- Postgres/SQLite resources, events, and work
  +-------------------------- durable session/revision lookup
            |
            v
Deep Agents 0.6.12 + LangGraph checkpoints
  |                 |                 |
  v                 v                 v
model provider   remote MCP       sandbox backend
```

The control plane owns public IDs, workspace isolation, immutable revisions, session state, durable events, and compatibility translation. Deep Agents owns the in-process agent loop, model/tool middleware, compaction, checkpoint integration, and synchronous delegation. The sandbox—not the model or middleware—is the security boundary for tenant code.

See [Claude alignment](docs/claude-managed-agents-alignment.md), [sandbox runtime](docs/sandbox-runtime.md), and [open-core architecture](docs/open-core-architecture.md) for the full contracts.

## Quick start

Requirements:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)

Set up a local SQLite instance:

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn votrix_managed_agents:create_app --factory --reload
```

The local configuration permits anonymous requests when no API key is configured. Production should set `APP_ENV=production`, configure `VMA_API_KEY` or an injected auth provider, and use Postgres. Production vault credentials also require `VMA_ENCRYPTION_KEY` unless an injected secret provider replaces database-backed secrets.

Check the service:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/db
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Configure a model provider

Agent definitions choose a provider and model, but connection URLs and credentials remain server-controlled.

Anthropic is the default:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
VMA_DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-6
```

OpenAI:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=openai
OPENAI_API_KEY=...
VMA_DEFAULT_OPENAI_MODEL=gpt-5.5
OPENAI_USE_RESPONSES=false
```

DeepSeek:

```dotenv
VMA_DEFAULT_MODEL_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
VMA_DEFAULT_DEEPSEEK_MODEL=deepseek-chat
```

Use `deepseek-chat` with the current runtime. VMA marks `deepseek-reasoner` as not supporting tool calls, and the Deep Agents harness requires a tool-calling model even when the public agent disables its tools, so the reasoner model is rejected before execution. Additional server-approved providers are configured through `VMA_MODEL_PROVIDERS`; see [model providers](docs/openai-compatible-providers.md).

## Run with Postgres and object storage

For a durable deployment, configure at least:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/votrix_managed_agents
VMA_CHECKPOINT_DATABASE_URL=postgresql://user:password@host:5432/votrix_managed_agents
S3_ENDPOINT_URL=https://...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...
```

Run migrations once per release. The reference [Docker Compose deployment](deploy/docker-compose/README.md) includes Postgres and MinIO; other targets are listed in [deployment platforms](docs/deployment-platforms.md).

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
```

The extra pins `langchain-e2b==0.0.5` and `e2b==2.31.0`. Creating a Session immediately provisions exactly one E2B sandbox, uploads its fixed Skills, read-only inputs, and initial memory seed once, seals the immutable files, and pauses the sandbox. Every turn reconnects the same private `external_sandbox_id`, verifies the seal and input digest, and gives Deep Agents an `AsyncE2BSandbox`; the control plane never re-uploads or synchronizes Session files on resume. Changing a Skill, initial input, initial memory source, configured template, or Session resource after the seal is rejected and requires a new Session.

`VMA_E2B_TEMPLATE` is required and must name an operator-owned hardened template. At bootstrap and before every turn, VMA checks that the template's default execution user matches `VMA_E2B_GUEST_USER`, is not root, and cannot complete `sudo -n true`; all remaining Linux filesystem and privilege hardening belongs to that trusted template.

`/workspace` and read-write memory remain mutable and persist with that sandbox, but edits are not written back to VMA Memory Store versions and generated artifacts are not automatically exported. Read-only uploads default below `/mnt/session/uploads` and cannot overlap mutable workspace or memory roots. The seal protects VMA-owned immutable files; the hardened template is responsible for confining other guest writes.

Provider auto-resume is disabled, secure access is enabled, public traffic is disabled, and turn exit uses full-memory pause. Archive preserves the sandbox, deletion kills it, and the in-process janitor provides best-effort cleanup after 30 days by default. Limited networking is passed through E2B's `allow_out` setting without a separate deny-all rule. Packages and CPU/memory/disk sizing must be built into and match the operator-owned template. VMA has no sandbox generations, provider snapshots, Daytona migration, operation leases/heartbeats, durable lifecycle outbox, orphan recovery, or managed-file sync. VMA does not return the external sandbox ID in its public API, although E2B may expose runtime identifiers inside the sandbox itself. See the detailed [sandbox runtime](docs/sandbox-runtime.md) and [E2B persistence](https://e2b.dev/docs/sandbox/persistence).

To use a different isolated container or VM provider, configure a factory using `module:attribute` syntax:

```dotenv
VMA_SANDBOX_FACTORY=my_service.sandboxes:create_backend
```

The factory receives `workspace_id`, `session_id`, and `environment_config`, and must return a Deep Agents backend or async context manager. It is responsible for enforcing filesystem, process, network, package, resource, secret, and lifecycle policy. See [sandbox runtime](docs/sandbox-runtime.md).

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

Authentication accepts `x-api-key` or a bearer token. API keys resolve to workspaces without putting workspace IDs into public paths. The official SDK contract suite demonstrates how to point `AsyncAnthropic` at the VMA base URL; route coverage is documented in [Managed Agents API coverage](docs/managed-agents-api-coverage.md).

## Tests

```bash
uv run pytest
uv run pytest -m contract
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
- [Agent versioning](docs/agent-versioning.md)
- [Sandbox runtime](docs/sandbox-runtime.md)
- [Model providers](docs/openai-compatible-providers.md)
- [Work queue](docs/work-queue.md)
- [Memory stores](docs/memory-stores.md)
- [Deployments](docs/deployments.md)
- [Webhooks](docs/webhooks.md)
- [Open-core architecture](docs/open-core-architecture.md)

## Open-core embedding

Hosted or enterprise code can compose the application in-process:

```python
from votrix_managed_agents import create_app

app = create_app(auth_provider=HostedAuthProvider())
```

The open core intentionally stops at workspace-scoped resources. Organization membership, SSO, RBAC, billing, quotas, metering, audit retention, managed sandbox fleets, and hosted secret policy belong in an injected private layer. See [open-core architecture](docs/open-core-architecture.md).

## Security and maturity

This is an early `0.1.0` project. Deep Agents tools, skills, memory, MCP output, and subagent output all enter an LLM-controlled execution loop and may contain prompt injection. Tool permissions are not a substitute for sandbox isolation. Review [known incompatibilities](docs/known-incompatibilities.md) before exposing VMA to untrusted tenants.

## License

[MIT](LICENSE)
