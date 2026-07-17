# Worker Capacity and Horizontal Scaling Runbook

Internal only. Do not copy this runbook into the public documentation tree.

This runbook describes how to size and scale VMA turn-execution capacity on
Cloud Run. It assumes the P1 hardening, P2 API/worker split, and mandatory P2.5
cross-instance preview transport from `PLAN-horizontal-scaling.md` have shipped.
Autoscaling (P3, Cloud Tasks) is not built yet — it is a committed follow-up
roadmap sequenced after P2.5 plus an idempotency stage; scope lives in `TODO.md`
under "Roadmap — P3 auto scale: Cloud Tasks per-turn dispatch". This runbook
governs operations until it lands.

## Topology after P2 + P2.5

| Service | Role | Scaling | Manifest |
|---|---|---|---|
| `votrix-managed-agents` | Public API + SSE | `minScale=1 / maxScale=3`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Turn execution (embedded Postgres-queue pollers + E2B janitor) | Warm/manual bounded fleet: `minScale=2 / maxScale=3` | `service.worker.production.yaml` |
| `votrix-managed-agents-staging` | Staging API + SSE | `minScale=1 / maxScale=2`, request-driven | `service.staging.yaml` |
| `votrix-managed-agents-staging-worker` | Staging turn execution | `minScale=1 / maxScale=1` | `service.worker.staging.yaml` |

Project `votrixai-480422`, region `us-central1` (see `scripts/gcloud/config.sh`).

## Capacity model

```
concurrent turn capacity = worker instances × VMA_WORKER_CONCURRENCY
```

- `VMA_WORKER_CONCURRENCY` is pinned to `5` in the worker manifests.
- Production starts with 2 warm instances → **10 guaranteed concurrent turns**.
  An intentional 3-instance fleet provides **15 concurrent turns**.
- Turns are IO-bound coordinators (model streaming, E2B, Postgres); heavy
  compute happens inside E2B sandboxes, not on the worker. A 1 vCPU / 4 GiB
  instance sustains 5 concurrent turns comfortably.
- Admitted overflow waits in the Postgres queue (`environment_work`, status
  `queued`) until a slot frees. The hosted Organization active-work limit is 20,
  so requests beyond that queued/running cap can be rejected rather than queued.

## How to scale the worker fleet

Deploys in this repo are declarative (`gcloud run services replace <manifest>`),
so the manifest is the source of truth. **A manual `gcloud run services update`
is reverted by the next deploy.**

### Standard path (persistent)

1. Edit `service.worker.production.yaml`. Raise `minScale` to change guaranteed
   warm capacity and keep `maxScale >= minScale`. Set both to the same value when
   an exactly fixed fleet is operationally required. The checked-in default is
   deliberately bounded at `minScale=2 / maxScale=3`; Postgres queue depth does
   not drive the service to its maximum.
2. Update the pinned values in `tests/test_cloud_run_config.py` (the topology
   is asserted there; the suite fails on drift by design).
3. Ship through the normal pipeline (merge → Cloud Build → migrate job →
   `services replace`).

### Emergency path (immediate, temporary)

```bash
gcloud run services update votrix-managed-agents-worker \
  --project=votrixai-480422 --region=us-central1 \
  --min-instances=4 --max-instances=4
```

Takes effect in seconds. **Backfill the manifest + test pins immediately**, or
the next deploy silently reverts the fleet size.

### Second knob: per-instance concurrency

`VMA_WORKER_CONCURRENCY` (worker manifest env) can go from 5 to ~8 before
instance sizing becomes the constraint. Prefer adding instances: it also adds
memory headroom and blast-radius isolation. If raising it, watch instance
memory (4 GiB) under peak concurrent turns.

## Connection budget (check BEFORE scaling up)

Every instance holds Postgres connections against the Supabase Supavisor
session-mode pooler (port 5432), which has a hard per-plan client-connection
cap. Per-instance worst case:

| Component | API instance | Worker instance |
|---|---|---|
| SQLAlchemy pool (`VMA_DB_POOL_SIZE` + `VMA_DB_MAX_OVERFLOW`) | 10 + 5 | 5 + 2 |
| LangGraph checkpoint pool (`VMA_CHECKPOINT_POOL_MAX_SIZE`) | 0 (API never runs turns) | 5 |
| Preview broker LISTEN connection (P2.5, API/combined only) | 1 | 0 (worker runs no listener) |
| **Total ceiling** | **16** | **12** |

```
total ≈ 16 × (API instances) + 12 × (worker instances) + migrations/ops slack
```

Example: 3 API + 3 worker ≈ 84 + slack. Verify the target Supabase plan's
connection limit covers the total before raising either fleet; raise the plan
first if not.

## Signals that say "scale" (and the one that says "build P3")

Scale up (add 1 instance) when either holds for more than a day:
- Work items regularly sit in `queued` for tens of seconds while all slots are
  busy (queue wait = `started_at - queued_at` in `environment_work` rows).
- Worker instance CPU or memory sustained above ~70% at normal load.

Scale down when the fleet is mostly idle for a week and queue waits are zero.

P3 auto scale is a committed follow-up (see the `TODO.md` roadmap), sequenced
after P2 plus its Stage A idempotency gate — budget ~1–1.5 weeks for both
stages. The demand signals above raise its priority; they are no longer gates.
Until it lands, manual fleet scaling per this runbook is the operating model.

## What scaling does NOT change

- **Turn speed.** More instances add parallel capacity, not faster turns.
- **Token preview.** Preserved across the split by the P2.5 `pg_notify` preview
  broker (`VMA_PREVIEW_BROKER=pg_notify`): workers publish preview frames via
  Postgres `NOTIFY`, and API instances `LISTEN` and forward them to their local
  SSE subscribers. This preserves `event_deltas` typewriter delivery when the
  SSE client and executing worker land on different instances. Delivery remains
  best-effort and non-replayable; durable Session events reconcile any missed
  frame. The real Supabase broker smoke used separate worker-role and API-role
  OS processes and delivered three frames, including two coalesced deltas with
  complete text. It verifies the cross-process PostgreSQL path but is not a
  substitute for a smoke through the complete public SSE endpoint.
  This transport is not a scaling knob — adding worker instances neither helps
  nor harms it. `VMA_PREVIEW_BROKER=process_local` remains the local-development
  default; it must not replace `pg_notify` in the split hosted topology.
- **Correctness.** Work-queue leases (`lease_id + generation` + heartbeat) and
  the per-Session execution lease fence concurrent instances; a superseded
  worker cannot persist events or finalize work. Retries are capped by
  `VMA_WORK_MAX_ATTEMPTS=3` — the 4th lease of a failing work item errors the
  work and terminates the Session with a `session.error` event.

## Combined role is local/self-hosted only

The `combined` role remains the compatibility default for local development and
simple self-hosting. It is not the maintained Cloud Run production topology.
Do not turn `service.production.yaml` back into a combined service or use API
instance scaling to add turn capacity; scale the dedicated worker manifest as
described above.
