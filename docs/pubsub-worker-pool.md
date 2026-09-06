# Pub/Sub execution draft

VMA accepts a turn in a short HTTP request, commits its input to `session_turns`
with the user events, and publishes its `(session_id, generation)` key. A Cloud
Run Worker Pool uses StreamingPull to run the existing agent graph. HTTP/SSE
connections are observation channels; their rotation does not cancel this work.
Postgres LISTEN/NOTIFY, the public event schema, and the backend client remain as
they are in this phase.

## Execution and recovery

- Default: one 1-vCPU/4-GiB instance, at most 10 concurrent deliveries. Ten is an
  admission setting, not a measured capacity guarantee. Raise instance count
  separately after checking queue delay, memory, and database usage.
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

## Staged activation

The API manifests intentionally start with `TURN_DISPATCH=cloud`. This makes the
first deployment a compatibility release, before any Pub/Sub work is accepted.
An old sweeper does not understand durable queued work and could mark it failed.

1. Deploy this image and its additive migration with the existing deploy scripts.
   Both the API and legacy Cloud Tasks worker must have the compatibility code.
   Wait for pre-release instances and their in-flight requests to exit. Do not
   cut over while an old sweeper is still running.
2. Provision the topic, Pull subscription, and scoped IAM grants with:
   `sh scripts/gcloud/9-setup-pubsub.sh staging` (or `production`). The deployer
   also needs permissions to deploy Worker Pools and use the runtime identity.
3. Change `TURN_DISPATCH` from `cloud` to `pubsub` in `service.staging.yaml`, commit,
   and use the normal staging deployment. Local scripts and Cloud Build detect
   that value and run `deploy-pubsub.sh` after migration, deploying the pool before
   the API. Existing manual instance counts, including zero, are preserved; a
   new pool starts at one. Repeat for production after staging acceptance.
4. Verify worker startup logs and a short real turn. A successful Worker Pool
   deployment alone does not prove the subscriber is healthy. Then run a turn
   beyond 30 minutes, recycle its instance during model execution, and verify
   that it resumes with one final idle event and no duplicate durable output.
   Also exercise cancellation, tool pauses, queue saturation, and a Pub/Sub outage.
5. Allow already accepted Cloud Tasks work to drain before disabling that queue
   and its old worker service. They are retained for rollback in this draft.

For rollback, set the API manifest back to `cloud` and deploy the compatibility
image. Keep the Worker Pool running until accepted Pub/Sub turns drain. Do not
downgrade the schema or restore pre-compatibility worker images while any durable
turn is outstanding. Switching the dispatch setting affects new turns only.

The backend still has a separate 20-minute turn deadline and a tool-driving loop
coupled to its stream. Its corresponding change is required before claiming
multi-hour operation through the complete Votrix UI. This PR changes VMA only.
Automatic pool scaling and moving LISTEN/NOTIFY are separate follow-ups.

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
