---
title: Votrix Core Architecture
description: Ownership boundaries across the public API, durable control plane, and agent runtime.
---

VMA is the self-hosted, Organization-scoped Votrix core of a managed-agents platform. It exposes a Claude Managed Agents-shaped control plane and uses Deep Agents 0.6.12 as the execution kernel. A private hosted product should compose this package with enterprise identity, policy, infrastructure, and commercial services rather than fork the core.

The boundary is architectural, not a claim that the current core already provides Claude-equivalent managed infrastructure. See the [compatibility matrix](./compatibility-matrix.md) and [known incompatibilities](./known-incompatibilities.md).

## Layer model

```text
Hosted/private product
  members, RBAC, SSO, RLS, paid billing, audit operations, support
  hosted model gateway, secret manager, sandbox fleet, broker, scheduler
                              |
                              v
Votrix Managed Agents core
  Organization auth -> FastAPI compatibility routes -> durable resources/events
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
- Organization-scoped authentication interfaces and API-key implementations.
- Database API-key create/list/retrieve/revoke/rotate lifecycle, expiration,
  `api`/`api_keys:manage`/`worker` scopes, and trusted first-key bootstrap.
- Request IDs, stable error codes, and authenticated request audit correlation.
- Agent/version immutability and session version pinning.
- Environment and session resources, append-only events, session state, and work records.
- Deep Agents graph compilation and translation of runtime events into public events.
- Server-controlled model-provider routing for Anthropic, OpenAI, DeepSeek, and
  approved extensions, with model keys supplied only by Session-mounted
  Organization Vault model Credentials.
- LangGraph checkpointer selection.
- A sandbox factory interface plus a safe no-shell default.
- S3-compatible file and custom-skill bytes.
- Organization-scoped memory and credential resources.
- Optional self-hosted worker mechanics and an importable deployment scheduler tick.
- Durable work leases/generations, heartbeat, expired-attempt recovery, and
  stale-worker terminal-write fencing.
- Atomic Organization request, active-work, daily model-token, and stored-byte
  quotas with append-only raw audit/usage ledgers.
- Generic tenant idempotency for Session creation plus transactional event
  submission idempotency.
- A bootstrapped Organization experience for local development.

The core may expose extension interfaces, but it must stay useful without a private repository.

## Hosted/private responsibilities

A hosted or enterprise layer owns:

- Users, memberships, invitations, teams, and human/service-account identity.
- RBAC/ABAC, SSO/SAML/OIDC, SCIM, trust grants, and support impersonation policy.
- Advanced policy beyond the core's narrow Organization quotas, including sandbox
  compute/egress, tool/MCP, retention, and monetary spend controls.
- Commercial billing after the BYOK/free beta: price books, currency amounts,
  balances/credits, top-ups, refunds, Stripe, invoices, plans, seats, and taxes.
- Enterprise audit export, automated retention, legal hold, external tamper
  anchoring, and administrator/support access logging.
- Hosted model gateways and tenant credential policy, while preserving VMA's
  rule that tenant model traffic never falls back to a VMA-owned model key.
- KMS-backed secret management, credential rotation, OAuth enrollment/refresh, and revocation.
- Remote sandbox fleet selection, isolation, images, lifecycle, snapshots, and regional placement.
- Preview-transport operation and database connection budgeting. The core ships
  both a local in-process transport and a PostgreSQL `pg_notify` transport for
  hosted API/worker deployments.
- Production queues, dead-letter handling, scheduler operation, webhook delivery, and retry SLOs.
- Compliance controls, data residency, deletion verification, incident response, and Organization support tooling.

These concerns should not become required foreign keys or imports in the Votrix core data model.

## Tenant model

The only tenant boundary inside the core is `organization_id`.

```text
Hosted/private identity:
  user or service account -> Organization -> role/policy

VMA core request:
  AuthProvider -> CurrentOrganization(id=...) -> scoped resources and execution
