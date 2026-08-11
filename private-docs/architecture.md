# VMA First-Release Architecture

Internal only. Do not copy this document into the public documentation tree.
`docs/votrix-core-architecture.md` is the public, contract-level view;
deployment topology, scaling limits, and connection budgets stay here.

Status as of 2026-08-11: the hosted API/worker split, PostgreSQL event wake-up,
Cloud Tasks dispatch, Session leases, and Supavisor connection-mode split are
deployed. The API and private worker both scale from zero. The checked-in
manifests are the source of truth. The W1 isolation matrix and W2 encryption-key
rotation remain launch gates tracked in `PLAN-pre-launch-hardening.md` and
`private-docs/pre-launch-checklist.md`; they do not change this topology.

## Topology

```text
              Cloudflare API router (api.vma.votrixai.com)
                                  |
             +--------- public API service ---------------------+
             | role=api · minScale 0 / maxScale 2               |
             | containerConcurrency 80 · no inline turns        |
             | FastAPI public/hosted routes · durable SSE poll  |
             | + pg_notify wake-up · governance · task dispatch |
             +--------------------+------------------------------+
                                  |
                 Supabase Postgres — source of truth
                 | sessions · append-only events
                 | LangGraph checkpoints · vault · usage
                 |
                 | transaction pooler :6543
                 |   runtime CRUD, event publish,
                 |   checkpoint pool (prepare_threshold=None)
                 | session mode :5432
                 |   event LISTEN
                 | direct/session migration DSN
                                  |
   committed user event batch -> named Cloud Task
                                  |
               OIDC POST /internal/sessions/{session_id}/process
                                  |
             +--------- private worker service -----------------+
             | role=worker · minScale 0 / maxScale 4            |
             | containerConcurrency 20                          |
             | Cloud Tasks push · Session lease expiry          |
             | Session lease + lock_version write fencing       |
             | append-only event commits                        |
             | DeepAgents/LangGraph · E2B sandbox 1:1/session   |
             | event append -> pg_notify -> API listeners       |
             +--------------------------------------------------+
```

Staging mirrors the same split and scale-to-zero envelope with separate
Cloud Run services, queue, secrets, and database. Local development retains the
`combined` compatibility role and does not define the production topology.

## Load-bearing invariants

Violating any of these is an incident.

1. **Postgres holds durable application state.** Sessions, events, and graph
   checkpoints survive process loss. The accepted event batch is also carried
   in the Cloud Task payload, so task creation and delivery must remain healthy
   for a newly accepted hosted turn to start.
2. **Session claim and generation fencing apply to writes.** The API claims an
   idle Session atomically. A release or interrupt advances `lock_version`, and
   a superseded worker cannot append another Agent event.
3. **A lost worker is bounded by the Session lease.** The next message can
   reclaim an expired lease. The optional standalone sweep only shortens stale
   UI state when an operator runs it.
4. **The worker endpoint stays private.** Cloud Tasks calls it with a runtime
   service-account OIDC token; public traffic must not bypass the Session gate.
5. **Session-scoped PostgreSQL features never use the transaction pooler.**
   LISTEN/NOTIFY subscriptions use the dedicated session DSN. Runtime and
   checkpoint traffic use transaction mode with server-side prepared statements
   disabled.
6. **Event wake-up is best-effort.** `pg_notify` reduces stream polling latency,
   but clients reconcile against durable events and no correctness path depends
   on a notification arriving.
7. **Cloud Tasks is the hosted execution path.** Scale-to-zero workers wake on
   task delivery. A warm worker or separately run sweep does not recreate a task
   whose creation failed.

## Scaling model

- The API plane is stateless and every instance may serve any request or SSE
  stream. Long-held SSE streams consume request concurrency.
- Worker turns are in-flight private HTTP requests, so Cloud Run scales on
  actual execution demand and drains normal scale-in. Session leases bound the
  stale state left by infrastructure interruption.
- Production and staging have no guaranteed warm turn capacity. Each can scale
  to 80 concurrent turn requests: `4 max worker instances × 20 requests per
  instance`.
- Worker `maxScale`, `containerConcurrency`, and Cloud Tasks
  `maxConcurrentDispatches` form three independent backpressure layers. Change
  them only from the measured PostgreSQL, E2B, provider, and spend budgets in
  `private-docs/scaling-runbook.md`.
- Production currently permits at most two API and four worker instances. A
  larger value requires updating the manifests, their test pins, the runbook's
  connection arithmetic, and the staging load evidence together.

## API surfaces: one implementation, four contract tiers

The FastAPI codebase exposes surfaces with different audiences,
authenticators, visibility, and compatibility disciplines. A shared process or
repository does not imply shared authorization.

| Tier | Paths | Audience | Authentication | Visibility and contract |
|---|---|---|---|---|
| Public product API | `/v1/...` on the GA allowlist in `app/public_surface.py` | SDK and API integrations | Organization API key | Filtered OpenAPI and Fumadocs; public fields, status codes, errors, IDs, and event shapes are compatibility surfaces pinned by tests |
| First-party builder | `/v1/me/organizations` today; future first-party routes stay below `/v1/me/...` | VMA builder browser | Supabase user JWT | Excluded from OpenAPI; may evolve with `vma-developer-app` |
| Hosted operator | `/internal/organizations/...` | VMA operators | Supabase superadmin JWT through `require_super_admin` | Private SOPs only; never an SDK surface |
| Infrastructure M2M | `/internal/sessions/.../process` on the private worker service | Cloud Tasks | Cloud Run IAM/OIDC plus Session generation fencing | No public schema; changes with deployment infrastructure |

The edge also admits exact utility paths `/`, `/openapi.json`, `/health`, and
`/health/...`. The complete hostname-to-path table, permanent naming tree,
certificate behavior, and coordinated cutover live in
`private-docs/domains.md`.

`api.vma.votrixai.com` is the only production hostname SDK users should see.
Under the permanent domain contract, its Worker admits only `/`,
`/openapi.json`, `/health[/...]`, and `/v1[/...]`, so `/internal/...` never
reaches the API origin through that door.
`vma.votrixai.com` is the builder frontend. The production Cloud Run `run.app`
URL is the official operator door behind superadmin JWT unless the optional
admin-host/origin-cloaking bundle in `private-docs/domains.md` is adopted.
Hostnames remain routing; the authentication tiers above remain enforcement.
`docs.vma.votrixai.com` is served as a Cloudflare Worker Static Assets site
from the checked-in `website/wrangler.jsonc` deployment contract.

Rules:

- Every new non-public endpoint goes under `/internal/...` or is explicitly
  excluded from OpenAPI as a first-party route.
- Authentication tiers never cross: an Organization API key does not
  authenticate `/internal/...` or `/v1/me/...`; a user JWT does not
  authenticate Organization API resources under `/v1/...`; superadmin JWTs do
  not replace worker IAM/OIDC.
- SDKs are generated and validated only against the filtered public OpenAPI.
- `/internal/...` is not versioned and is never documented as a public API.
- The public Worker's broad `/v1/...` edge allowlist does not override the
  narrower application GA allowlist or any route authentication.

## Pointers

- Domain routing and cutover: `private-docs/domains.md`.
- Capacity, connections, and operational fallback:
  `private-docs/scaling-runbook.md`.
- Launch gates: `private-docs/pre-launch-checklist.md`.
- Current implementation status and execution gaps: `docs/rewrite-status.md`.
- Contract-level public topology: `docs/votrix-core-architecture.md`.
- Deferred evolution: `TODO.md`.
