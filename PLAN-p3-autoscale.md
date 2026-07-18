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
- **W2 — graph completed, finalize not committed** (the critical gap): `run_state.last_input_event_seq` was never persisted, so a retry re-derives the same candidate input, **appends the same user message to the thread again, and re-invokes the model** → duplicate model execution, duplicate `agent.message`. Stage A eliminates W2 entirely.
- **W3 — after finalize, before work status update**: already safe — `_graph_input` sees the updated seq, returns `None`, no model call.

### A.1 Turn journal (crash-safe finalize) — `app/runtime/runner.py`

- Immediately after `_execute(...)` returns, in a small dedicated transaction (lease-fenced with `_lease_matches`): write `work.data["turn_journal"] = {"input_seq": run_state["last_input_event_seq"], "run_state": result.run_state, "requires_action": result.requires_action, "blocking_event_ids": [...], "usage": result.usage, "sandbox_state": result.sandbox_state}` and persist discovered outputs in this same transaction (mark `"outputs_persisted": true`). Then proceed to usage + finalize as today.
- At turn start (only when `work_lease` is present): if `work.data["turn_journal"]` exists AND its `input_seq` is greater than the session's persisted `run_state.last_input_event_seq` → **skip `_execute` entirely**, synthesize the `RuntimeResult` from the journal, and run the normal usage + finalize path. If the journal's seq is ≤ the persisted seq (finalize already landed), clear the journal and proceed normally.
- Usage on recovery is naturally deduped by the existing `model_tokens:{work_id}` idempotency key — add a test proving a double-record attempt counts once.
- No journal when `work_lease is None` (direct test invocations): behavior unchanged.
- Journal holds JSON only — never file bytes. If recovery cannot re-discover sandbox outputs, skip with a logged warning (bounded, documented gap).

### A.2 Deterministic event IDs + idempotent append

- `events_q.append_event`: when a caller supplies `event_id`, tolerate a duplicate — on unique-constraint conflict, roll back to a savepoint and return the existing row. A wasted seq (gap) is acceptable; SSE cursors are ordered, not dense.
- `deepagents_engine.py` (only these lines): derive the final message event id deterministically — `"evt_" + sha1(f"{thread_id}:{processed_seq}:final").hexdigest()[:24]` instead of `new_id("evt")` (both the preview `message_event_id` and the final payload `_event_id`). Derive tool event ids as `"evt_" + sha1(f"{thread_id}:{tool_use_id}:{event_type}").hexdigest()[:24]`. This dedupes replays of the *same* streamed response (W1 partial-persist, W2); a fresh model call generates new `tool_use_id`s — that residual duplication is accepted and bounded by the attempt cap.
- Retry visibility: the `session.status_running` event runner appends at turn start gains `attempt` and `work_id` payload fields, so takeovers are observable by operators and clients.

### A.3 Attempt-semantics amendment (interlocks with Stage B — do not skip)

P1.1 as originally specced increments `attempt` in `lease_work`. That burns the `VMA_WORK_MAX_ATTEMPTS` cap on **deferred** outcomes (leased, but `run_session_turn` returned False without executing) — under push dispatch, Cloud Tasks retries of a busy session would exhaust an innocent session's attempts. Amend: increment `attempt` at the transition to `running` in `execute_work_item` (just before `data["started_at"]` is set), not in `lease_work`. The cap check stays where P1.1 put it. Only actual executions consume attempts. Update P1.1's tests accordingly.

### Stage A tests

- W2 recovery (Postgres): run a turn, simulate crash after the journal transaction but before finalize (monkeypatch to abort); re-run `execute_work_item` → model runner is NOT invoked again (spy), session finalizes from the journal, exactly one `agent.message` exists for that input seq, usage counted once.
- Idempotent append: same `event_id` twice → one row, second call returns the existing event.
- Deterministic ids: replaying `_emit` for the same `(thread_id, seq)` produces the same id; different seq → different id.
- Attempt semantics: repeated lease + deferred outcomes do not consume attempts; three real executions then exhaustion behaves per P1.1.

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
- New `scripts/gcloud/6-setup-cloud-tasks.sh`: create queue (`vma-turns`, `vma-turns-staging`; `dispatchDeadline=1800s` — fits the 900s turn budget with init/finalize margin; `maxAttempts=8`, `minBackoff=5s`, `maxBackoff=300s`, modest `maxConcurrentDispatches≈100`); grant `roles/cloudtasks.enqueuer` to the runtime SA and `roles/run.invoker` on the worker service to the same SA. Update `preflight.sh`, `status.sh`, README, and `tests/test_cloud_run_config.py` pins in the same commit.

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

- [ ] W2 crash window closed: a completed graph is never re-invoked; exactly one `agent.message` per input seq under forced crashes at every boundary (post-journal, post-usage, pre-finalize).
- [ ] Duplicate emissions dedupe via deterministic ids + idempotent append; seq gaps tolerated.
- [ ] Attempts count executions, not leases; `deferred` never burns the cap.
- [ ] Outcome→HTTP mapping exactly per table; terminal outcomes never retried by Cloud Tasks.
- [ ] Push handler and poller share one turn limiter; instance-level concurrency can never exceed `vma_worker_turn_limit`.
- [ ] Reconciler alone (dispatch disabled) still executes all work correctly.
- [ ] `poll` mode byte-identical to pre-Stage-B behavior; local dev/test untouched.
- [ ] Postgres remains sole source of truth: deleting the Cloud Tasks queue loses no work.
- [ ] Load-test gate (all four scenarios) passed before first production deploy; final `maxScale` recorded in the runbook.
