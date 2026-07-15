---
title: Public-beta platform readiness handoff
description: Implementation, migration, deployment, validation, and residual-risk handoff for the 2026-07-15 multi-tenant public-beta foundation.
---

# Public-beta platform readiness handoff

Date: 2026-07-15
Audience: next implementation/release session
Release channel: public beta

## Outcome

The repository now contains the minimum platform foundation for a controlled
multi-tenant public beta:

- Requests in local, development, staging, and production fail closed through
  database-backed workspace API keys with lifecycle, expiration, revocation,
  rotation, and small scopes.
- Request IDs, stable error codes, and quota headers provide a supportable
  client contract.
- Hosted Session execution uses durable Postgres work leases with generations,
  heartbeats, recovery, and stale-attempt terminal-write fencing.
- Workspace request, active-work, daily model-token, and stored-byte limits are
  enforced atomically.
- E2B lifecycle transitions accumulate a private, best-effort local runtime
  cost estimate from configured vCPU/memory rates without calling E2B billing
  services.
- Raw usage and audit facts are append-only; generic tenant idempotency protects
  Session creation, while Session event submission keeps its transactional
  work-linked record.
- Object storage is private; authenticated VMA routes serve bytes. A public
  bucket URL is not required, and presign/complete uploads are hidden from GA.
- The native Python SDK has a full async GA client and a synchronous
  provisioning subset with pagination, streams, retries, API-key management,
  native model-Credential lifecycle, request IDs, and stable error codes.

This is not a production-HA, enterprise-identity/compliance, or paid-billing
claim. The public beta is BYOK/free. The raw usage ledger supports quota
enforcement and cost analysis; it does not contain a price book or authoritative
monetary balances.

The E2B estimate is a separate internal operations aid. It is not provider
usage truth, does not add monetary values or bills to `usage_ledger`, and is not
exposed as a customer-facing billing API.

## Product boundary frozen for this beta

Public GA includes API keys, Agents and versions, Environments without worker
operations, Sessions/events/resources without Threads, authenticated Files
without presign/complete, Skills, Vaults/native model Credentials, Model
Providers, health, and `/v1/capabilities`.

Explicitly deferred:

- Multi-instance preview delivery and complete distributed
  per-Session/checkpoint ownership. Keep one web process and `maxScale=1`.
- Memory Stores, Deployments/scheduling, Outcomes, User Profiles, Session
  Threads, system Skills, tunnels, GitHub repository resources, and MCP OAuth.
- Webhook endpoint registration/delivery; there is no beta webhook product
  promise.
- Postgres RLS; Organizations, memberships, human/service-account RBAC, SSO,
  and SCIM.
- Enterprise audit export/retention/legal hold/external anchoring and complete
  administrator/support access history.
- Paid billing: price books, monetary amounts, balances/credits, top-ups,
  refunds, Stripe, invoices, plans, seats, taxes, and spend alerts. These are
  not prerequisites for the BYOK/free beta.

## Important semantics

### API keys

- Local, development, staging, and production select
  `DatabaseApiKeyAuthProvider`; the trusted CLI creates the first workspace and
  administrator key after migrations.
- Key plaintext appears only in create/rotate output. The database stores a
  SHA-256 digest and non-secret metadata.
- `api` grants normal API access, `api_keys:manage` protects key lifecycle, and
  `worker` protects Environment work operations.
- Callers cannot choose a workspace through an untrusted request header.
- An invalid key has no trusted workspace and therefore cannot be written as a
  tenant-attributed audit event. Add a separate security-event sink later if
  hosted operations need invalid-auth aggregation.

### Quotas and raw usage

- Request quota is a per-workspace one-minute counter.
- Active work is a durable reservation keyed to the work ID and is released
  idempotently on completion, error, or stop.
- File and Skill writes enforce the workspace stored-byte limit.
- Model-token preflight admits a turn only while the UTC-day counter is below
  its limit. Provider-reported actual usage is known postflight and is appended
  exactly once. One admitted turn may cross the limit; subsequent turns are
  denied until reset. Do not “fix” this by dropping over-limit usage.
- Workspace override storage exists, but no public quota-administration API is
  promised. Environment defaults are the current operator control surface.

