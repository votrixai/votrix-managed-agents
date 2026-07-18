# Worker Capacity and Horizontal Scaling Runbook

Internal only. Do not copy this runbook into the public documentation tree.

This runbook describes how to size and scale VMA turn-execution capacity on
Cloud Run. It assumes the P1 hardening, P2 API/worker split, and mandatory P2.5
cross-instance preview transport from `PLAN-horizontal-scaling.md` have shipped.
Autoscaling (P3, Cloud Tasks) ships pre-launch as part of the first release
(spec: `PLAN-p3-autoscale.md`).
The checked-in hosted profile uses `VMA_WORK_DISPATCH_MODE=hybrid`: Cloud Tasks
push drives Cloud Run autoscaling, while the permanent PostgreSQL reconciler is
the correctness fallback. The connection-budget section below governs every
`maxScale` change in both hybrid and poll modes.

## First-release topology after P3

| Service | Role | Scaling | Manifest |
|---|---|---|---|
| `votrix-managed-agents` | Public API + SSE | `minScale=1 / maxScale=2`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Private OIDC turn execution + slow PostgreSQL reconciler + E2B janitor | Cloud Tasks request-driven, `minScale=1 / maxScale=2` | `service.worker.production.yaml` |
| `votrix-managed-agents-staging` | Staging API + SSE | `minScale=1 / maxScale=2`, request-driven | `service.staging.yaml` |
| `votrix-managed-agents-staging-worker` | Staging turn execution | Cloud Tasks request-driven, `minScale=1 / maxScale=2` | `service.worker.staging.yaml` |

Project `votrixai-480422`, region `us-central1` (see `scripts/gcloud/config.sh`).

## Capacity model

```
concurrent turn capacity = worker instances × VMA_WORKER_TURN_LIMIT
```

- `VMA_WORKER_TURN_LIMIT` and Cloud Run `containerConcurrency` are both pinned
  to `5` in the worker manifests.
- Production starts with 1 warm instance → **5 guaranteed concurrent turns**
  and may autoscale to 2 instances → **10 concurrent turns** under the
  conservative measured connection budget below.
- Staging starts at 5 and may autoscale to 10 concurrent turns.
- Turns are IO-bound coordinators (model streaming, E2B, Postgres); heavy
  compute happens inside E2B sandboxes, not on the worker. A 1 vCPU / 4 GiB
  instance sustains 5 concurrent turns comfortably.
- Admitted overflow waits in the Postgres queue (`environment_work`, status
  `queued`) until a slot frees. The hosted Organization active-work limit is 20,
  so requests beyond that queued/running cap can be rejected rather than queued.

## How worker autoscaling operates

Deploys in this repo are declarative (`gcloud run services replace <manifest>`),
so the manifest remains the source of truth. **A manual
`gcloud run services update` is reverted by the next deploy.**

Named Cloud Tasks target the private worker URL with an OIDC token. Each task is
an in-flight HTTP request for the duration of the turn, so Cloud Run observes
demand and scales worker instances. The process-wide limiter prevents the push
handler and reconciler together from exceeding five turns per instance.

`VMA_WORKER_CONCURRENCY=1` and
`VMA_WORKER_POLL_INTERVAL_SECONDS=20` intentionally describe only the slow
reconciler. They do not cap push capacity. If task creation or delivery fails,
the reconciler eventually leases the durable PostgreSQL work row; Cloud Tasks
never carries correctness.

### Standard persistent capacity change

1. Recalculate the connection ceiling, E2B concurrency/quota, provider limits,
   and spend ceiling.
2. Edit the worker manifest's `maxScale` and, only when guaranteed warm capacity
   is required, `minScale`. Keep `VMA_WORKER_TURN_LIMIT` equal to
   `containerConcurrency`.
3. Update `tests/test_cloud_run_config.py` and this runbook in the same change.
4. Ship through the normal migration-gated pipeline and rerun the staging load
   scenarios before production promotion.

### Emergency path (immediate, temporary)

```bash
gcloud run services update votrix-managed-agents-worker \
  --project=votrixai-480422 --region=us-central1 \
  --min-instances=2 --max-instances=2
```

Takes effect in seconds. **Backfill the manifest + test pins immediately**, or
the next deploy silently reverts the bounds. Do not raise the per-instance turn
limit independently of `containerConcurrency`; prefer adding instances because
that also adds memory headroom and blast-radius isolation.

