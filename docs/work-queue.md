# Work Queue

Session execution now writes a durable `environment_work` resource before execution.

This queue is visible state for session execution. It does not mean local development must run a separate worker.

## Current MVP

- User events enqueue a work item with `session_id`, trigger, attempt count, and queued timestamp.
- `cloud` environments are consumed inline by the API process for the default local and hosted-provider path.
- `local` environments are an explicit development/test escape hatch and are also consumed inline.
- `self_hosted` environments only enqueue work. Workers use the environment work routes to lease and report progress.
- `GET /v1/environments/{environment_id}/work/poll` leases one queued item.
- `POST /work/{work_id}/ack` marks it running.
- `POST /work/{work_id}/heartbeat` records progress and extends the lease.
- `ack` and `heartbeat` require the caller's `worker_id` to match the current lease owner.
- When `VMA_WORKER_TOKEN` is set, all work routes also require `x-worker-token`.
- Expired `leased` or `running` work can be recovered by a later poll from another worker.
- Transient runtime failures can mark work `rescheduling`; polling respects `retry_at` before leasing it again.
- `POST /work/{work_id}/stop` marks it stopped.
- `vma-worker` leases work before execution and passes its `worker_id` back into execution, so the CLI path follows the same lease ownership model as direct HTTP workers.

This makes pending work visible in Postgres, but it is not yet equivalent to a production queue. Production should move inline execution to Cloud Tasks, Pub/Sub, or a dedicated worker service with stronger fencing locks, durable retry execution, and worker identity/RBAC when long-running async execution is required.

## Optional Self-Hosted Worker

Most users should not run this in local development. Use it only for `self_hosted` environments or queue lifecycle testing:

```bash
vma-worker --poll-interval 1
```

Use `--environment-id env_...` to constrain the worker to one environment, `--worker-id worker-...` to set a stable lease owner, or `--once` for one-shot execution in tests and maintenance jobs.

If `VMA_WORKER_TOKEN` is configured, direct HTTP workers must send:

```text
x-worker-token: ...
```