### Work ownership

- Each attempt receives a unique `lease_id` and increasing generation.
- Ack, execution heartbeat, and terminal writes must match current
  worker/lease/generation. Expired leases can be recovered under a new
  generation; the old attempt cannot finalize it.
- This fences the work item and final public state. It is not yet a distributed
  mutex spanning every checkpoint write and external provider side effect.
- Durable database events replay across processes; transient token/tool preview
  frames remain process-local.

### E2B local cost estimate

- `app.runtime.e2b_cost_estimation` records locally observed E2B running
  intervals under the private `_vma_e2b_cost_estimate` key in
  `SessionSandbox.config`; it adds no migration or public API field.
- The estimate is:

  ```text
  elapsed_seconds * (
    vCPU_count * vCPU_second_USD
    + memory_GiB * GiB_second_USD
  )
  ```

- The default assumptions are `0.000014 USD/vCPU-second` and
  `0.0000045 USD/GiB-second`. Both are configuration defaults, not discovered
  E2B prices or customer rates. `VMA_E2B_TEMPLATE_RESOURCES` supplies the
  allocated CPU and `memory_mb`; if the resource profile is missing/invalid or
  estimation is disabled, sandbox operation continues without an estimate.
- A resource/rate profile is frozen when an interval opens. Provision/connect
  and resume open intervals; pause, deletion, and covered failure transitions
  close them idempotently and accumulate `runtime_ms` plus `estimated_usd`.
- Open-interval summaries can project through the current time without
  mutating stored metadata. The helper ignores non-E2B providers.
- There is no E2B usage/pricing API call, billing webhook, invoice import, or
  reconciliation. The formula currently models only locally observed allocated
  vCPU/memory time; it can omit provider rounding, storage, network, templates,
  snapshots, taxes, credits, discounts, and other charges.
- The estimator does not write `estimated_usd`, a price, balance, charge, or
  invoice into `usage_ledger`. That ledger remains raw metering/quota data;
  neither system is an authoritative customer bill.

### Idempotency

- Session create uses the generic `tenant_idempotency` record keyed by
  workspace, operation, key hash, and request fingerprint. Same key/body
  replays; a different body conflicts; an in-progress request is reported.
- Session events use `session_event_idempotency`, created in the same
  transaction as event append/work enqueue and linked to the work ID.
- The native SDK generates keys for Session create and event submission. A
  caller-supplied key is required when identity must survive a caller process
  restart.
- These contracts do not imply exactly-once behavior for every endpoint or an
  external model provider.

## Implementation file map

| Area | Primary files |
| --- | --- |
| Workspace auth and scopes | `app/auth.py`, `app/workspace.py`, `app/db/queries/api_keys.py` |
| API-key HTTP lifecycle and bootstrap | `app/routers/api_keys.py`, `app/models/api_keys.py`, `scripts/bootstrap_api_key.py` |
| Request IDs and error contract | `app/factory.py`, `app/errors.py`, `app/models/status.py` |
| Governance service and headers | `app/governance.py`, `app/governance_runtime.py`, `app/db/queries/governance.py` |
| Governance persistence | `app/db/models.py`, `alembic/versions/20260715_0015_workspace_governance.py` |
| Work leases and embedded consumer | `app/runtime/work_queue.py`, `app/worker.py`, `app/routers/environments.py`, `app/factory.py` |
| Runtime usage accounting | `app/runtime/runner.py`, `app/runtime/work_queue.py` |
| Private E2B runtime estimate | `app/runtime/e2b_cost_estimation.py`, `app/runtime/sandbox_lifecycle.py` |
| Storage quota hooks | `app/routers/files.py`, `app/routers/skills.py` |
| Session/event idempotency | `app/routers/sessions.py`, `app/db/queries/event_idempotency.py`, `app/governance.py` |
| Private object storage | `app/storage.py`, `app/routers/files.py`, `app/routers/skills.py` |
| Public GA filter/capabilities | `app/public_surface.py`, `app/factory.py`, `scripts/export_openapi.py` |
| Outbound SSRF boundary | `app/network_security.py`, `app/runtime/deepagent_tools.py` |
| Native SDK | `sdks/python/src/votrix/`, `sdks/python/tests/`, `docs/python-sdk.md` |
| Hosted topology | `service.production.yaml`, `service.staging.yaml`, `cloudbuild.yaml`, `scripts/gcloud/` |
| Release narrative | `CHANGELOG.md`, `README.md`, `docs/compatibility-matrix.md`, `docs/known-incompatibilities.md` |

