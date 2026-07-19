# VMA First-Release Architecture

Internal only. Do not copy this document into the public documentation tree —
`docs/votrix-core-architecture.md` is the public, contract-level view;
deployment topology, scaling limits, and connection budgets stay here.

Status: this describes the **first-release architecture** — what ships when
`PLAN-horizontal-scaling.md` (P1/P2/P2.5), `PLAN-p3-autoscale.md` (Stage A/B),
`PLAN-amendment-A1-connection-modes.md`, and `PLAN-pre-launch-hardening.md`
(W1/W2) have all landed. The system as deployed before that is a single
combined Cloud Run instance (`maxScale=1`, embedded worker coroutines); the
PLAN documents are the migration path. After launch, this file is the living
architecture reference; the PLANs become history.

## Topology

```text
                        Cloudflare API router
                                 |
                       Cloud Run load balancer
                                 |
        +----------- API service (stateless) --------------------+
        |  role=api · minScale 1 / maxScale 3 (request-driven)   |
        |  FastAPI /v1 routes · SSE streams (durable-event poll  |
        |  + preview frame replay) · rate limits & quotas as     |
        |  Postgres counters · dispatches work, never runs it    |
        +--------------------------+------------------------------+
                                   |
                 Supabase Postgres — the single source of truth
                 |  sessions · append-only events · work queue  |
                 |  LangGraph checkpoints · vault · usage       |
                 |                                              |
                 |  transaction pooler :6543                    |
                 |    runtime CRUD, work queue, events,         |
                 |    checkpoint pool (prepare_threshold=None)  |
                 |  session mode :5432                          |
                 |    preview LISTEN, janitor advisory lock,    |
                 |    Alembic migration Job                     |
                                   |
   enqueue commits work item ──► Cloud Tasks queue (one named task per
                                 turn — wake-up signal and scale driver
                                 ONLY; deleting the queue loses no work)
                                   |
                   OIDC  POST /internal/work/{work_id}/execute
                                   |
        +----------- Worker service (turn execution) ------------+
        |  role=worker · autoscales on in-flight turns           |
        |  (containerConcurrency = per-instance turn bound)      |
        |                                                        |
        |  lease (lease_id + generation + heartbeat)             |
        |    → attempt cap (counts executions, not leases)       |
        |    → turn journal (crash-safe finalize)                |
        |    → DeepAgents / LangGraph graph, one turn per run    |
        |    → deterministic event ids, idempotent append        |
        |  shared turn limiter across push handler + poller      |
        |  slow poller = permanent reconciler (expired leases,   |
        |    missed dispatches; the recovery path)               |
        |  E2B sandbox 1:1 per Session (sealed at create,        |
        |    explicit reconnect each turn) · model providers     |
        |  preview frames → coalesced pg_notify → API listeners  |
        +--------------------------------------------------------+
```

## Load-bearing invariants (violating any of these is an incident)

1. **Postgres is the only source of truth.** Cloud Tasks, preview frames, and
   in-process state are all reconstructible or losable. The reconciler poller
   alone must be able to run the whole system.
2. **Lease fencing everywhere.** `(worker_id, lease_id, generation)` gates
   execution, every event persist, usage recording, and finalization; a
   superseded worker cannot write.
3. **Lock ordering: session row first, then work row.** Never hold a work-row
   `FOR UPDATE` while acquiring a session lock (deadlock inversion).
4. **Bounded at-least-once turn execution.** A completed graph run is never
   re-invoked (turn journal); replays dedupe events where identity is
   derivable (deterministic ids); replay count is capped
   (`VMA_WORK_MAX_ATTEMPTS`, execution-counted). External tool side effects
   are NOT exactly-once — high-risk custom tools must be idempotent on the
   caller's side (public-docs contract note at launch).
5. **Session-scoped Postgres features never cross the transaction pooler**:
   LISTEN/NOTIFY subscriptions and session advisory locks use the dedicated
   `:5432` DSN; prepared statements stay off pooled paths.
6. **Preview is best-effort by contract.** Dropped frames are reconciled by
   durable events; nothing may ever depend on a preview frame arriving.

## Scaling model

- **API plane**: classic stateless fleet behind the platform LB; scales on
  request load; every instance can serve any request including SSE. Ceiling
  per instance ≈ `containerConcurrency` (long-held SSE streams count).
- **Worker plane**: turns are in-flight HTTP requests, so Cloud Run scales on
  them directly and scale-in drains rather than kills executing turns
  (residual interruption: infra SIGTERM/OOM — bounded by invariant 4).
- **Three backpressure layers**, all derived from the connection/E2B/spend
  budgets in `scaling-runbook.md`, never from intuition: worker `maxScale`,
  per-instance turn limiter, queue `maxConcurrentDispatches`.
- Overflow degrades to queue wait — never to failures.

## API surfaces (one app, four tiers)

One FastAPI implementation exposes four surfaces with different audiences,
authenticators, and — critically — different contract disciplines. The public
tier is frozen; everything else may change freely.

| Tier | Paths | Audience | Auth | Visibility | Contract |
|---|---|---|---|---|---|
| Public product API | `/v1/*` (GA allowlist in `app/public_surface.py`) | SDK/API integrators | Organization API key | fumadocs + filtered OpenAPI export | Frozen: fields, codes, event shapes never break; pinned by `tests/test_public_ga_surface.py` |
| First-party app | `/v1/me/*` | VMA builder frontend | Supabase user JWT | `include_in_schema=False` | Changeable with the frontend |
| Hosted operator | `/internal/organizations/*` | Operators | Supabase superadmin JWT (`require_super_admin` router dependency) | private-docs SOPs only | Changeable; never versioned |
| Infrastructure M2M | `/internal/work/*` (P3, worker service only) | Cloud Tasks | Cloud Run IAM/OIDC; service is private | No schema at all | Changes with the deployment |

Domains: the naming plan, the hostname→path forwarding table, and the
admin-host/origin-cloaking three-together rule live in
`private-docs/domains.md`. Summary: `api.vma.votrixai.com` is the only
hostname SDK users ever see (its Worker rejects `/internal/*`);
`vma.votrixai.com` is the builder frontend; the Cloud Run `run.app` URL is the
official operator entry behind superadmin JWT. Hostnames are routing, never
the security boundary — the auth tiers above are the enforcement.

Rules:

- Every new non-public endpoint goes under `/internal/` (auto-exempt from the
  GA middleware, never exported) or carries `include_in_schema=False`.
- Auth tiers never cross: an Organization API key must not authenticate
  `/internal` or `/v1/me`; a human JWT must not authenticate `/v1`
  Organization resources. (Tier-crossing denial tests ride with the W1
  isolation matrix.)
- SDKs are generated/validated only against the filtered GA OpenAPI.
- No separate internal service, no gRPC, no versioning of `/internal`.

## Pointers

- Operations: `private-docs/scaling-runbook.md` (capacity, budgets, manual
  fallback mode), `private-docs/pre-launch-checklist.md` (launch gate).
- Contract-level public view: `docs/votrix-core-architecture.md`; update
  `docs/work-queue.md` at launch (it still describes the embedded
  single-process consumer).
- Deferred evolution: `TODO.md` (Redis/preview transport swap at much larger
  scale, RLS defense-in-depth, event retention implementation).
