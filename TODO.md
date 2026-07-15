# VMA TODO

## Votrix production compatibility critical path

Goal: make VMA the only Managed Agents control plane used by
`votrix-backend`, while preserving the Claude Managed Agents API/SDK behavior
that the backend actually consumes. This is a consumer-driven compatibility
target, not a promise to clone every current or future Claude beta feature.

### Delivery rules

- Keep the existing `AsyncAnthropic` call sites. Select VMA with an explicit
  root `base_url`, API key, and model profile; do not add a generic gateway.
- Treat the current `votrix-backend` behavior as the executable compatibility
  contract. A VMA endpoint is not complete merely because its response passes
  one SDK model.
- Do not mix Claude and VMA resources. Agent, Environment, Skill, Memory Store,
  File, and Session IDs are control-plane-specific and must be reprovisioned.
- Do not do Session-level percentage routing. Skills and Memory would split
  across two writable control planes.
- Keep Skills, Agent versions, Environment/template configuration, and existing
  mounted inputs immutable. Dynamic user files may only use the separately
  specified append-only resource path below.

### Public-beta platform baseline (completed 2026-07-15)

- [x] Fail closed in hosted environments with database-backed tenant API keys,
  lifecycle APIs, small scopes, and a trusted first-key bootstrap command.
- [x] Correlate responses and audit facts with request IDs; return stable
  machine-readable error codes and quota reset headers.
- [x] Run hosted turns from durable Postgres work with lease IDs, generations,
  heartbeats, recovery, and stale-attempt terminal-write fencing.
- [x] Enforce request, active-work, daily model-token, and stored-byte limits
  with append-only raw usage and audit ledgers.
- [x] Protect Session creation with generic tenant idempotency and preserve the
  dedicated transactional idempotency contract for Session event submission.
- [x] Keep object storage private and hide presign/complete upload routes from
  the public GA schema.
- [x] Publish a native async GA client plus a synchronous provisioning subset
  with pagination, streaming, reconnecting SSE, retries, typed errors, API-key
  administration, and native model-Credential lifecycle.

This is a BYOK/free public-beta baseline, not production HA or enterprise
readiness. Multi-replica previews/Session ownership, Postgres RLS, Organization
RBAC/SSO, enterprise audit export/retention, and webhook delivery remain open.
Commercial billing is not a beta requirement.

### Native Python SDK and provider BYOK

- [x] Keep the server distribution/import surface unchanged and create an
  independently buildable `votrix` project under
  `sdks/python`, imported as `from votrix import AsyncVotrix`.
- [x] Add an authenticated, secret-free model-provider catalog and a native
  model-Credential create endpoint that accepts `provider` plus `api_key`
  without exposing `api_key_env` or `secret_name`.
- [x] Preserve the Claude-compatible low-level REST/`AsyncAnthropic` surface;
  use the native SDK for VMA-specific provider discovery and BYOK helpers.
- [ ] Publish `sdk-python-v0.1.0` through PyPI Trusted Publishing after the
  isolated SDK CI, native server contract tests, and package metadata pass on
  the release commit.
- [ ] Migrate `votrix-backend` to `AsyncVotrix` behind its existing provider
  enum only after the native consumer contract covers every production call;
  never mix resource IDs between Claude and VMA.

### P0-0 — Freeze the backend consumer contract (in progress)

- [x] Add a narrow SDK contract suite covering only the public SDK paths used by
  `votrix-backend`: Agents, Environments, Sessions, Events/SSE, Session
  Resources, Files, Skills, and Memory Stores.
- [x] Run that same narrow suite with both:
  - `anthropic==0.97.0`, the current production backend caller.
  - `anthropic==0.116.0`, the current strict VMA contract target.
- [x] Keep the broader VMA discovery/contract suite on `0.116.0`; do not weaken
  it to accommodate the older backend SDK.
- [x] Add backend-to-VMA consumer contracts for Agent/Skill provisioning,
  Environment/Session creation, opening a real loopback stream before sending
  `user.message`, two consecutive turns, custom-tool result parsing, and delete.
- [x] Add a repeatable credentialed acceptance smoke that drives the public SDK
  through real Postgres, R2, E2B pause/resume, and model execution without using
  production credentials in ordinary CI (`scripts/pilot_acceptance.py`).
- [ ] Pin the accepted beta/version headers and error envelopes in fixtures.
- [ ] Record every known semantic gap as an expected failing acceptance test,
  not as an undocumented exception.

Acceptance gate: a backend SDK upgrade or VMA API change cannot merge unless
the current production caller and target SDK both pass the consumer suite.

### P0-1 — Durable and idempotent turn execution