## Migration order

Run the chain in order through `alembic upgrade head`; do not cherry-pick only
the final revision:

1. `20260713_0012` — creates `session_event_idempotency`, unique by workspace,
   Session, and key hash, with the canonical request hash, linked work ID, and
   exact successful response.
2. `20260713_0013` — adds the idempotent
   `ix_managed_resources_type_parent_name` lookup index used by Session file
   discovery.
3. `20260714_0014` — adds API-key scopes, expiration, creation/revocation
   metadata, and rotation replacement links. Existing database keys receive
   the legacy-compatible `api`, `api_keys:manage`, and `worker` scopes; archived
   rows are backfilled as revoked.
4. `20260715_0015` — creates workspace quota overrides, atomic counters,
   active-work reservations, append-only audit/usage ledgers, generic tenant
   idempotency, indexes, constraints, and PostgreSQL/SQLite append-only
   triggers.

Deploy/migrate before starting the new application revision, then bootstrap the
first workspace key before sending authenticated traffic. The GCP scripts run a
dedicated migration Job and replace the service only after it succeeds. A
downgrade from `0015` drops governance ledgers and their data; treat rollback as
a data-loss decision, not a routine retry.

## Configuration knobs

| Purpose | Settings |
| --- | --- |
| Environment/protocol | `APP_ENV`, `VMA_REQUIRE_BETA_HEADER`, `VMA_REQUIRE_ANTHROPIC_VERSION_HEADER`; authentication keys are database records created through the bootstrap/API lifecycle |
| Governance | `VMA_GOVERNANCE_ENABLED`, `VMA_REQUESTS_PER_MINUTE`, `VMA_MAX_ACTIVE_WORK`, `VMA_DAILY_MODEL_TOKENS`, `VMA_WORKSPACE_STORAGE_BYTES` |
| Durable consumer | `VMA_EMBEDDED_WORKER_ENABLED`, `VMA_WORKER_CONCURRENCY`, `VMA_WORKER_POLL_INTERVAL_SECONDS`, `VMA_WORKER_LEASE_SECONDS`; external workers use tenant-bound database API keys with `worker` scope |
| Public surface/browser | `VMA_PUBLIC_GA_ONLY`, `VMA_CORS_ORIGINS` |
| Database/checkpoints | `DATABASE_URL`, optional `VMA_CHECKPOINT_DATABASE_URL` |
| Private object storage | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, `S3_REGION` |
| Upload/runtime limits | `VMA_MAX_FILE_UPLOAD_BYTES`, `VMA_MAX_SESSION_INPUT_BYTES`, `VMA_MAX_SKILL_ARCHIVE_BYTES` |
| Secret storage | `VMA_ENCRYPTION_KEY`, local-only `VMA_ALLOW_PLAINTEXT_SECRETS_LOCAL` |
| Web-tool egress | `VMA_WEB_FETCH_MAX_BYTES`, `VMA_WEB_SEARCH_ENDPOINT`, `VMA_WEB_ALLOW_PRIVATE_NETWORKS` (keep `false` for tenants) |
| Model/sandbox | `VMA_DEFAULT_MODEL_PROVIDER`, `VMA_MODEL_PROVIDERS`, provider keys, `VMA_SANDBOX_PROVIDER`, `VMA_SANDBOX_FACTORY`, E2B settings |
| E2B internal estimate | `VMA_E2B_COST_ESTIMATION_ENABLED`, `VMA_E2B_VCPU_SECOND_USD`, `VMA_E2B_GIB_SECOND_USD`, resource inputs from `VMA_E2B_TEMPLATE_RESOURCES` |

Hosted public beta should keep:

```dotenv
APP_ENV=production
VMA_GOVERNANCE_ENABLED=true
VMA_EMBEDDED_WORKER_ENABLED=true
VMA_PUBLIC_GA_ONLY=true
VMA_WEB_ALLOW_PRIVATE_NETWORKS=false
VMA_ALLOW_PLAINTEXT_SECRETS_LOCAL=false
```

