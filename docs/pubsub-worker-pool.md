# Pub/Sub execution draft

VMA accepts a turn in a short HTTP request, commits its input to `session_turns`
with the user events, and publishes its `(session_id, generation)` key. A Cloud
Run Worker Pool uses StreamingPull to run the existing agent graph. HTTP/SSE
connections are observation channels; their rotation does not cancel this work.
Postgres LISTEN/NOTIFY, the public event schema, and the backend client remain as
they are in this phase.

## Execution and recovery

- Default in staging and production: on-demand 0 ↔ 1 instances, 1 vCPU / 4 GiB
  when active, at most 10 concurrent deliveries. Ten is an admission setting,
  not a measured capacity guarantee. Manual scaling remains available with
  on-demand control disabled, after checking memory and database usage.
- Each process shares its existing bounded pools: 8 + 4 business connections and
  6 checkpoint connections. Preparation commits before model/tool waits. Ownership
  locks are short transactions around writes, not connections held for hours.
- Pub/Sub turns have no whole-turn deadline by default. Set
  `VMA_TURN_TIMEOUT_SECONDS` to a positive value for an application limit; zero
  disables it. Individual model/tool timeouts still apply. The legacy Cloud Tasks
  path retains its 20-minute deadline for the compatibility deployment.
- The subscriber extends acknowledgements for up to 24 hours by default, with
  each extension capped at 60 seconds. This is configurable and is not the agent
  deadline. If a message is redelivered while its database owner is alive, the
  duplicate is acknowledged without executing. The durable record still owns
  recovery even if that acknowledgement removes the broker message.
- A database lease lasts 120 seconds and renews every 30 seconds. The same
  generation and owner token fence heartbeat, event, checkpoint, and completion
  writes. A failed heartbeat cancels local execution. SIGTERM stops intake and
  cancels work; the lease stays until expiry so a replacement can take over.
- Every 60 seconds, workers republish queued or expired turns, including a commit
  followed by a lost initial publish. A retry window limits repeated publishes;
  duplicate deliveries are expected. Recovery scans use `SKIP LOCKED`.
- Network/transport failures, database connection errors, HTTP 429 and HTTP 5xx
  retry up to `VMA_TURN_MAX_ATTEMPTS` (default 3). Other failures finish the turn
  with the existing error/idle events. Exhausted restarts also finish as errors.
- Checkpoints carry the turn identity and use synchronous durability. Recovery
  resumes the saved graph instead of adding the user input again. Stable message
  IDs deduplicate public events, including a crash between checkpoint commit and
  event publication. Completed or human-paused checkpoints are reconciled without
  invoking the graph again. Final idle and session release commit together.

Recovery is deliberately conservative around side effects. If a checkpoint is
waiting on an uncertain tool step, automatic recovery is allowed only for the
built-in `ls`, `read_file`, `glob`, and `grep` tools. Other tool steps produce
`RecoveryRequired`; verify the external result before asking the agent to retry.
This is basic recovery, not exactly-once execution of arbitrary tools. It also
does not resume an unfinished model generation token-for-token: that model step
can run again, and ephemeral preview text can be repeated or lost.

## On-demand startup and idle shutdown

Accepted turns attempt a bounded wake before publishing. Every minute Cloud
Scheduler independently checks the durable `session_turns` records, including
lost publishes, running work, expired leases, and future retries. A worker is
kept at one while any unfinished turn exists. After 15 uninterrupted idle
minutes the target becomes zero. Worker acquisition resets the idle timer too,
so a short turn between Scheduler ticks cannot be missed. This is an idle
timeout; a one-hour or multi-hour turn does not trigger it.

`worker_pool_control` is a singleton per environment/database. Before requesting
zero, the controller commits a closed execution gate. Concurrent submissions
remain accepted and queued until a newer scale-up has fully reconciled. Cloud
Run updates carry an etag and a unique annotation: a delayed old scale-down
cannot land after the confirmed new scale-up, including when a request response
was lost. Short row locks serialize controllers; there is no session advisory
lock or database connection held for the lifetime of a turn. The worker checks
startup readiness every two seconds and republishes pending work when ready.
Cold starts still include Cloud Run provisioning and image startup time; there
is no fixed first-token latency promise. A failed direct wake falls back to the
minute tick, then provisioning and any unexpired ownership lease.

The scheduled endpoint runs in a separate **request-billed** Cloud Run service,
`<api-service>-pool-scaler`, using the same image and `app.scaler:app`. It has
1 vCPU / 512 MiB, min 0, max 1, concurrency 1, CPU throttling enabled, and at most
one business DB connection. Minute ticks do not hit the larger, instance-billed
API and keep it warm all month. Cloud Run IAM and application-level Google OIDC
verification restrict invocation to the Scheduler identity. The runtime identity
gets only `run.workerpools.get/update` as its new project-level role; this shared
identity already spans staging and production. The Scheduler identity only has
Invoker on the small scaler service.

