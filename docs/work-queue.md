---
title: Work Queue
description: Durable environment work, leases, heartbeats, retries, and worker operation.
---

Session execution now writes a durable `environment_work` resource before execution.

This queue is visible state for session execution. It does not mean local development must run a separate worker.

## Current MVP

- User events enqueue a work item with `session_id`, trigger, attempt count, and queued timestamp.
- `cloud` environments can be consumed inline in local mode. The checked-in
  hosted profile separates API instances from a private, manually scaled Cloud
  Run worker fleet; each worker process runs bounded durable consumers.
- `local` environments are an explicit development/test escape hatch and are also consumed inline.
- `self_hosted` environments only enqueue work. Workers use the environment work routes to lease and report progress.
- `GET /v1/environments/{environment_id}/work/poll` leases one queued item and
  assigns a unique `lease_id` plus monotonically increasing generation.
- `POST /work/{work_id}/ack` marks it running only for the current worker and
  lease ID.
- `POST /work/{work_id}/heartbeat` records progress and extends that same lease.
- Ack, heartbeat, execution, and terminal completion verify the worker,
  `lease_id`, and generation; an old worker cannot finalize a recovered attempt.
- Direct HTTP workers authenticate with a database-issued API key that belongs
  to the target Organization and includes the `worker` scope. They use the same
  `x-api-key` or Bearer authentication schemes as other API clients.
- Expired `leased` or `running` work can be recovered by a later poll from
  another worker, which receives a new lease ID/generation.
- Transient runtime failures can mark work `rescheduling`; polling respects `retry_at` before leasing it again.
- Execution attempts are capped in hosted configuration; exhausted work becomes
  terminal instead of being recovered forever.
- `POST /work/{work_id}/stop` marks it stopped.
- `vma-worker` leases work before execution, heartbeats for the full turn, and
  passes its worker/lease/generation identity into execution, so the CLI and
  embedded-consumer paths follow the same ownership model as direct HTTP workers.
- Enqueue reserves one unit of the Organization active-work quota. Terminal
  completion, error, or stop releases it idempotently; queued/rescheduling work
  keeps its reservation.

This durable queue and the Session execution lease support the maintained
multi-instance worker fleet. They fence stale terminal writes and share
checkpoints through PostgreSQL, but they do not make provider, MCP, or sandbox
side effects exactly-once. Strong worker identity/RBAC, managed push dispatch,
queue-depth autoscaling, and a separate dead-letter resource remain future
work; P3 is deliberately deferred.

Live previews do not travel through work rows. Local mode defaults to the
in-process preview bus, while checked-in hosted services publish bounded frames
through PostgreSQL `pg_notify` so API SSE subscribers can observe turns running
in another worker process. Preview delivery is best-effort and clients reconcile
against durable Session events.

## Optional Self-Hosted Worker

Most users should not run this in local development. Use it only for `self_hosted` environments or queue lifecycle testing:

```bash
vma-worker --poll-interval 1
```

Use `--environment-id env_...` to constrain the worker to one environment, `--worker-id worker-...` to set a stable lease owner, or `--once` for one-shot execution in tests and maintenance jobs.

Before connecting a direct HTTP worker, use the authenticated `/v1/api_keys`
lifecycle to issue a separate, tenant-bound key with `worker` scope. Store its
one-time plaintext result in the worker's secret manager and rotate or revoke it
independently of user-facing API keys.
