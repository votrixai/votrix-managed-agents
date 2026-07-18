# Worker Capacity and Horizontal Scaling Runbook

Internal only. Do not copy this runbook into the public documentation tree.

This runbook describes how to size and scale VMA turn-execution capacity on
Cloud Run. It assumes the P1 hardening and P2 API/worker split from
`PLAN-horizontal-scaling.md` have shipped; a "before P2" section at the end
covers the interim single-service topology. Autoscaling (P3, Cloud Tasks)
ships pre-launch as part of the first release (spec: `PLAN-p3-autoscale.md`).
This runbook's fixed-fleet procedures remain valid as the
`VMA_WORK_DISPATCH_MODE=poll` fallback and for reasoning about capacity; the
connection-budget section below governs `maxScale` derivation in both modes.

## Topology after P2

| Service | Role | Scaling | Manifest |
|---|---|---|---|
| `votrix-managed-agents` | Public API + SSE | `minScale=1 / maxScale=3`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Turn execution (embedded Postgres-queue pollers + E2B janitor) | **Fixed fleet**: `minScale = maxScale = N` | `service.worker.production.yaml` |
| `votrix-managed-agents-staging` / `-staging-worker` | Staging pair | 1 instance each | `service.staging.yaml`, `service.worker.staging.yaml` |

Project `votrixai-480422`, region `us-central1` (see `scripts/gcloud/config.sh`).

## Capacity model

```
concurrent turn capacity = worker instances × VMA_WORKER_CONCURRENCY
```

- `VMA_WORKER_CONCURRENCY` is pinned to `5` in the worker manifests.
- Production default fleet: 2–3 instances → **10–15 concurrent turns**.
- Turns are IO-bound coordinators (model streaming, E2B, Postgres); heavy
  compute happens inside E2B sandboxes, not on the worker. A 1 vCPU / 4 GiB
  instance sustains 5 concurrent turns comfortably.
- Overflow behavior is graceful: excess turns wait in the Postgres queue
  (`environment_work`, status `queued`) until a slot frees. Nothing fails;
  turn start is delayed.

## How to scale the worker fleet

Deploys in this repo are declarative (`gcloud run services replace <manifest>`),
so the manifest is the source of truth. **A manual `gcloud run services update`
is reverted by the next deploy.**

### Standard path (persistent)

1. Edit `service.worker.production.yaml`: set
   `autoscaling.knative.dev/minScale` and `maxScale` to the new instance count
   (keep them equal — this is a fixed fleet).
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

Two DIFFERENT Supabase limits matter, and they scale with the compute tier:

- **Postgres backend (direct) connections** — the scarce one. Micro 60,
  Small 90, Medium 120, Large 160, XL 240.
- **Supavisor pooler client connections** — the cheap one. Micro 200 … XL 1,000.

Supavisor **session mode pins one backend per client** for the client's
lifetime; transaction mode multiplexes many clients over few backends. That is
why Amendment A1 (`PLAN-horizontal-scaling.md`) routes runtime traffic and
checkpoints through the transaction pooler (`:6543`) and reserves session mode
(`:5432`) for LISTEN/NOTIFY, the janitor advisory lock, and migrations.

Per-instance connections after A1:

| Component | Mode | API instance | Worker instance |
|---|---|---|---|
| SQLAlchemy pool (`VMA_DB_POOL_SIZE` + `VMA_DB_MAX_OVERFLOW`) | transaction | 4 + 2 | 4 + 1 |
| LangGraph checkpoint pool (`VMA_CHECKPOINT_POOL_MAX_SIZE`) | transaction | 0 | 3 |
| Preview broker LISTEN (P2.5) | **session** | 1 | 0 |
| Janitor advisory lock (P1.3) | **session** | 0 | ≤1 transient |
| **Pooler clients** | | **7** | **8** |
| **Backend-pinned** | | **1** | **≤1 transient** |

```
backend-pinned  ≈ 1 × API instances (+1 during a janitor tick) + migration slack
pooler clients  ≈ 7 × API instances + 8 × worker instances
```

Example: 3 API + 4 workers ≈ 53 pooler clients but only ~4 pinned backends —
the transaction pooler multiplexes the rest over a handful of shared backends.

Rules: record the production compute tier here: ______ ; keep combined backend
usage (pinned + the pooler's own backend pool) under ~60% of the tier's direct
limit; derive worker `maxScale` from this budget, never by feel. The staging
load test must watch SQLAlchemy pool-wait metrics — worker 4+1 against 5
concurrent turns is deliberately tight and is the first knob to raise.

## Signals that say "scale" (and the one that says "build P3")

Scale up (add 1 instance) when either holds for more than a day:
- Work items regularly sit in `queued` for tens of seconds while all slots are
  busy (queue wait = `started_at - queued_at` in `environment_work` rows).
- Worker instance CPU or memory sustained above ~70% at normal load.

Scale down when the fleet is mostly idle for a week and queue waits are zero.

P3 auto scale ships with the first release (`PLAN-p3-autoscale.md`), so in
normal operation Cloud Run adds and removes worker instances automatically and
the signals above become capacity-planning hints (chiefly: when to raise
`maxScale`, which must always be re-derived from the connection budget above).
Manual fleet scaling per this runbook applies when running in the
`VMA_WORK_DISPATCH_MODE=poll` fallback mode.

## What scaling does NOT change

- **Turn speed.** More instances add parallel capacity, not faster turns.
- **Token preview.** Preserved across the split by the P2.5 `pg_notify` preview
  broker (`VMA_PREVIEW_BROKER=pg_notify`): workers publish preview frames via
  Postgres `NOTIFY`, API instances `LISTEN` and replay them to their local SSE
  subscribers, so `event_deltas` typewriter streaming works regardless of which
  instances the SSE client and the executing worker land on. This is a preview
  transport, not a scaling knob — adding worker instances neither helps nor
  harms it. If `VMA_PREVIEW_BROKER=process_local` (the pre-P2.5 default),
  hosted preview falls back to per-instance-only and clients see complete
  durable events via ~1s DB polling instead.
- **Correctness.** Work-queue leases (`lease_id + generation` + heartbeat) and
  the per-Session execution lease fence concurrent instances; a superseded
  worker cannot persist events or finalize work. Retries are capped by
  `VMA_WORK_MAX_ATTEMPTS=3` — the 4th lease of a failing work item errors the
  work and terminates the Session with a `session.error` event.

## Before P2 ships (interim, single combined service)

The combined service can be scaled to a small fixed fleet the same way
(`service.production.yaml` `minScale`/`maxScale`, plus
`tests/test_cloud_run_config.py` pins). Prerequisite: the P1 hardening items
(attempt cap, checkpoint pool reuse, janitor advisory lock) must be deployed
first. Two caveats specific to this interim mode: token previews become a
per-connection lottery (they only reach SSE clients that happen to share an
instance with the executing worker coroutine), and API traffic shares CPU with
turn execution, so keep the fleet ≤3 and finish P2 instead of growing it.
