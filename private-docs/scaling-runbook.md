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
| `votrix-managed-agents` | Public API + SSE | `minScale=1 / maxScale=3`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Private OIDC turn execution + slow PostgreSQL reconciler + E2B janitor | Cloud Tasks request-driven, `minScale=1 / maxScale=8` | `service.worker.production.yaml` |
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
  and may autoscale to 8 instances → **40 concurrent turns** only after the
  connection gate below is satisfied.
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
  --min-instances=2 --max-instances=4
```

Takes effect in seconds. **Backfill the manifest + test pins immediately**, or
the next deploy silently reverts the bounds. Do not raise the per-instance turn
limit independently of `containerConcurrency`; prefer adding instances because
that also adds memory headroom and blast-radius isolation.

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

Production manifest peak: 3 API + 8 workers = `3×16 + 8×12 = 144`
connections before migrations, operator SQL, incident tooling, or transient
pool overlap during revisions.

### Production maxScale=8 release gate

**Status: UNMEASURED — the first production deploy is blocked.**

The production manual deploy script, Cloud Build deployment step, and GCP
preflight all fail closed while this exact status remains. Documentation alone
is not the release gate.

Before deploying the production worker manifest, the release owner must:

1. Measure or obtain the production Supavisor session-mode client-connection
   ceiling from the actual Supabase project and record the numeric value here.
2. Reserve explicit headroom for migrations, operations, and revision overlap;
   the accepted ceiling must be at least 160 connections, and a higher margin
   is preferred.
3. Run the 3× fleet-capacity staging burst and the scale-out/scale-in scenarios
   from `PLAN-p3-autoscale.md` without pool timeouts.
4. Replace `UNMEASURED` above with the measured ceiling, date, plan, and release
   owner in the same change that clears the production launch checklist.

`maxScale=8` is a checked-in target, not evidence that this gate passed. If the
measured ceiling is below 160, lower production `maxScale` using the formula
above or upgrade the database plan before deployment.

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