- [x] Add optional `Idempotency-Key` support to Session event submission. Store
  the key hash, canonical request hash, linked work ID, and exact successful
  response in the same transaction as event append and work enqueue.
- [x] Make `votrix-backend` attach a stable key to every Managed Agents event
  POST and reuse it across both SDK-internal retries and the existing outer 529
  retry loop.
- [x] Serialize same-Session event submissions with a PostgreSQL row lock and
  add a dedicated PostgreSQL CI test proving concurrent same-key requests
  create one input event, one work item, and one replay record.
- [ ] Extend the logical request identity through the complete `/chat` request
  lifecycle. The current completed slice prevents duplicate provider POSTs
  during one live Backend attempt; a brand-new Backend process/request still
  needs durable `client_message_id` handling before it can safely replay a turn.
- [x] Give every queued turn a durable work ID. Keyed event submissions persist
  the request hash, work ID, and exact successful response; the native SDK
  supplies keys by default.
- [ ] Prevent duplicate model execution, event emission, and raw usage
  attribution when `user.message` or a custom-tool result is retried.
- [x] Use a durable Postgres consumer in hosted environments. Every attempt
  acquires a unique lease ID/generation, heartbeats for the full model timeout,
  and recovers queued/rescheduling/expired work after a process or revision
  restart. Local/test may still use a response-following task over the same
  durable work protocol.
- [x] Fence terminal Session events/status and work completion with
  `(work_id, lease_id, run_generation)`, so a stale worker cannot finalize a
  recovered attempt.
- [ ] Replace process-local per-Session ownership with database or equivalent
  fencing before raising Cloud Run above one worker/instance.
- [x] Recover expired leased/running attempts with a new generation and make
  terminal completion/error/stop release the active-work reservation
  idempotently.
- [ ] Deliver ordered token/tool previews across processes, or persist bounded
  batched deltas that SSE clients can tail by sequence.
- [ ] Verify the exact custom-tool `requires_action` handshake, retry, interrupt,
  and cancellation behavior expected by `votrix-backend`.

Pilot boundary: `minScale=1`, `maxScale=1`, and one Uvicorn worker may be used
for a controlled Votrix-only pilot, but that is not the scalable production
gate and must remain visible in operations documentation.

### P0-2 — Dynamic files and generated artifacts

- [x] Implement active, idle-only, append-only `sessions.resources.add` for user
  files. An idle `requires_action` custom-tool window is intentionally allowed.
- [x] Restrict E2B mounts to one direct filename below the fixed read-only
  `/mnt/session/uploads` root; reject overwrite, conflicting/overlapping paths,
  nesting, traversal, symlinks/hardlinks, and mutable-root overlap. Existing
  mounted inputs cannot be updated or deleted.
- [x] Verify Workspace ownership, size, copied object identity, and SHA-256
  before advancing the sandbox seal and uncommitted resource row.
- [x] Advance a monotonic immutable Session manifest revision after each
  successful append and verify the latest digest, revision, manifest, paths,
  permissions, and contents on every Sandbox reconnect.
- [x] Discover direct regular files created under `/mnt/session/outputs` at turn
  completion, upload immutable versions to R2-compatible object storage, and
  expose them as downloadable session-scoped Files.
- [x] Support the `files.list(scope_id=session_id)` and download behavior used by
  `present_file_to_user` and the Votrix file tools.
- [x] Resolve supported image/document file blocks only against immutable files
  mounted in the current Session, then translate verified bytes into LangChain
  standard blocks instead of passing VMA/Anthropic-shaped IDs to OpenRouter.
  Text-only profiles receive a sandbox-path marker for binary attachments.
- [x] Add and run a credentialed real-E2B acceptance test for append,
  pause/resume, output discovery, scoped listing, and download. The repeatable
  manual release smoke lives at `scripts/pilot_acceptance.py`; deterministic
  provider and API tests remain the ordinary-CI coverage.
- [x] Cover the provider-ahead/database-rollback window with a deterministic
  fault-injected integration test: leave the provider seal at the exact next
  revision while the database remains at the previous revision, retry the same
  add, and verify the database commit completes.
- [ ] Repeat that recovery window against credentialed real E2B and PostgreSQL;
  also exercise append against concurrent Session delete/archive and retention
  cleanup under PostgreSQL row locks.
- [ ] Replace the bounded base64/JSON provider output handoff with streaming
  object transfer before increasing the current output count or byte limits.
- [ ] Move Session-scoped Files cursor pagination fully into the database so a
  Session with more than 1,000 input/output File versions remains pageable.

