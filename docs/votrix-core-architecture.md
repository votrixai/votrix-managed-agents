---
title: Votrix Core Architecture
description: Ownership boundaries across the public API, durable control plane, and agent runtime.
---

VMA is the self-hosted, workspace-scoped Votrix core of a managed-agents platform. It exposes a Claude Managed Agents-shaped control plane and uses Deep Agents 0.6.12 as the execution kernel. A private hosted product should compose this package with enterprise identity, policy, infrastructure, and commercial services rather than fork the core.

The boundary is architectural, not a claim that the current core already provides Claude-equivalent managed infrastructure. See the [compatibility matrix](./compatibility-matrix.md) and [known incompatibilities](./known-incompatibilities.md).

## Layer model

```text
Hosted/private product
  organizations, members, RBAC, SSO, RLS, paid billing, audit operations, support
  hosted model gateway, secret manager, sandbox fleet, broker, scheduler
                              |
                              v
Votrix Managed Agents core
  workspace auth -> FastAPI compatibility routes -> durable resources/events
                              |
                              v
Deep Agents execution adapter
  LangChain model + MCP tools + LangGraph checkpoint + sandbox backend
```

Deep Agents is below the public service boundary. Public callers should never need to know about LangChain messages, LangGraph checkpoint IDs, middleware names, provider profile registries, or backend-native sandbox IDs.

## Core responsibilities

The Votrix core owns:

- FastAPI paths and models for the covered `/v1` Managed Agents-shaped resources.
- Beta/version-header validation and official-SDK contract tests.
- Workspace-scoped authentication interfaces and API-key implementations.
- Database API-key create/list/retrieve/revoke/rotate lifecycle, expiration,
  `api`/`api_keys:manage`/`worker` scopes, and trusted first-key bootstrap.
- Request IDs, stable error codes, and authenticated request audit correlation.
- Agent/version immutability and session version pinning.
- Environment and session resources, append-only events, session state, and work records.
- Deep Agents graph compilation and translation of runtime events into public events.
- Server-controlled model-provider resolution for Anthropic, OpenAI, DeepSeek, and approved extensions.
- LangGraph checkpointer selection.
- A sandbox factory interface plus a safe no-shell default.
- S3-compatible file and custom-skill bytes.
- Workspace-scoped memory and credential resources.
- Optional self-hosted worker mechanics and an importable deployment scheduler tick.
- Durable work leases/generations, heartbeat, expired-attempt recovery, and
  stale-worker terminal-write fencing.
- Atomic workspace request, active-work, daily model-token, and stored-byte
  quotas with append-only raw audit/usage ledgers.
- Generic tenant idempotency for Session creation plus transactional event
  submission idempotency.
- A bootstrapped workspace experience for local development.

The core may expose extension interfaces, but it must stay useful without a private repository.

## Hosted/private responsibilities

A hosted or enterprise layer owns:

- Organizations, users, memberships, invitations, teams, and human/service-account identity.
- RBAC/ABAC, SSO/SAML/OIDC, SCIM, trust grants, and support impersonation policy.
- Advanced policy beyond the core's narrow workspace quotas, including sandbox
  compute/egress, tool/MCP, retention, and monetary spend controls.
- Commercial billing after the BYOK/free beta: price books, currency amounts,
  balances/credits, top-ups, refunds, Stripe, invoices, plans, seats, and taxes.
- Enterprise audit export, automated retention, legal hold, external tamper
  anchoring, and administrator/support access logging.
- Hosted model gateways and tenant credential policy.
- KMS-backed secret management, credential rotation, OAuth enrollment/refresh, and revocation.
- Remote sandbox fleet selection, isolation, images, lifecycle, snapshots, and regional placement.
- A cross-process preview broker and distributed run locks.
- Production queues, dead-letter handling, scheduler operation, webhook delivery, and retry SLOs.
- Compliance controls, data residency, deletion verification, incident response, and customer support tooling.

These concerns should not become required foreign keys or imports in the Votrix core data model.

## Tenant model

The only tenant boundary inside the core is `workspace_id`.