## Connection budget (check BEFORE scaling up)

Two different Supabase limits matter, and they scale with the compute tier:

- **Postgres backend (direct) connections** are scarce. Supavisor session mode
  pins one backend per client for the client's lifetime.
- **Supavisor pooler client connections** are cheaper. Transaction mode
  multiplexes many clients over a smaller shared backend pool.

Amendment A1 (`PLAN-amendment-A1-connection-modes.md`) therefore sends runtime
and checkpoint traffic through the transaction pooler (`:6543`). It reserves
session mode (`:5432`) for the preview LISTEN connection, the janitor advisory
lock, and migrations.

Per-instance connections after A1:

| Component | Mode | API instance | Worker instance |
|---|---|---|---|
| SQLAlchemy pool (`VMA_DB_POOL_SIZE` + `VMA_DB_MAX_OVERFLOW`) | transaction | 4 + 2 | 4 + 1 |
| LangGraph checkpoint pool (`VMA_CHECKPOINT_POOL_MAX_SIZE`) | transaction | 0 | 3 |
| Preview broker LISTEN connection (P2.5) | **session** | 1 | 0 |
| Janitor advisory-lock contender (P1.3) | **session** | 0 | up to +1 transient per active instance |
| **Steady Supavisor clients (excluding janitor)** | | **7** | **8** |
| **Steady backend-pinned clients** | | **1** | **0** |

```
steady Supavisor clients ≈ 7 × (active API instances) + 8 × (active worker instances)
janitor-pass client burst ≤ steady clients + active worker instances
steady backend pins ≈ active API instances
janitor-pass backend-pin burst ≤ active API instances + active worker instances
```

Production steady-state manifest peak: 2 API + 2 workers =
`2×7 + 2×8 = 30` Supavisor clients and two dedicated backend pins. If both
workers contend in the same janitor pass, the brief peak is 32 clients
and four backend pins. Only one contender becomes the advisory-lock leader, but
every contender must first pin its own session-mode connection to try the lock.
During a revision overlap, count active old- and new-revision instances in both
terms; do not budget only for one leader or one revision. These figures exclude
migrations, the transaction pooler's own backend pool, and operator sessions.
Cloud Tasks starts at 25 concurrent dispatches and rises only when this complete
connection budget permits it. The staging load test must watch SQLAlchemy
slow-checkout warnings: worker 4+1 against five concurrent turns is
deliberately tight and is the first knob to raise on measured contention.

### Staging SQLAlchemy checkout-latency gate

The main SQLAlchemy engine uses `ObservedAsyncAdaptedQueuePool`. It emits no
per-checkout telemetry. It emits one structured warning only when an acquisition
takes at least **250 ms**, including queue wait, connection establishment, and
checkout work such as pre-ping:

- `event="database_pool_checkout_slow"`
- `checkout_latency_ms`, `threshold_ms`, and `outcome` (`acquired` or `error`)
- `pool_size`, `checked_out`, and `overflow_connections`
- `error_type` when acquisition failed

The interval is intentionally end-to-end checkout latency, not a claim of pure
queue duration. It measures the delay experienced by the caller while keeping
the normal fast path free of logging and external metrics dependencies.

For each staging load scenario, warm the services for 60 seconds, record that
post-warm-up timestamp, and run this exact Logs Explorer filter with the
timestamp substituted:

```text
resource.type="cloud_run_revision"
(resource.labels.service_name="votrix-managed-agents-staging" OR
 resource.labels.service_name="votrix-managed-agents-staging-worker")
jsonPayload.event="database_pool_checkout_slow"
severity>=WARNING
timestamp>="2026-01-01T00:00:00Z"
```

The CLI equivalent uses the same substituted post-warm-up timestamp:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND (resource.labels.service_name="votrix-managed-agents-staging" OR resource.labels.service_name="votrix-managed-agents-staging-worker") AND jsonPayload.event="database_pool_checkout_slow" AND severity>=WARNING AND timestamp>="2026-01-01T00:00:00Z"' \
  --project=votrixai-480422 --order=asc --limit=1000 \
  --format='table(timestamp,resource.labels.service_name,jsonPayload.checkout_latency_ms,jsonPayload.outcome,jsonPayload.checked_out,jsonPayload.pool_size,jsonPayload.overflow_connections,jsonPayload.error_type)'
