# Worker Capacity and Horizontal Scaling Runbook

Internal only. Do not copy this runbook into the public documentation tree.

This runbook describes how to size and scale VMA turn-execution capacity on
Cloud Run. It assumes the P1 hardening, P2 API/worker split, and mandatory P2.5
cross-instance preview transport from `PLAN-horizontal-scaling.md` have shipped.
Autoscaling (P3, Cloud Tasks) ships pre-launch as part of the first release
(spec: `PLAN-p3-autoscale.md`).
Cloud Tasks push drives Cloud Run autoscaling. Hosted services scale to zero;
Session leases remain authoritative when a worker disappears or while the
services are idle.

## First-release topology after P3

| Service | Role | Scaling | Manifest |
|---|---|---|---|
| `votrix-managed-agents` | Public API + SSE | `minScale=0 / maxScale=2`, request-driven | `service.production.yaml` |
| `votrix-managed-agents-worker` | Private OIDC turn execution | Cloud Tasks request-driven, `minScale=0 / maxScale=4` | `service.worker.production.yaml` |
| `votrix-managed-agents-staging` | Staging API + SSE | `minScale=0 / maxScale=2`, request-driven | `service.staging.yaml` |
| `votrix-managed-agents-staging-worker` | Staging turn execution | Cloud Tasks request-driven, `minScale=0 / maxScale=4` | `service.worker.staging.yaml` |

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
- Production and staging start at zero and may autoscale to 4 worker instances
  → **80 concurrent turns**. The first turn after an idle period includes a
  worker cold start; there is no guaranteed warm turn capacity.
- Turns are IO-bound coordinators (model streaming, E2B, Postgres); heavy
  compute happens inside E2B sandboxes, not on the worker. What a worker
  instance holds per turn is the graph's state, one model stream and one
  sandbox handle, so **memory is the binding constraint, not CPU** — which is
  why capacity is added in instances rather than by raising concurrency on a
  1 vCPU / 4 GiB box.
- Cloud Tasks retains accepted deliveries until a worker is available. The
  application itself permits only one active turn per Session; another message
  to that Session is rejected as busy rather than stored in an application
  work queue.

## How worker autoscaling operates

Deploys in this repo are declarative (`gcloud run services replace <manifest>`),
so the manifest remains the source of truth. **A manual
`gcloud run services update` is reverted by the next deploy.**

Named Cloud Tasks target the private worker URL with an OIDC token. Each task is
an in-flight HTTP request for the duration of the turn, so Cloud Run observes
demand and scales worker instances. Nothing inside the process adds a second
turn limiter beyond `containerConcurrency`. The optional standalone Session
sweep does not execute turns and does not recover failed task creation.

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

- Cloud Tasks queue depth or oldest-task age remains elevated while every
  worker instance is at its concurrency limit.
- Worker instance CPU or memory sustained above ~70% at normal load.

Lower `maxScale` when the fleet never approaches its bound for a week and queue
waits are zero. Change `minScale` only for cold-start or guaranteed-capacity
requirements; Cloud Tasks already supplies the scale signal in hosted `cloud`
dispatch mode.

## What scaling does NOT change

- **Turn speed.** More instances add parallel capacity, not faster turns.
- **Event streaming.** Durable Session events remain the replay source. A
  PostgreSQL `NOTIFY` only wakes listeners so they can poll sooner; losing a
  notification does not lose an event. This transport is not a scaling knob —
  adding worker instances neither helps nor harms it.
- **Correctness.** The per-Session `lock_version` and execution lease fence
  writes from a superseded worker. A task redelivered after the Session has
  returned to idle is a no-op. External tool side effects are not generally
  exactly-once, so high-risk tools still need caller-provided idempotency.

## Cloud Tasks incident fallback

If task creation or delivery is failing:

1. Do not delete queue tasks or rewrite Session state while diagnosing.
2. `minScale=0` is normal: a successfully created Cloud Task wakes the worker.
   Raising the minimum does not recreate a task that was never accepted.
3. Inspect `scripts/gcloud/status.sh`, then rerun
   `scripts/gcloud/8-setup-cloud-tasks.sh <environment>` to repair queue policy
   and IAM drift.
4. For a worker that died mid-turn, the Session lease expires and the next
   message can reclaim it. Run `python -m app.worker` separately only when an
   operator needs stale visible state cleared before another message arrives.

Cloud Tasks retry `maxAttempts=8` is an infrastructure delivery bound. Once the
application receives a turn, it records any execution failure as a Session
event and returns success so the same model turn is not executed again.

## Inline dispatch is local/self-hosted only

`TURN_DISPATCH=inline` remains the zero-infrastructure default for local
development and simple self-hosting. Hosted manifests use `cloud` and deploy the
same image as separate public API and private worker services. API instance
scaling does not add hosted turn capacity; scale the worker manifest instead.