At zero, worker compute is zero; Scheduler jobs, scaler requests/startups, and
Pub/Sub retain their own billing. Actual savings depend on workload clustering
and the 15-minute warm window. No always-on controller instance is required.
Keep the same idle-time setting in API, worker and scaler if overriding 900.

## Staged activation

The API manifests intentionally start with `TURN_DISPATCH=cloud`. This makes the
first deployment a compatibility release, before any Pub/Sub work is accepted.
An old sweeper does not understand durable queued work and could mark it failed.

1. Deploy this image and its additive migrations with the existing deploy scripts.
   Both the API and legacy Cloud Tasks worker must have the compatibility code.
   Wait for pre-release instances and their in-flight requests to exit. Do not
   cut over while an old sweeper is still running.
2. Provision the topic, Pull subscription, and scoped IAM grants with:
   `sh scripts/gcloud/9-setup-pubsub.sh staging` (or `production`). The deployer
   also needs permissions to deploy Worker Pools and use the runtime identity.
   Then run `sh scripts/gcloud/10-setup-worker-pool-scaling.sh staging` (or
   `production`) with an administrator that can manage custom IAM roles,
   service accounts and Scheduler jobs. This deploys the small private scaler
   using the compatibility API image and creates the minute job. Calls before
   the pool exists can return 503; the first pool deployment completes setup.
3. Change `TURN_DISPATCH` from `cloud` to `pubsub` in `service.staging.yaml`, commit,
   and use the normal staging deployment. Local scripts and Cloud Build detect
   that value and run `deploy-pubsub.sh` after migration, deploying the pool before
   the API and updating the small scaler image. The adapter verifies the
   Scheduler job is enabled and targets the correct service/audience before
   activation. Existing 0/1 counts are preserved; a new pool starts at zero.
   Repeat for production after staging acceptance.
4. Verify worker startup logs and a short real turn. A successful Worker Pool
   deployment alone does not prove the subscriber is healthy. Then run a turn
   beyond 30 minutes, recycle its instance during model execution, and verify
   that it resumes with one final idle event and no duplicate durable output.
   Also exercise cancellation, tool pauses, queue saturation, and a Pub/Sub outage.
   Verify the first request wakes a zero-sized pool, a lost initial publish is
   recovered, a long active turn prevents scale-down, and 15 idle minutes return
   to zero. Submit a new turn during shutdown and verify it waits then executes.
5. Allow already accepted Cloud Tasks work to drain before disabling that queue
   and its old worker service. They are retained for rollback in this draft.

For rollback, set the API manifest back to `cloud` and deploy the compatibility
image. Keep the Worker Pool running until accepted Pub/Sub turns drain. Do not
downgrade the schema or restore pre-compatibility worker images while any durable
turn is outstanding. Switching the dispatch setting affects new turns only.

The backend still has a separate 20-minute turn deadline and a tool-driving loop
coupled to its stream. Its corresponding change is required before claiming
multi-hour operation through the complete Votrix UI. This PR changes VMA only.
Moving LISTEN/NOTIFY is a separate follow-up.

To use manual scaling above one, first let the pool reach a settled ready state
with no scaling operation pending. Pause its Scheduler job and disable
`VMA_WORKER_POOL_ON_DEMAND` on the small scaler service. Set the same flag to
`false` in the environment's API manifest and deploy; the adapter passes it to
the workers. Then change the instance count. Do not manually expand the pool
while 0 ↔ 1 control is enabled: both deployment and reconciliation reject that
mixed policy. Re-enable the flag on all three components and resume the minute
job before returning to on-demand mode. Keep the scaler running during a switch
back to Cloud Tasks until all previously accepted Pub/Sub work has drained.

## Local verification

`uv run --extra dev pytest` runs the isolated suite. For actual PostgreSQL row
locks, set `VMA_TEST_POSTGRES_URL` to an expendable local test database and run
`pytest tests/test_pubsub_turns.py`; it creates and removes unique test schemas.
The suite includes a real persisted LangGraph checkpoint with fake model nodes.
No provider or customer database is needed.

Platform references: [StreamingPull](https://docs.cloud.google.com/pubsub/docs/pull),
[lease management](https://docs.cloud.google.com/pubsub/docs/lease-management),
[Worker Pool container lifecycle](https://docs.cloud.google.com/run/docs/container-contract#for_worker_pools),
[manual scaling](https://docs.cloud.google.com/run/docs/configuring/workerpools/manual-scaling).
The controller uses the [v2 worker pool API](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.workerPools),
and Scheduler uses [authenticated HTTP targets](https://docs.cloud.google.com/scheduler/docs/http-target-auth).