```text
Hosted/private identity:
  user or service account -> organization -> workspace -> role/policy

VMA core request:
  AuthProvider -> CurrentWorkspace(id=...) -> scoped resources and execution
```

Local self-hosting bootstraps an explicit workspace and database-backed key:

```text
bootstrap CLI -> workspace record + one-time administrator key
request API key -> authenticated workspace_id
```

Public resource paths remain workspace-free:

```text
/v1/agents
/v1/sessions
/v1/files
```

The workspace comes from the authenticated request. Do not add public paths such as `/v1/workspaces/{workspace_id}/agents` to the compatibility API.

Every core persistent resource and query must be scoped by workspace unless the function is explicitly internal and named accordingly. Object-storage keys must begin with a workspace partition. Public session IDs must resolve through a workspace-filtered database record before VMA uses the opaque internal LangGraph thread ID.

## Application composition

The supported in-process extension point is:

```python
from votrix_managed_agents import create_app

app = create_app(auth_provider=HostedAuthProvider())
```

An auth provider implements the public `AuthProvider` protocol and returns `CurrentWorkspace`. Core routers and query helpers then operate inside that scope.

The repository includes:

- `DatabaseApiKeyAuthProvider` for keys stored in the core database.
- The injectable `AuthProvider` path for hosted identity.

Those providers authenticate a workspace. Core request/quota activity can be
written to the append-only audit ledger, but the providers do not add
organization membership, roles, SSO, paid billing, or enterprise audit policy.

Prefer in-process provider injection over copying routers or placing an API-shape translation proxy in front of core. Run VMA as a separate internal service only when the deployment intentionally wants a network boundary and accepts the additional identity, tracing, and consistency work.

## Execution-provider boundaries

### Models

Agent resources select a provider/model, while credentials and endpoints remain server-owned. Built-in settings and `VMA_MODEL_PROVIDERS` construct LangChain models explicitly. Tenant-specific values must not be registered in Deep Agents' process-global profile registries.

A hosted product may replace direct provider keys with a tenant-aware model gateway, but must preserve the provider capability checks and usage attribution. See [model providers](./openai-compatible-providers.md).

### Sandboxes

`VMA_SANDBOX_FACTORY=module:attribute` injects a backend for a workspace/session/environment. A hosted implementation should return a remote `SandboxBackendProtocol` backed by containers or VMs. It is responsible for all actual security policy and lifecycle behavior.

The default `StateBackend` is safe because it has no shell, not because it is a production sandbox. The opt-in `LocalShellBackend` executes on the host and is excluded from untrusted production. See [sandbox runtime](./sandbox-runtime.md).

### Checkpoints and stores

LangGraph checkpoints preserve graph state and interrupts. VMA chooses Postgres for production-style DSNs and a separate SQLite checkpoint database locally. Checkpoints are internal runtime state; SQLAlchemy resource/event tables remain the public control-plane source of truth.

Cross-thread memory, files, and artifacts should use explicitly tenant-namespaced stores or object storage. No backend namespace may be derived from a caller-controlled public ID without a workspace lookup.

### MCP and secrets

Agent versions contain MCP names and URLs, not secrets. Sessions mount vault IDs. Runtime code matches credentials to server URLs and passes authorization only to the MCP client.

The Votrix core currently persists optionally encrypted credential material and lacks full OAuth refresh. A hosted product should inject a KMS-backed secret manager and short-lived token service, keeping secret values out of public responses, checkpoints, previews, logs, and model prompts.

## Process topology

Local mode can execute inline in one web process. Hosted work is durably
leased, heartbeated, recoverable, and terminal-write fenced, but the maintained
Cloud Run MVP still uses one web process and one instance because preview
delivery and parts of Session/checkpoint ownership remain process-local. A
future horizontally scalable production topology should separate
responsibilities:

```text
web/API -> Postgres work/event record -> worker -> model/MCP/remote sandbox
   ^                                      |
   +---------- tenant-scoped broker <-----+
```

Required horizontally scaled production services include:

- A distributed per-Session/checkpoint lock spanning side effects beyond the
  existing durable work-item leases and terminal-write fencing.