```

Local self-hosting bootstraps an explicit Organization and database-backed key:

```text
bootstrap CLI -> Organization record + one-time administrator key
request API key -> authenticated organization_id
```

Public resource paths remain Organization-free:

```text
/v1/agents
/v1/sessions
/v1/files
```

The Organization comes from the authenticated request. Do not add public paths such as `/v1/organizations/{organization_id}/agents` to the compatibility API.

Every core persistent resource and query must be scoped by Organization unless the function is explicitly internal and named accordingly. Object-storage keys must begin with an Organization partition. Public session IDs must resolve through an Organization-filtered database record before VMA uses the opaque internal LangGraph thread ID.

## Application composition

The supported in-process extension point is:

```python
from votrix_managed_agents import create_app

app = create_app(auth_provider=HostedAuthProvider())
```

An auth provider implements the public `AuthProvider` protocol and returns `CurrentOrganization`. Core routers and query helpers then operate inside that scope.

The repository includes:

- `DatabaseApiKeyAuthProvider` for keys stored in the core database.
- The injectable `AuthProvider` path for hosted identity.

Those providers authenticate an Organization. Core request/quota activity can be
written to the append-only audit ledger, but the providers do not add
membership, roles, SSO, paid billing, or enterprise audit policy.

Prefer in-process provider injection over copying routers or placing an API-shape translation proxy in front of core. Run VMA as a separate internal service only when the deployment intentionally wants a network boundary and accepts the additional identity, tracing, and consistency work.

## Execution-provider boundaries

### Models

Agent resources select a provider/model. Built-in settings and
`VMA_MODEL_PROVIDERS` control approved adapters, endpoints, routing policy,
defaults, capabilities, and an internal provider credential-slot name. They
never contain a model API key, and VMA never reads model API keys from process
environment. At creation each key-based Session fixes either a matching model
Credential from its ordered `vault_ids` or an exact Organization platform-key
row, according to its explicit funding request and Organization policy. No
billing account preserves the existing BYOK-only behavior. Keyless `fake` and
`ollama` adapters use source `none`.

The public model-Credential API is deliberately distinct from generic Vault
Credentials used by MCP servers or other integrations. The provider ID maps to
the private slot internally, so callers do not submit names such as
`OPENROUTER_API_KEY`. One immutable funding binding is stored per Session in
the MVP, which requires a multiagent coordinator and its pinned subagents to
use the same provider.

VMA does not infer an Organization's end users. The Organization backend maps
its own users and billing records to VMA Session IDs. VMA records raw usage by
Organization and Session only.

A hosted product may route a tenant-supplied Vault credential through a
tenant-aware model gateway, but must preserve provider capability checks,
credential isolation, and usage attribution. See [model providers](./openai-compatible-providers.md).

### Sandboxes

`VMA_SANDBOX_FACTORY=module:attribute` injects a backend for an Organization/Session/Environment. A hosted implementation should return a remote `SandboxBackendProtocol` backed by containers or VMs. It is responsible for all actual security policy and lifecycle behavior.

The default `StateBackend` is safe because it has no shell, not because it is a production sandbox. The opt-in `LocalShellBackend` executes on the host and is excluded from untrusted production. See [sandbox runtime](./sandbox-runtime.md).

### Checkpoints and stores

LangGraph checkpoints preserve graph state and interrupts. VMA chooses Postgres for production-style DSNs and a separate SQLite checkpoint database locally. Checkpoints are internal runtime state; SQLAlchemy resource/event tables remain the public control-plane source of truth.

Cross-thread memory, files, and artifacts should use explicitly tenant-namespaced stores or object storage. No backend namespace may be derived from a caller-controlled public ID without an Organization lookup.

### MCP and secrets

Agent versions contain MCP names and URLs, not secrets. Sessions mount vault IDs. Runtime code matches credentials to server URLs and passes authorization only to the MCP client.

The Votrix core currently persists optionally encrypted credential material and lacks full OAuth refresh. A hosted product should inject a KMS-backed secret manager and short-lived token service, keeping secret values out of public responses, checkpoints, previews, logs, and model prompts.

## Process topology

Local mode can execute inline in one web process and defaults to the
`process_local` preview transport. The maintained Cloud Run deployment separates
HTTP/SSE API instances from private worker instances. Hosted work is durably
leased, heartbeated, recoverable, and terminal-write fenced; LangGraph
checkpoints and control-plane state are shared in PostgreSQL.

```text
API Cloud Run -> Postgres work/event record -> worker Cloud Run
      ^                                          |
      |                                          +-> model/MCP/E2B
      +---- Postgres NOTIFY preview channel <----+