Use `APP_ENV=staging` in staging; the remaining fail-closed/public-GA settings
stay the same.

Do not restore `S3_PUBLIC_URL` or a public R2 domain. The bucket remains private.

## Bootstrap and deployment commands

### Local/staging database preparation

```bash
uv sync
uv run alembic upgrade head
uv run python -m scripts.bootstrap_api_key \
  --workspace-id wrkspc_example \
  --workspace-slug example \
  --workspace-name "Example"
```

The last command writes the plaintext API key once. Run it in a trusted
administrator environment and move the output directly to the intended secret
store. Do not write it into the repository or service logs. Use the
authenticated API for later keys and rotations. Local/development clients may
read the stored workspace secret through `VOTRIX_API_KEY`; the service does not
use that client variable as a process-global authentication setting.

### Checked-in Cloud Run path

One-time setup and secret import:

```bash
./scripts/gcloud/0-setup-registry.sh
cp .env.production.example .env.production
cp .env.staging.example .env.staging
./scripts/gcloud/1-create-secrets.sh .env.production
./scripts/gcloud/1-create-secrets.sh .env.staging --suffix staging
```

Deploy staging first, then production after its gates pass:

```bash
./scripts/gcloud/3-deploy-staging.sh
./scripts/gcloud/status.sh
./scripts/gcloud/2-deploy-production.sh
```

Optional trigger and public Cloud Run URL setup are documented in
`scripts/gcloud/README.md`:

```bash
./scripts/gcloud/4-setup-triggers.sh <github-owner> <repo-name>
./scripts/gcloud/5-allow-public.sh
```

Public Cloud Run ingress does not bypass VMA API-key authentication. Keep the
service manifest at `minScale=1`, `maxScale=1`, and `WEB_CONCURRENCY=1`.

### Credentialed staging smoke

```bash
VMA_SMOKE_BASE_URL=https://YOUR-STAGING-CLOUD-RUN-URL \
VMA_SMOKE_API_KEY=... \
uv run --extra sandbox-e2b python scripts/pilot_acceptance.py
```

## Validation record

Focused suites observed during implementation (useful evidence, not the final
release gate):

- Work queue/event/runtime history: 17 passed.
- Worker/work-queue focus: 13 passed.
- Outbound network/deep-agent security: 26 passed.
- Auth/request IDs: 7 passed.
- Governance HTTP: 1 passed.
- Session-create idempotency: 3 passed.
- Session/event semantics: 32 passed.

Final local validation on the settled 2026-07-15 workspace is recorded below.
PostgreSQL integration and credentialed staging remain explicit external gates;
they were not reported as passing without a test database or deployment
credentials.

| Gate | Command | Final result |
| --- | --- | --- |
| Server suite | `.venv/bin/pytest -q` | `PASS — 439 passed, 3 PostgreSQL tests skipped` |
| Anthropic consumer matrix | `./scripts/test-backend-contract-matrix.sh` | `PASS — 4 passed on anthropic 0.97.0 and 4 passed on 0.116.0` |
| Migration head/check | isolated SQLite `alembic upgrade head` and `alembic check` | `PASS — full 0001–0015 upgrade; no new operations detected` |
| PostgreSQL migration/concurrency | PostgreSQL-marked tests and production-like `alembic` gate | `NOT RUN — VMA_TEST_POSTGRES_URL was unavailable` |
| SDK tests/server contract | SDK `uv run pytest -q`; server SDK contract pytest | `PASS — 32 SDK tests and 3 ASGI contract tests` |
| SDK typing | `cd sdks/python && uv run pyright` | `PASS — 0 errors, 0 warnings` |
| SDK artifacts | `cd sdks/python && uv build`, `twine check`, isolated wheel install/import | `PASS — sdist/wheel built and validated; Python 3.13 wheel smoke passed` |
| E2B local estimate focus | `.venv/bin/pytest -q tests/test_e2b_cost_estimation.py tests/test_sandbox_lifecycle.py -k cost` | `PASS — 6 passed, 8 deselected; does not imply invoice accuracy` |
| Public OpenAPI export/schema gate | `cd website && npm run openapi:sync` plus public schema tests | `PASS — 40 paths exported; 21 schema/docs/public-surface tests passed` |
| Docs typecheck/lint/build | `cd website` typecheck, lint, and build scripts | `PASS — 174 static pages generated` |
| Dependency lock | `uv lock --check` | `PASS — 134 packages resolved with no lock changes required` |
| Repository whitespace | `git diff --check` | `PASS` |
| Credentialed staging smoke | `scripts/pilot_acceptance.py` | `NOT RUN — staging URL/API key/E2B credentials were unavailable` |

