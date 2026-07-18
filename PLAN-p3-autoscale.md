# VMA P3 Auto Scale — Implementation Handoff (Stage A + Stage B)

Self-contained spec for a coding agent. Companion to `PLAN-horizontal-scaling.md` (P1/P2/P2.5), which MUST be fully implemented first. Decision context: the service has not launched; the full sequence P1 → P2 → P2.5 → Stage A → Stage B → load test ships as the **first-release architecture**. Keep Stage A and Stage B as separate, independently revertable commits; do not interleave them with P1/P2 commits.

## Mission

Make turn execution safe to replay (Stage A), then let Cloud Run autoscale worker instances on in-flight turns via Cloud Tasks push dispatch (Stage B). Postgres remains the only source of truth throughout; Cloud Tasks is a wake-up signal and scale driver — if the queue is broken or misconfigured, the system degrades to polling and loses nothing.

**Target shape:**

```
user turn → Postgres work item (durable, exists)
         → named Cloud Task (hash-prefixed, idempotent create)
         → OIDC POST /internal/work/{work_id}/execute on the worker service
         → lease-fenced execute_work_item (exists)
         → Cloud Run scales workers on in-flight turns
slow reconciler (existing poller, demoted) → recovers expired leases + missed dispatches
```

**Explicitly out of scope:** exactly-once E2B side effects (contract is bounded at-least-once); Redis/Pub/Sub; queue-depth dashboards; any public API/OpenAPI change.

**Guardrail amendments vs the P1/P2 doc:** Stage A is allowed to touch `app/runtime/deepagents_engine.py` and `app/runtime/runner.py`, but ONLY at the exact points specified below. The lease algorithm in `work_queue.py` stays untouched except the attempt-semantics amendment in Stage A.3.

---

## Stage A — bounded-duplicate turn replay (hard gate for Stage B, ~2–4 days)

### Why (the crash windows, from the actual code)

`_run_session_turn` (runner.py) flow: execute graph (events are persisted incrementally via `emit_event` during streaming, including the final `agent.message`) → `_record_model_usage_after_result` → finalize transaction (session run_state/status/stop_reason, discovered outputs). Crash windows:

- **W1 — mid-graph**: LangGraph checkpoint recovers completed supersteps; the interrupted superstep replays → possible duplicate side effects/events. Bounded by the attempt cap; cannot be fully eliminated.
- **W2 — graph completed, finalize not committed** (the critical gap): `run_state.last_input_event_seq` was never persisted, so a retry re-derives the same candidate input, **appends the same user message to the thread again, and re-invokes the model** → duplicate model execution, duplicate `agent.message`. A database journal written only after `_execute(...)` returns is not sufficient: there is still a graph-complete/pre-journal crash window. Stage A eliminates W2 with a completion marker written by the graph itself into the terminal LangGraph checkpoint, followed by the database journal.
- **W3 — after finalize, before work status update**: already safe — `_graph_input` sees the updated seq, returns `None`, no model call.

### A.1 Checkpoint-backed completion marker + turn journal (crash-safe finalize)

- Add a VMA extension of the Deep Agents state schema with a JSON `vma_turn_marker`, and a final `after_agent` middleware node that runs immediately before graph `END`. The admitted graph input records `{ "version": 1, "work_id": ..., "input_seq": ..., "agent_version_id": ..., "phase": "started" }`; the final node changes the same marker to `phase="completed"`. This is a real graph node, so LangGraph checkpoints it before the graph reports completion. If the process dies after the last model/tool node but before this final node, the durable checkpoint still points at the final node and recovery resumes it with no new user input and no model call.
- Before deriving/submitting a new graph input, inspect the latest checkpoint for the exact `(thread_id, work_id, input_seq, agent_version_id)` marker. A matching completed marker proves the graph already finished. A matching started marker whose checkpoint has pending graph work resumes that checkpoint with `None`/the LangGraph continuation mechanism; it must never submit the same user message again. An older marker for a different input is simply not proof for the new turn; a conflicting marker for the same input seq but another work item or Agent version fails closed rather than silently suppressing or replaying execution.
- Immediately after `_execute(...)` returns, in a small dedicated transaction (lease-fenced with `_lease_matches`), write a versioned, JSON-only journal that can faithfully reconstruct the `RuntimeResult`:

  ```json
  {
    "version": 1,
    "input_seq": 123,
    "agent_version_id": "aver_...",
    "run_state": {},
    "final_text": "...",
    "events_persisted": true,
    "requires_action": false,
    "blocking_event_ids": [],
    "usage": {},
    "sandbox_state": {},
    "outputs_persisted": true
  }
  ```

  Persist discovered outputs in this same transaction before setting `outputs_persisted=true`. `version` is the journal schema version; unknown versions fail closed.