```

The maintained horizontally scaled topology includes:

- Database-backed work and Session execution leases with generation fencing.
- PostgreSQL for control-plane data, LangGraph checkpoints, and the best-effort
  hosted preview transport.
- Private S3-compatible object storage for bytes and artifacts.
- A manually scaled worker fleet that polls durable work independently from API
  request autoscaling.

Hosted manifests use `VMA_PREVIEW_BROKER=pg_notify`; workers publish and API
processes hold one dedicated PostgreSQL `LISTEN` connection each. Local and
simple self-hosted deployments retain `process_local` as the zero-infrastructure
default. Both transports are best-effort: clients reconcile against durable
events after reconnect or frame loss. Supabase deployments use the transaction
pooler on port `6543` for control-plane, checkpoint, and `NOTIFY` publishing
traffic. A separate session-mode URL on port `5432` carries the lifetime
`LISTEN` connection and janitor advisory lock. Reserve one connection per API
(or combined-role) process beyond the ordinary application pool; migrations
use their own session/direct URL.

This topology is horizontally operable but not an exactly-once side-effect
engine. Provider calls, MCP tools, and sandbox commands still need their own
idempotency and cancellation semantics. A production scheduler for due
Deployments, webhook delivery, and queue-driven push dispatch/automatic worker
scaling remain separate future services; P3 is deliberately deferred.

## Data ownership

Core tables should remain independently migratable:

```text
Core:
  organizations
  api_keys
  agents
  agent_versions
  environments
  sessions
  session_events
  managed_resources and versions
  organization_quotas and quota counters/reservations
  audit_ledger and usage_ledger
  tenant_idempotency and session_event_idempotency

Hosted/private:
  organization_members
  roles and grants
  service_accounts
  billing_accounts, price books, balances, invoices, and payments
  enterprise audit export/retention state
  sso_connections
  support_access
```

Hosted tables may reference core Organizations. Core migrations and queries must not require hosted tables to exist.

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

- Every persistent core resource is Organization-scoped.
- Every public lookup resolves the current Organization before returning or mutating data.
- Object-storage keys include an Organization partition.
- Public session IDs are never accepted as raw checkpoint authorization.
- Agent revisions are immutable; sessions resume with their pinned revision.
- Model endpoints and routing are server-controlled; model API keys are
  Organization Vault-only. Model keys, MCP headers, and sandbox IDs are redacted.
- Tenant-specific Deep Agents provider/harness profiles are never registered globally.
- Tenant shell execution never occurs in the web/worker host unless explicit unsafe local mode is enabled.
- Durable public events are persisted independently from best-effort previews.
- Durable work attempts are leased, heartbeated, recoverable, and fenced by
  lease generation before terminal writes.
- Audit and usage facts are append-only; raw usage is not priced billing.
- Hosted features may extend core but cannot become prerequisites for basic self-hosting.
- New resource families include cross-Organization non-visibility tests.
- Documentation labels wire compatibility separately from runtime and production semantics.

## Current boundary gaps

The interfaces for hosted implementations are not equally mature. Organization
auth, narrow quotas, raw append-only ledgers, sandbox injection,
server-controlled model configuration, database-fenced multi-instance work,
and PostgreSQL cross-process previews exist. Exactly-once external side effects,
KMS secret management, Postgres RLS, Organization RBAC/SSO, enterprise audit
operations, webhook delivery, automatic queue-driven worker scaling, and
optional commercial billing still need formalization. Hosted implementations
should avoid embedding those assumptions into unrelated core resource tables.

The complete gap ledger is [known incompatibilities](./known-incompatibilities.md).