## Residual risks

1. `maxScale=1` is a correctness guardrail, not availability. Revision overlap
   and process failure can still lose transient previews.
2. Work-attempt fencing prevents stale terminal writes but does not guarantee
   exactly-once provider calls or fence every checkpoint/external side effect.
3. The request limiter performs durable database writes and fails closed with
   authentication; database latency/availability is therefore on the API path.
4. Postgres RLS is absent. Application-scoped queries and tests remain the
   primary tenant boundary; a full two-workspace matrix must stay in the gate.
5. Audit/usage rows are append-only in normal application/database operation,
   but there is no external tamper anchor, enterprise export, or retention job.
   Bounded request-counter/completed-idempotency cleanup exists as an internal
   service method but is not run by an operated scheduler.
6. One model turn can overrun the daily token limit by its actual postflight
   usage. This is documented and intentional.
7. Token accounting depends on the model adapter returning parseable usage.
   Missing provider usage is not guessed; alert on unmetered successful turns
   and keep provider contract tests in the release gate.
8. The E2B dollar estimate depends entirely on configured assumptions and
   locally observed lifecycle intervals. It can drift from an E2B invoice and
   omit provider-specific charges; never expose it as customer billing or use
   it for settlement without independent reconciliation.
9. Invalid credentials cannot be mapped to a tenant audit row. Edge/security
   logs must cover brute-force and credential-stuffing operations.
10. Private storage still needs operator backup, lifecycle, orphan inspection,
   malware policy, and regional controls. Public presigned upload completion is
   intentionally unavailable in GA.
11. Workspace quota override persistence exists without a public operator UI or
   documented admin CLI. Defaults are environment-controlled for this beta.
12. No Organization RBAC/SSO, RLS, enterprise audit operations, webhook
    delivery, or paid billing is implied by “multi-tenant public beta.”

## Next-session checklist

1. Inspect `git status`, review the settled diff, and never discard unrelated
   user changes.
2. Re-run the local gates on the release commit and fix failures before
   deployment; then complete the PostgreSQL and credentialed-staging gates that
   are explicitly marked `NOT RUN` above.
3. Confirm exported OpenAPI contains only the GA path allowlist and no empty
   request or 2xx JSON schemas; review the rendered API playground.
4. Exercise migrations `0012` through `0015` against a production-like Postgres
   clone, including append-only trigger checks and a documented backup/restore.
5. Run concurrent Postgres tests for request counters, active-work reservation
   acquire/release, usage idempotency, same-key Session create, lease expiry,
   and stale-generation completion.
6. Complete the two-workspace denial matrix across lookup, pagination, SSE,
   work execution, checkpoints, private objects, Vaults, and E2B lifecycle.
7. Deploy staging, bootstrap a staging-only management key, create a separate
   least-privilege application key, and run the credentialed pilot smoke.
8. Observe database latency, quota denial rates, stuck work, lease recovery,
   token overrun or missing provider usage, storage growth, and audit/usage
   volume before production.
9. Compare the configured E2B rates/formula with current provider invoices for
   internal forecasting, document the calibration date, and alert on drift.
   Do not turn the estimate into a customer-visible charge.
10. Define retention cutoffs and an operator job for bounded expired request
   counter/completed-idempotency cleanup; do not mutate append-only ledgers.
11. Keep `maxScale=1` and the GA filter enabled. Do not expose deferred routes or
   a public bucket as a workaround.
12. Treat Organization/RLS/enterprise audit work as the next platform phase.
    Add commercial billing only if/when the free BYOK beta product decision
    changes; it is not a current release blocker.