```

The post-warm-up staging gate passes only when this query returns no rows. Any
`outcome="error"` is a failure; any `outcome="acquired"` row means the caller
waited at least 250 ms and requires investigation. A connection-establishment
warning confined to the excluded warm-up minute should still be recorded, but
does not fail the post-warm-up gate. Recurring `acquired` warnings mean the 4+1
worker pool is contended and must be raised or the turn limit reduced before
production.

### Production maxScale=2 release record

**Status: MEASURED — conservative maxScale=2 approved on 2026-07-18 by Votrix engineering.**

The production database measured `max_connections=60`, three reserved slots,
`shared_buffers=256MB`, and 20 total backend connections immediately before the
rollout. The connection class is therefore Nano/Micro-equivalent; both classes
have the same published limits: 60 Postgres connections and 200 Supavisor
clients. The observed backend total included seven application-role Supavisor
connections and two Supavisor auth-query connections.

The current fine-grained Supabase CLI token can list the project but receives
HTTP 403 for the billing-addon and pooler-configuration endpoints, so it cannot
independently attest the configured backend pool-size ceiling. The initial
production bound is deliberately held to the staging-proven two API and two
worker instances instead of assuming the earlier eight-worker target.

At the checked-in bounds, 30 steady and 32 janitor-burst Supavisor clients are
well below the 200-client ceiling. Conservatively adding four dedicated pins
and one migration connection to the measured 20-backend baseline yields 25,
below 60% of the direct/backend limit (36). Staging previously completed a
20-Session real Cloud Tasks/E2B/OpenRouter burst across two worker instances
without duplicate model execution or database pool errors.

The production manual deploy script, Cloud Build deployment step, and GCP
preflight continue to fail closed whenever the runbook is returned to its
unmeasured-status sentinel. Any increase above `maxScale=2` requires all of the
following in the same release:

1. Read and record the exact Supabase compute variant and configured pool size
   through a token with `infra_add_ons_read` and
   `database_pooling_config_read`, or directly from Database Settings.
2. Recalculate steady, janitor-burst, migration, operator, and revision-overlap
   headroom. Keep dedicated pins plus the transaction pool's backend footprint
   below 60% of the direct/backend limit.
3. Run the 3× proposed fleet-capacity burst and every scale-out, scale-in,
   crash-recovery, and broken-dispatch scenario from `PLAN-p3-autoscale.md`
   without pool timeouts or duplicate execution.
4. Update both manifests, their static tests, and this measured record. Upgrade
   the database plan instead when either connection budget does not fit.

## Signals that say "change the bounds"

Raise `maxScale` when either holds for more than a day and every budget permits:

- Work items regularly sit in `queued` for tens of seconds while all slots are
  busy (queue wait = `started_at - queued_at` in `environment_work` rows).
- Worker instance CPU or memory sustained above ~70% at normal load.

Lower `maxScale` when the fleet never approaches its bound for a week and queue
waits are zero. Change `minScale` only for cold-start or guaranteed-capacity
requirements; Cloud Tasks already supplies the scale signal in hybrid mode.

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
  `VMA_WORK_MAX_ATTEMPTS=3`; only admission to graph execution consumes an
  attempt. Duplicate, busy, deferred, or superseded dispatches do not consume
  the cap. Stage A journals and deterministic events prevent a completed graph
  from invoking the model again after a control-plane crash.

## Cloud Tasks incident fallback

If task creation or delivery is failing:

1. Do not delete or rewrite PostgreSQL work rows. They are the durable ledger.
2. Confirm workers remain at `minScale=1`; the 20-second reconciler continues
   processing queued and expired work without Cloud Tasks.
3. Inspect `scripts/gcloud/status.sh`, then rerun
   `scripts/gcloud/8-setup-cloud-tasks.sh <environment>` to repair queue policy
   and IAM drift.
4. If hybrid dispatch itself must be disabled, deploy both API and worker with
   `VMA_WORK_DISPATCH_MODE=poll`. Restore the normal hybrid manifests only after
   task delivery is healthy.

Cloud Tasks retry `maxAttempts=8` is an infrastructure delivery bound, not the
model-execution attempt counter. The application maps terminal/busy outcomes to
HTTP 200 and only transient outcomes to retryable 5xx responses.

## Combined role is local/self-hosted only

The `combined` role remains the compatibility default for local development and
simple self-hosting. It is not the maintained Cloud Run production topology.
Do not turn `service.production.yaml` back into a combined service or use API
instance scaling to add turn capacity; scale the dedicated worker manifest as
described above.