The append CAS crosses PostgreSQL and E2B and is therefore not globally atomic.
VMA advances E2B before committing the database manifest. A crash in that
window is recoverable only by retrying the exact same file, bytes, and mount
path; unrelated append/resume attempts fail closed. There is deliberately no
outbox, operation lease, automatic orphan recovery, snapshot, or replacement
Sandbox in this MVP.

Acceptance gate: an existing Session can receive a new file, use it on the next
turn, generate an output, expose that output through the Files API, and retain
both across E2B pause/resume without allowing an old input to change.

### P0-3 — Durable Memory Store runtime writeback

- [ ] Load the latest bounded Memory Store version when a Session is created.
- [ ] Track changes made only within approved read-write memory roots.
- [ ] At idle/pause, write changed memory back to the relational Memory Store
  using content hash/version preconditions.
- [ ] Never silently use last-write-wins when two Sessions update one store;
  preserve conflicts and surface a deterministic result.
- [ ] Prove that a memory written in Session A is available in a newly created
  Session B for the same employee.

Acceptance gate: the database, not one E2B filesystem, remains the durable
cross-Session Memory Store source of truth.

### P0-4 — Tenant isolation and credentials

- [x] Match CMA Vault ordering and lifecycle for the current credential path:
  first matching Vault wins, active keys are unique, each Vault is capped at
  20 Credentials, structural keys are immutable, and Vault archive/delete
  revokes child Credentials.
- [x] Allow a Session Vault `environment_variable` Credential to provide the
  selected model provider's `api_key_env` without copying the key into E2B.
- [x] Resolve the first matching model Credential once at Session creation,
  persist only its ID, and fail closed after revocation instead of changing the
  Session's payer. The trusted caller expresses user/shared preference through
  `vault_ids` ordering; VMA does not add a separate policy enum.
- [x] Complete the database-backed scoped API-key lifecycle and trusted
  bootstrap path required for the public beta.
- [ ] Enforce Workspace ownership for every relational row, R2 object, stream,
  checkpoint, work item, and E2B Sandbox binding.
- [ ] Fix `votrix-backend` file/session ownership checks before using one shared
  VMA service tenant for multiple Votrix Workspaces.
- [x] Add atomic per-Workspace limits for requests/minute, active work, stored
  File/Skill bytes, and daily model tokens. Sandbox-count and sandbox-compute
  limits remain deferred.

Acceptance gate: a two-Workspace denial matrix covers reads, writes, streaming,
background work, object storage, and external Sandbox lifecycle operations.

### P0-5 — Provisioning, model identity, and raw usage

- [x] Implement CMA create-time `agent_with_overrides` for model, system,
  tools, MCP servers, and Skills, persist the resolved Session snapshot, and
  pin custom Skill `latest` references before sandbox bootstrap.
- [ ] Replace checked-in provider IDs with environment/control-plane-scoped
  mappings for Environments, Skills, Agents, Memory Stores, and Files.
- [ ] Make full provisioning idempotent from logical Votrix Agent/Skill source.
- [ ] Resolve a logical model profile to the deployment model; VMA currently
  uses `deepseek/deepseek-v4-pro` for the fast profile.
- [x] Append provider-reported actual token usage with provider/model attribution
  exactly once to the raw usage
  ledger and enforce the daily token budget. A turn admitted below the limit
  may cross it; the full usage is retained and later turns are blocked until
  the UTC-day reset.
- [x] Keep raw usage provider/model-attributed so operators can analyze BYOK
  cost without assuming Anthropic COGS. Priced billing is explicitly outside
  the free public-beta gate.
- [ ] Define a one-way cutover: reprovision resources, import Memory once, send
  all new Sessions to VMA, and leave old Claude Sessions read-only or closed.

### P0-6 — Production operations and cost controls

- [ ] Correlate Workspace, Session, turn, work item, model request, and Sandbox
  IDs in structured logs and metrics without logging secrets.
- [ ] Alert on stuck work, duplicate execution, model/provider errors, Sandbox
  create/pause/delete failures, and retention backlog.
- [x] Enforce the public-beta request, active-work, daily model-token, and
  stored-byte budgets. Tool/MCP, per-turn, E2B compute/egress, and monetary
  spend budgets remain deferred.
- [ ] Run retention cleanup independently of request traffic and scale-to-zero;
  paused E2B Sandboxes must eventually be explicitly deleted.
- [ ] Document database backup/restore, migration rollback, provider outage,
  orphan inspection, and production cutover runbooks.

### Explicitly outside the Votrix cutover critical path

- Scheduled deployments and webhook delivery.
- Durable Claude-style multi-agent threads.
- Native MCP tunnels, GitHub repository mounts, and Anthropic system Skills.
- Daytona, Sandbox generations/snapshots, and automatic provider migration.
- Paid billing: price books, monetary amounts, balances/credits, top-ups,
  refunds, Stripe, invoices, plans, seats, taxes, and spend alerts.
