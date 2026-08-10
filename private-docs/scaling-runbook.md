# Worker Capacity and Horizontal Scaling Runbook

Internal only. Do not copy this runbook into the public documentation tree.

This runbook describes how to size and scale VMA turn-execution capacity on
Cloud Run. It assumes the P1 hardening, P2 API/worker split, and mandatory P2.5
cross-instance preview transport from `PLAN-horizontal-scaling.md` have shipped.
Autoscaling (P3, Cloud Tasks) ships pre-launch as part of the first release
(spec: `PLAN-p3-autoscale.md`).
The checked-in hosted profile uses `VMA_WORK_DISPATCH_MODE=hybrid`: Cloud Tasks
push drives Cloud Run autoscaling, while the permanent PostgreSQL reconciler is
the correctness fallback.

## First-release topology after P3

| Service | Role | Scaling | Manifest |
|---|---|---|---|
| `votrix-managed-agents` | Public API + SSE | `minScale=1 / maxScale=2`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Private OIDC turn execution + slow PostgreSQL reconciler + E2B janitor | Cloud Tasks request-driven, `minScale=1 / maxScale=2` | `service.worker.production.yaml` |
| `votrix-managed-agents-staging` | Staging API + SSE | `minScale=1 / maxScale=2`, request-driven | `service.staging.yaml` |
| `votrix-managed-agents-staging-worker` | Staging turn execution | Cloud Tasks request-driven, `minScale=1 / maxScale=2` | `service.worker.staging.yaml` |

Project `votrixai-480422`. Production runs in `us-east4`; staging runs in
`us-west2` (see `scripts/gcloud/config.sh`).

## Capacity model

```
concurrent turn capacity = worker instances × containerConcurrency
```

Cloud Run's own limit is the only one. An earlier version of this runbook
described a second, process-wide `VMA_WORKER_TURN_LIMIT` held equal to it —
that setting exists nowhere in the code, the manifests, or the deploy scripts,
and never did.

- Worker `containerConcurrency` is `20`, `maxScale` is `4`.
- Production starts with 1 warm instance → **20 guaranteed concurrent turns**
  and may autoscale to 4 instances → **80 concurrent turns**. Staging matches.
- Turns are IO-bound coordinators (model streaming, E2B, Postgres); heavy
  compute happens inside E2B sandboxes, not on the worker. What a worker
  instance holds per turn is the graph's state, one model stream and one
  sandbox handle, so **memory is the binding constraint, not CPU** — which is
  why capacity is added in instances rather than by raising concurrency on a
  1 vCPU / 4 GiB box.
- Admitted overflow waits in the Postgres queue (`environment_work`, status
  `queued`) until a slot frees. The hosted Organization active-work limit is 20,
  so requests beyond that queued/running cap can be rejected rather than queued.

## How worker autoscaling operates

Deploys in this repo are declarative (`gcloud run services replace <manifest>`),
so the manifest remains the source of truth. **A manual
`gcloud run services update` is reverted by the next deploy.**

Named Cloud Tasks target the private worker URL with an OIDC token. Each task is
an in-flight HTTP request for the duration of the turn, so Cloud Run observes
demand and scales worker instances. Nothing inside the process limits turns
beyond that: the push handler and the reconciler share whatever
`containerConcurrency` allows.

`VMA_WORKER_CONCURRENCY=1` and
`VMA_WORKER_POLL_INTERVAL_SECONDS=20` intentionally describe only the slow
reconciler. They do not cap push capacity. If task creation or delivery fails,
the reconciler eventually leases the durable PostgreSQL work row; Cloud Tasks
never carries correctness.

### Standard persistent capacity change

1. Recalculate E2B concurrency/quota, provider limits, and spend ceiling.
2. Edit the worker manifest's `maxScale` and, only when guaranteed warm capacity
   is required, `minScale`. Raising `containerConcurrency` instead trades memory
   headroom for the same capacity — see the capacity model above.
3. Update this runbook in the same change.
4. Ship through the normal migration-gated pipeline and rerun the staging load
   scenarios before production promotion.

### Emergency path (immediate, temporary)

```bash
gcloud run services update votrix-managed-agents-worker \
  --project=votrixai-480422 --region=us-east4 \
  --min-instances=2 --max-instances=2
```

Takes effect in seconds. **Backfill the manifest immediately**, or the next
deploy silently reverts the bounds. Prefer adding instances to raising
`containerConcurrency`: it adds memory headroom with the capacity, and it keeps
one instance's death from taking every turn on it.

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