- At turn start (only when `work_lease` is present), prefer a matching `work.data["turn_journal"]`. If its `input_seq` is greater than the Session's persisted `run_state.last_input_event_seq`, **skip `_execute` entirely**, synthesize the complete `RuntimeResult` including `final_text` and `events_persisted`, and run the normal usage + finalize path. If the journal is absent but the checkpoint has the matching completed marker, rebuild the result from the checkpointed state, re-discover only bounded sandbox outputs as needed, write the same journal, and then finalize without invoking the model. If the journal's seq is already persisted, clear it and complete the work row without starting another execution.
- Usage on recovery is naturally deduped by the existing `model_tokens:{work_id}` idempotency key — add a test proving a double-record attempt counts once.
- No journal when `work_lease is None` (direct test invocations): behavior unchanged.
- Journal and marker hold JSON only — never file bytes. If recovery cannot re-discover sandbox outputs, skip them with a logged warning (bounded, documented gap); this does not permit re-running a graph whose completed marker exists.

### A.2 Deterministic event IDs + idempotent append

- `events_q.append_event`: when a caller supplies `event_id`, tolerate a duplicate — on unique-constraint conflict, roll back to a savepoint and return the existing row. A wasted seq (gap) is acceptable; SSE cursors are ordered, not dense.
- `deepagents_engine.py` (only these lines): derive the final message event id deterministically — `"evt_" + sha1(f"{thread_id}:{processed_seq}:final").hexdigest()[:24]` instead of `new_id("evt")` (both the preview `message_event_id` and the final payload `_event_id`). Derive tool event ids as `"evt_" + sha1(f"{thread_id}:{tool_use_id}:{event_type}").hexdigest()[:24]`. This dedupes replays of the *same* streamed response (W1 partial-persist, W2); a fresh model call generates new `tool_use_id`s — that residual duplication is accepted and bounded by the attempt cap.
- Retry visibility: the `session.status_running` event runner appends at turn start gains `attempt` and `work_id` payload fields, so takeovers are observable by operators and clients.

### A.3 Attempt-admission semantics (interlocks with Stage B — do not skip)

P1.1 as originally specced increments `attempt` in `lease_work`. Moving the increment mechanically to the current `status="running"` assignment is still wrong: `run_session_turn` can subsequently return `False`, producing `deferred` after the counter was already burned. Implement an explicit admission boundary instead:

- `lease_work` advances only the lease generation; acquiring or recovering a lease never changes `attempt`.
- Pass an async admission hook from `execute_work_item` through `runner.py` into `execute_deep_agent`. Invoke it only after the process-local Session guard and database Session checks pass, `_graph_input` has found real graph work, and checkpoint/journal recovery has determined that execution rather than finalize-only recovery is required — immediately before `graph.astream(...)` starts or resumes.
- The admission hook locks Session then work in the established order, revalidates the lease, computes `next_attempt = attempt + 1`, applies the max-attempt check, and atomically writes the increment, `started_at`/`started_by`, work `status="running"`, Session `status="running"`, and the observable `session.status_running` event. Only after that transaction commits may model/tool execution begin.
- Busy-process, stale-lease, non-runnable Session, no-new-input, already-finalized journal, and completed-checkpoint paths never call the hook. They therefore return `deferred`/terminal recovery without consuming an attempt. A failure after successful admission does consume one attempt, even if it occurs before the first provider response.
- Preserve the existing contract exactly: `VMA_WORK_MAX_ATTEMPTS=0` disables the cap; a positive value `N` permits `N` admitted executions, and the next admission finalizes the work as exhausted without invoking the graph.

The lease heartbeat may start while work is merely leased, but the attempt counter and `running` timestamps belong exclusively to the successful admission transaction. Update P1.1's tests accordingly.

### Stage A tests

- W2 checkpoint recovery (Postgres): crash after the terminal graph checkpoint but before the database journal; re-run `execute_work_item` → the matching checkpoint marker rebuilds the journal, the model runner is NOT invoked again (spy), and the Session finalizes with exactly one `agent.message` and one usage charge for that input seq.
- Final-node recovery (Postgres): crash after the last model/tool node checkpoint but before the completion-marker node; retry resumes only that node with no new user message and no model call.
- Journal round trip: recovered data preserves `version`, `final_text`, `events_persisted`, `run_state`, action fields, usage, and sandbox state; an unknown journal version fails closed.
- Idempotent append: same `event_id` twice → one row, second call returns the existing event.
- Deterministic ids: replaying `_emit` for the same `(thread_id, seq)` produces the same id; different seq → different id.
- Attempt admission: repeated lease + busy/no-input/deferred outcomes do not consume attempts; an admitted failure consumes one; three admitted executions then exhaustion behaves per P1.1; cap `0` remains unlimited.