- Byte-for-byte Claude compaction, undisclosed infrastructure behavior, or
  complete compatibility with future SDK endpoints that Votrix does not use.

## Multi-tenant API keys

Status: implemented for the public-beta workspace boundary. Enterprise human
identity and organization hierarchy remain deferred.

### MVP tenancy decision

- Treat the existing `Workspace` as the tenant and Organization boundary.
- Do not add a separate Organization-to-Workspace hierarchy yet.
- An Organization/Workspace may own multiple API keys for different callers,
  such as a production backend, developer laptop, CI, and third-party
  integration.
- Keep `VMA_API_KEY` only for local development or deliberate embedded use.
  Staging and production authenticate with database-backed workspace keys;
  the trusted bootstrap CLI emits the first secret once without installing a
  permanent global production key.

### Existing foundations

- `workspaces` and `api_keys` database tables exist.
- API keys are stored as SHA-256 hashes; plaintext is returned only at creation
  or rotation.
- API key records already bind to `workspace_id`, expose a non-secret prefix,
  track `last_used_at`, and support archival.
- `DatabaseApiKeyAuthProvider` resolves a key to request workspace context.
- Core resources already carry `workspace_id` in the database.
- R2 object keys and E2B ownership include the workspace boundary.

### Required implementation

- [x] Add an Alembic migration for API key authorization and lifecycle fields:
  - `scopes`
  - `expires_at`
  - `created_by`
  - explicit revocation metadata if archival is insufficient
- [x] Make database-backed authentication the default for staging and
  production while preserving local anonymous/bootstrap behavior.
- [x] Add authenticated API key management endpoints:
  - `POST /v1/api_keys`
  - `GET /v1/api_keys`
  - `GET /v1/api_keys/{api_key_id}`
  - `POST /v1/api_keys/{api_key_id}/revoke`
  - `POST /v1/api_keys/{api_key_id}/rotate`
- [x] Return a newly generated plaintext key exactly once and never persist or
  log it.
- [x] Start with the small `api`, `api_keys:manage`, and `worker` scope model;
  expand only when product requirements justify it.
- [x] Enforce expiration, revocation, and scopes in request dependencies.
- [x] Ensure callers cannot select a tenant with an untrusted
  `X-Workspace-ID`-style header.
- [x] Define a secure bootstrap path for creating the first workspace admin key
  without leaving a permanent global production key.
- [x] Attribute authenticated key lifecycle and request authorization/completion
  to the workspace/key without recording plaintext credentials. Invalid keys
  cannot be tenant-attributed and still need a separate security-event sink if
  hosted operations require one.

### Isolation audit and tests

- [ ] Add a two-workspace denial matrix proving Workspace A cannot read,
  mutate, stream, or delete Workspace B resources.
- [ ] Audit ID-based lookup, pagination, and background execution paths for:
  - Agents and Agent versions
  - Environments
  - Sessions, events, previews, and checkpoints
  - Files, Skills, and R2 object keys
  - Memory stores and Vaults
  - Deployments, scheduled runs, and workers
  - Webhooks
  - E2B sandbox bindings and cleanup
- [x] Verify revoked/archived and expired keys receive `401` and insufficient scopes
  receive `403`.
- [x] Verify key rotation does not interrupt unrelated keys belonging to the
  same workspace.
- [ ] Consider PostgreSQL row-level security as defense in depth after the
  application-level workspace audit is complete.

### Possible hosted identity path

If all customer traffic passes through `votrix-backend`, consider accepting a
short-lived backend-signed JWT containing `workspace_id`, audience, scopes, and
expiry instead of storing one long-lived backend API key per Organization.
Direct Claude-compatible SDK users can continue to receive workspace-scoped API
keys. Do not trust an unsigned tenant identifier forwarded by another service.

### Explicitly deferred

- Separate Organization and Workspace tables.
- Multiple Workspaces under one Organization.
- Organization membership and human-user RBAC.
- Cross-workspace Organization administrator roles.
- Commercial billing and advanced quota-policy ownership at the Organization
  level.
- Automated migration of existing tenants into a future two-level hierarchy.

### Acceptance criteria

- Production can run without a permanent global `VMA_API_KEY`.
- Every authenticated request resolves exactly one trusted workspace context.
- Every API key belongs to one workspace; a workspace can own many keys.
- Plaintext API keys are shown once, hashed at rest, redacted from logs, and
  independently revocable.
- Cross-workspace access tests cover all durable and external resources.
- Existing Claude-compatible API and SDK request shapes remain unchanged.
