# VMA First-Release Architecture

Internal only. Do not copy this document into the public documentation tree.
`docs/votrix-core-architecture.md` is the public, contract-level view;
deployment topology, scaling limits, and connection budgets stay here.

Status as of 2026-07-19: the hosted API/worker split, PostgreSQL preview broker,
Cloud Tasks hybrid dispatch, bounded replay, and Supavisor connection-mode
split are deployed. Production is not the old single-process service: both the
API and private worker currently run at `minScale=1 / maxScale=2`. The checked-in
manifests are the source of truth. The W1 isolation matrix and W2 encryption-key
rotation remain launch gates tracked in `PLAN-pre-launch-hardening.md` and
`private-docs/pre-launch-checklist.md`; they do not change this topology.

## Topology

```text
              Cloudflare API router (api.vma.votrixai.com)
                                  |
             +--------- public API service ---------------------+
             | role=api · minScale 1 / maxScale 2               |
             | containerConcurrency 40 · no embedded worker     |
             | FastAPI public/hosted routes · durable SSE poll  |
             | + preview replay · governance · work dispatch    |
             +--------------------+------------------------------+
                                  |
                 Supabase Postgres — source of truth
                 | sessions · append-only events · work queue
                 | LangGraph checkpoints · vault · usage
                 |
                 | transaction pooler :6543
                 |   runtime CRUD, queue, event/preview publish,
                 |   checkpoint pool (prepare_threshold=None)
                 | session mode :5432
                 |   preview LISTEN, janitor advisory lock
                 | direct/session migration DSN
                                  |
   committed work row --------> Cloud Tasks named wake-up task
                                  |
                   OIDC POST /internal/work/{work_id}/execute
                                  |
             +--------- private worker service -----------------+
             | role=worker · minScale 1 / maxScale 2            |
             | containerConcurrency 5 · turn limit 5            |
             | Cloud Tasks push + permanent slow reconciler     |
             | lease/generation fencing · bounded attempt cap   |
             | crash-safe turn journal · idempotent event append|
             | DeepAgents/LangGraph · E2B sandbox 1:1/session   |
             | preview coalescing -> pg_notify -> API listeners |
             +--------------------------------------------------+
```

Staging mirrors the same split and `1..2` scaling envelope with separate
Cloud Run services, queue, secrets, and database. Local development retains the
`combined` compatibility role and does not define the production topology.

## Load-bearing invariants

Violating any of these is an incident.

1. **Postgres is the only source of truth.** Cloud Tasks, preview frames, and
   process-local state are reconstructible or losable. The reconciler alone
   must be able to recover and run durable work.
2. **Lease fencing applies to execution and writes.**
   `(worker_id, lease_id, generation)` gates execution, event persistence,
   usage recording, journal writes, and finalization. A superseded worker
   cannot commit.
3. **Lock session rows before work rows.** Never hold a work-row `FOR UPDATE`
   lock while acquiring a Session lock; that inversion can deadlock.
4. **Turn execution is bounded at-least-once.** A journaled completed graph run
   is finalized without invoking the model again. Replays deduplicate events
   where deterministic identity exists and the execution attempt count is
   capped by `VMA_WORK_MAX_ATTEMPTS`. External tool side effects are not
   exactly-once; high-risk tools must accept caller-provided idempotency.
5. **Session-scoped PostgreSQL features never use the transaction pooler.**
   LISTEN/NOTIFY subscriptions and session advisory locks use the dedicated
   session DSN. Runtime and checkpoint traffic use transaction mode with
   server-side prepared statements disabled.
6. **Preview is best-effort.** Coalesced `pg_notify` frames preserve hosted
   typewriter feedback, but clients reconcile against durable events and no
   correctness path depends on a preview arriving.
7. **Dispatch is a wake-up optimization.** A failed or deleted Cloud Tasks
   queue must not lose committed work. The PostgreSQL reconciler remains the
   recovery path permanently.

## Scaling model

- The API plane is stateless and every instance may serve any request or SSE
  stream. Long-held SSE streams consume request concurrency.
- Worker turns are in-flight private HTTP requests, so Cloud Run scales on
  actual execution demand and drains normal scale-in. Lease/journal recovery
  covers infrastructure interruption.
- Production currently provides five warm concurrent turns and can scale to
  ten: `2 max worker instances × 5 turns per instance`. Accepted overflow
  waits in the durable queue.
- Worker `maxScale`, the per-instance turn limiter, and Cloud Tasks
  `maxConcurrentDispatches` form three independent backpressure layers. Change
  them only from the measured PostgreSQL, E2B, provider, and spend budgets in
  `private-docs/scaling-runbook.md`.
- Production currently permits at most two API and two worker instances. A
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
| Infrastructure M2M | `/internal/work/...` on the private worker service | Cloud Tasks and the reconciler | Cloud Run IAM/OIDC plus work lease fencing | No public schema; changes with deployment infrastructure |

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