---

## Stage B — Cloud Tasks push dispatch (~3–5 days, after Stage A ships)

### Config (`app/config.py`)

```python
vma_work_dispatch_mode: Literal["poll", "hybrid"] = "poll"   # poll = today; local/test never change
vma_tasks_queue: str = ""            # e.g. "vma-turns"
vma_tasks_location: str = ""         # e.g. "us-central1"
vma_tasks_service_account: str = ""  # OIDC SA email (vma-runtime@...)
vma_worker_url: str = ""             # base URL of the worker service
vma_worker_turn_limit: int = Field(default=5, ge=1)
```
Fail fast at startup if `hybrid` is set with any of the queue settings empty. New dependency: `google-cloud-tasks`.

### B.1 Dispatcher — new `app/runtime/dispatch.py`

- `async def dispatch_work(work_id: str, *, attempt: int, schedule_at: datetime | None = None) -> None`
- Task name: `wk-{sha1(work_id).hexdigest()[:8]}-{work_id}-a{attempt}` — the hash prefix avoids queue hotspotting on sequential names (Cloud Tasks scaling guidance); the `attempt` suffix makes every retry generation a fresh name, so the ~dedup window on reused names is irrelevant.
- Target: `POST {vma_worker_url}/internal/work/{work_id}/execute` with OIDC token for `vma_tasks_service_account`; `schedule_time` from `schedule_at` when set; swallow `ALREADY_EXISTS`; log-and-continue on any creation failure (the reconciler is the safety net — dispatch is never load-bearing for correctness).
- Behind a small interface with a recording fake for tests; a no-op implementation when mode is `poll`.

### B.2 Call sites

- `app/routers/sessions.py` (`resume`, `send_events`): when mode is `hybrid`, `background_tasks.add_task(dispatch_work, work.id, attempt=0)` — after-response ⇒ after-commit, same pattern as the existing inline branch.
- `execute_work_item` finalize: when the outcome is `rescheduling`, dispatch a follow-up task with `schedule_at=retry_at`.

### B.3 Worker push endpoint

- `POST /internal/work/{work_id}/execute` mounted ONLY on the worker-role app (P2.1 gives it its own surface). Auth = Cloud Run IAM: the worker service is private and Cloud Tasks invokes with OIDC (`roles/run.invoker`); do not build custom in-app auth.
- Handler: acquire the shared turn limiter (see B.4), then `await execute_work_item(work_id, worker_id=f"push-{uuid4().hex[:8]}", lease_seconds=settings.vma_worker_lease_seconds)`.
- **Outcome → HTTP mapping (the design-sensitive table; wrong mappings cause retry storms or silent loss):**

| `execute_work_item` outcome | HTTP | Rationale |
|---|---|---|
| `completed`, `error`, `stopped`, `exhausted` | 200 | terminal — never retry |
| `rescheduling` | 200 | follow-up task already scheduled at `retry_at` |
| `already_running`, `superseded` | 200 | another executor owns it; its own lifecycle covers it |
| `missing`, `not_runnable` | 200 | nothing to execute |
| `deferred` | 503 + `Retry-After: 15` | transient (session busy / lease not ready); Cloud Tasks backs off and retries — costs no attempt after A.3 |
| unexpected exception | 500 | transient infra; Cloud Tasks retries |

### B.4 Shared turn limiter (poller and push must not add up)

Process-wide `asyncio.Semaphore(vma_worker_turn_limit)`. The push handler blocks on it (holding the HTTP request open is correct — it signals concurrency to the autoscaler); the reconciler poller uses **non-blocking** acquire and skips the cycle when full. Without this, `containerConcurrency` bounds only HTTP turns and poller coroutines stack on top.

### B.5 Reconciler (the poller, demoted — it stays forever)

In `hybrid` mode the embedded worker loop keeps running with `VMA_WORKER_CONCURRENCY=1` and `VMA_WORKER_POLL_INTERVAL_SECONDS=20`. It is the recovery path for expired leases (instance death mid-turn) and missed task creation. Push is an optimization over poll, never a replacement — removing the poller loses crash recovery.

### B.6 Manifests + infra

