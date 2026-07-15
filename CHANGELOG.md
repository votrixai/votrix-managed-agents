# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows semantic versioning once release tags begin.

## [Unreleased]

This work establishes the minimum multi-tenant **public-beta foundation**. It
does not claim production high availability, enterprise identity/compliance,
or paid billing readiness. The beta is BYOK/free.

### Added

- Database-backed workspace API keys with one-time plaintext create/rotate
  responses, hashes at rest, expiration, independent revocation, last-used
  tracking, replacement links, and `api`, `api_keys:manage`, and `worker`
  scopes.
- Authenticated API-key create/list/retrieve/revoke/rotate endpoints and the
  trusted `scripts.bootstrap_api_key` first-key CLI.
- `request-id`/`x-request-id` correlation on responses and audit facts, with
  stable machine-readable `error.code` values and typed OpenAPI error models.
- Atomic workspace defaults/overrides for requests per minute, active work,
  daily model tokens, and stored File/Skill bytes, including rate/quota reset
  headers and stable denial codes.
- A private, best-effort E2B runtime cost estimator that accumulates locally
  observed running intervals in `SessionSandbox.config`. Its formula uses the
  configured vCPU count and memory GiB with operator-controlled rates; defaults
  are `0.000014 USD/vCPU-second` and `0.0000045 USD/GiB-second`.
- Append-only audit and raw-usage ledgers protected by ORM guards and
  PostgreSQL/SQLite mutation-rejection triggers. Provider-reported raw usage is
  idempotent, carries provider/model attribution, and supports quotas and cost
  analysis only.
- Generic tenant idempotency records scoped by workspace, operation, key hash,
  and request fingerprint; Session creation uses them to replay the exact
  successful response. Session event submission retains its dedicated
  transactional idempotency record.
- Durable work attempt identity through unique lease IDs and monotonically
  increasing generations, automatic execution heartbeats, expired-lease
  recovery, stale-worker terminal-write fencing, and idempotent active-work
  quota release.
- A public-beta capability manifest and GA-only OpenAPI filter for API keys,
  Agents, Environments, Sessions, Files, Skills, Vaults/native model
  Credentials, and Model Providers.
- Native model-Credential list/retrieve/rotate/archive/delete lifecycle in both
  server and SDK surfaces.
- A synchronous `Votrix` provisioning client for API keys, Model Providers,
  Vaults, and native model Credentials alongside the full GA `AsyncVotrix`
  client.
- Native SDK cursor pagination, true incremental file downloads, reconnecting
  SSE with `Last-Event-ID`, bounded replay-safe retries, typed request IDs and
  stable error codes, API-key administration, and automatic Session/event
  idempotency keys.
- Public-beta readiness documentation and a dated next-session handoff.

### Changed

- Authentication now fails closed through database-backed workspace keys in
  local, development, staging, and production. The environment-key and
  anonymous development paths were removed; the trusted CLI bootstraps the
  first key.
- External HTTP workers now use tenant-bound database API keys with `worker`
  scope through the standard `x-api-key` or Bearer schemes; the separate static
  worker-authentication secret was removed.
- Hosted execution uses embedded durable consumers while the reference Cloud
  Run topology remains one warm instance, one web process, and `maxScale=1`.
- Object storage is private. VMA serves authenticated downloads and requires no
  `S3_PUBLIC_URL`, R2 public development URL, or public custom domain.
- Public GA hides presign/complete file uploads, Environment worker routes,
  Session Threads, Memory Stores, Deployments, User Profiles, Outcomes, and
  other deferred/experimental capabilities.
- The daily token quota uses an explicit one-turn overrun semantic: a turn
  admitted below the limit may cross it, its complete usage is recorded, and
  later turns are blocked until the UTC-day reset.
- E2B estimate profiles are frozen per locally observed running interval, so a
  rate/resource configuration change affects new intervals without rewriting
  earlier estimates. Estimation can be disabled with
  `VMA_E2B_COST_ESTIMATION_ENABLED=false`.

### Fixed

- Runtime event-history loading now paginates to the end instead of silently
  stopping after the first 500 events.
- Recovered work can no longer be finalized by an older lease held by the same
  worker ID.
- Active-work reservations are released on terminal completion/error/stop and
  remain held while queued or rescheduling.
- Session creation retries no longer create duplicate Sessions when callers or
  the native SDK reuse an idempotency key.
- Native SDK HTTP errors retain the server's stable error code and request ID.

### Security

- Hosted keys are stored only as SHA-256 digests; plaintext is emitted once and
  excluded from list/retrieve/revoke responses and logs.
- Private object storage is the byte source of truth; public bucket access is
  neither required nor recommended.
- Outbound web tools reject credentials in URLs, redirects, localhost,
  metadata/private/non-global addresses, and non-HTTPS/non-443 destinations;
  DNS answers are validated and the approved address is pinned at connection
  time to narrow rebinding exposure.
- Workspace quota counters and reservations use atomic database operations;
  audit and usage rows reject mutation at both application and database layers.
- Public SDK surfaces omit deferred Memory Stores and generic Vault Credential
  escape hatches; provider keys remain write-only and server-mapped.

### Deferred

- Cross-process live-preview delivery and complete distributed
  per-Session/checkpoint ownership. The checked-in `maxScale=1` constraint must
  remain until both exist.
- Postgres RLS; Organizations, memberships, human/service-account RBAC, SSO,
  and SCIM.
- Enterprise audit export, automated retention, legal hold, external tamper
  anchoring, and administrator/support access history.
- Presigned upload completion in public GA, webhook registration/delivery,
  production deployment scheduling, MCP OAuth refresh, and deferred resource
  families listed by `/v1/capabilities`.
- Paid billing: price books, monetary amounts, balances/credits, top-ups,
  refunds, Stripe, invoices, plans, seats, taxes, and spend alerts. None is a
  blocker for the BYOK/free public beta.
- Authoritative E2B billing reconciliation. The local estimate does not call an
  E2B usage/pricing API, consume an E2B webhook, discover current prices, write
  monetary amounts or bills to `usage_ledger`, or claim invoice accuracy.

### Testing

- Added focused contracts for API-key lifecycle/scopes/bootstrap, request IDs
  and error envelopes, governance counters/ledgers/idempotency, stale work
  leases and runtime-history pagination, outbound network restrictions,
  Session-create idempotency, public-GA OpenAPI schemas, and native SDK sync and
  async surfaces.
- Added deterministic E2B estimate formula/configuration tests and lifecycle
  tests for open/close interval accumulation, idempotent closure, pause/resume,
  delete, disabling, and non-E2B providers; no external E2B call is required.
- Existing validation entry points include the server test suite, dual-version
  Anthropic consumer contract matrix, Alembic upgrade/check, SDK pytest/pyright/
  build/wheel smoke, and Fumadocs typecheck/lint/build.