- A preview broker for live deltas between worker and web processes.
- Postgres for control-plane data and LangGraph checkpoints.
- Private S3-compatible object storage for bytes and artifacts.
- A scheduler service for due deployments.
- A webhook delivery service with retries and idempotency.

The current process-local preview bus and remaining Session/checkpoint lock are
development/preview mechanisms. They do not become distributed merely because
Postgres work leases are configured. Until the broker and complete distributed
ownership exist, the Cloud Run manifest must remain at `WEB_CONCURRENCY=1` and
`maxScale=1`; this preserves the public-beta process assumptions but does not
provide high availability.

## Data ownership

Core tables should remain independently migratable:

```text
Core:
  workspaces
  api_keys
  agents
  agent_versions
  environments
  sessions
  session_events
  managed_resources and versions
  workspace_quotas and quota counters/reservations
  audit_ledger and usage_ledger
  tenant_idempotency and session_event_idempotency

Hosted/private:
  organizations
  organization_members
  workspace_members
  roles and grants
  service_accounts
  billing_accounts, price books, balances, invoices, and payments
  enterprise audit export/retention state
  sso_connections
  support_access
```

Hosted tables may reference core workspaces. Core migrations and queries must not require hosted tables to exist.

The same rule applies to code dependencies: hosted code may import VMA, but VMA must not import private hosted packages. Provider hooks may accept implementations from the application at runtime.

## Deployment surface

Google Cloud Run is the repository's only maintained hosted deployment target. Core provider and storage boundaries remain explicit, but VMA does not publish or test deployment templates for other platforms.

```text
Dockerfile
cloudbuild.yaml
service.production.yaml
service.staging.yaml
scripts/start-web.sh
scripts/start-worker.sh
scripts/migrate.sh
scripts/gcloud/
```

Production and staging use separate VMA-owned Postgres databases or schemas and run migrations through a once-per-release Cloud Run Job before traffic moves. The control plane may use S3-compatible object storage and external providers even though its service runs on GCP. In particular, E2B sandboxes remain external sandbox resources rather than Cloud Run containers.

The optional worker still supports queued `self_hosted` Environment execution; it is a product-level execution protocol, not another supported control-plane hosting target, and it is not itself a remote sandbox. Scheduled Deployments retain their importable idempotent tick, but the checked-in Cloud Run MVP does not yet include an operated production scheduler.

See the [Cloud Run deployment guide](./deployment-platforms.md), [GCP operations guide](https://github.com/votrixai/votrix-managed-agents/tree/main/scripts/gcloud), and [work queue](./work-queue.md).

## Votrix core invariants

- Every persistent core resource is workspace-scoped.
- Every public lookup resolves the current workspace before returning or mutating data.
- Object-storage keys include a workspace partition.
- Public session IDs are never accepted as raw checkpoint authorization.
- Agent revisions are immutable; sessions resume with their pinned revision.
- Model endpoints, API keys, MCP headers, and sandbox IDs are server-controlled and redacted.
- Tenant-specific Deep Agents provider/harness profiles are never registered globally.
- Tenant shell execution never occurs in the web/worker host unless explicit unsafe local mode is enabled.
- Durable public events are persisted independently from best-effort previews.
- Durable work attempts are leased, heartbeated, recoverable, and fenced by
  lease generation before terminal writes.
- Audit and usage facts are append-only; raw usage is not priced billing.
- Hosted features may extend core but cannot become prerequisites for basic self-hosting.
- New resource families include cross-workspace non-visibility tests.
- Documentation labels wire compatibility separately from runtime and production semantics.

## Current boundary gaps

The interfaces for hosted implementations are not equally mature. Workspace
auth, narrow quotas, raw append-only ledgers, sandbox injection, and
server-controlled model configuration exist. A cross-process preview broker,
complete distributed Session/checkpoint ownership, KMS secret management,
Postgres RLS, Organization RBAC/SSO, enterprise audit operations, webhook
delivery, and optional commercial billing still need formalization. Hosted
implementations should avoid embedding those assumptions into unrelated core
resource tables.

The complete gap ledger is [known incompatibilities](./known-incompatibilities.md).