- Worker manifests: `VMA_WORK_DISPATCH_MODE=hybrid`, `containerConcurrency: 5` (was 10 — it now carries turn requests and must equal `vma_worker_turn_limit`), `minScale: 1`, `maxScale: 8` production (verify `8×12 + API 3×16 ≈ 144` connections fit the Supabase plan per `private-docs/scaling-runbook.md` before raising; staging `1/2`), worker concurrency/poll pins per B.5, queue/location/SA/worker-URL envs.
- API manifests: `VMA_WORK_DISPATCH_MODE=hybrid` (it only dispatches).
- New `scripts/gcloud/8-setup-cloud-tasks.sh` (numbers 6 and 7 already exist): create queues (`vma-turns`, `vma-turns-staging`) with `maxAttempts=8`, `minBackoff=5s`, `maxBackoff=300s`, and `maxConcurrentDispatches=100`; grant `roles/cloudtasks.enqueuer`, both required `iam.serviceAccounts.actAs` bindings, the primary `roles/cloudtasks.serviceAgent`, and worker-scoped `roles/run.invoker`. The 1,800-second dispatch deadline belongs to each Task in the runtime dispatcher, not to the queue. First deploy creates a missing worker in poll mode, discovers its real Cloud Run URL, grants Invoker, then renders final hybrid worker/API revisions; no URL is guessed or stored as a secret. `preflight.sh`, `status.sh`, the deployment README, and static config tests pin the contract. Production deploy paths fail closed while the connection ceiling remains `UNMEASURED`.

### Stage B tests

- Mapping table: one test per outcome row (monkeypatched `execute_work_item`), asserting exact status codes and `Retry-After` on `deferred`.
- Race (Postgres): push request and poller contending for one work item → one executes, the other maps to `already_running`/`superseded` → 200.
- Deferred storm: repeated 503-driven re-invocations never consume attempts (A.3 interlock).
- Dispatcher: named-task idempotency (ALREADY_EXISTS swallowed), `schedule_at` propagation, creation failure logged without raising.
- Reconciler: undispatched `queued` work (task creation "failed") is picked up and executed; expired mid-turn lease recovered.
- Limiter: with the semaphore held at capacity, the poller skips and a push request waits rather than exceeding `vma_worker_turn_limit`.
- Poll mode: full existing suite green with zero behavior change (`poll` is the default everywhere except hosted manifests).

---

## Load test + first-launch gate (after both stages, ~1–2 days)

1. Staging with real Cloud Tasks/E2B/Supabase: burst ≥ 3× fleet capacity of concurrent turns → workers scale out; every turn completes; zero duplicate `agent.message` per input seq; zero double-counted usage (SQL asserts).
2. Kill a worker instance mid-turn (force new revision): lease expires → reconciler recovers → Stage A journal/dedupe holds (no second model call for a completed graph; at most one bounded superstep replay otherwise).
3. Scale-in drain: turns in flight complete during scale-down; none are killed by it.
4. Break dispatch deliberately (wrong queue name in staging): system degrades to reconciler-paced execution; nothing is lost; alarms/logs make the degradation visible.
5. Derive and record the final production `maxScale` from the measured connection ceiling in `private-docs/scaling-runbook.md`.

First production deploy happens only after all four pass.

## Acceptance checklist

- [x] W2 crash window closed: a completed graph is never re-invoked; exactly one `agent.message` per input seq under forced crashes at every boundary (last graph node/pre-marker, completed marker/pre-journal, post-journal, post-usage, pre-finalize).
- [x] Version-1 journals round-trip `final_text`, `events_persisted`, and every other field required to reconstruct `RuntimeResult`; unknown versions fail closed.
- [x] Duplicate emissions dedupe via deterministic ids + idempotent append; seq gaps tolerated.
- [x] Attempts count admitted graph executions, not leases or pre-admission status changes; `deferred` never burns the cap and `VMA_WORK_MAX_ATTEMPTS=0` remains unlimited.
- [x] Outcome→HTTP mapping exactly per table; terminal outcomes never retried by Cloud Tasks.
- [x] Push handler and poller share one turn limiter; instance-level concurrency can never exceed `vma_worker_turn_limit`.
- [x] Reconciler alone (dispatch disabled) still executes all work correctly.
- [x] `poll` mode byte-identical to pre-Stage-B behavior; local dev/test untouched.
- [x] Postgres remains sole source of truth: deleting the Cloud Tasks queue loses no work.
- [x] Hosted API/worker manifests pin hybrid dispatch, queue identity, OIDC service account, a discovered worker URL, worker `containerConcurrency=5`, and the 1/8 production plus 1/2 staging bounds.
- [x] Idempotent queue/IAM setup, first-deploy URL bootstrap, read-only preflight checks, status reporting, and static deployment tests are implemented; the production maxScale=8 connection gate remains explicitly unmeasured.
- [ ] Load-test gate (all four scenarios) passed before first production deploy; final `maxScale` recorded in the runbook.
