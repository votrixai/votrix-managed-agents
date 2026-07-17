# VMA Horizontal Scaling — Implementation Handoff (P1 + P2)

Self-contained spec for a coding agent. No conversation context is assumed. Follow it in order; P1 and P2 are independently shippable.

## Mission

Make the VMA control plane safe to run on multiple Cloud Run instances (P1), then split it into an API service and a worker service with a fixed, manually-scaled worker fleet (P2), while preserving token-level preview streaming across instances with a Postgres `pg_notify` broker (P2.5).

**Explicitly out of scope (do NOT build):**
- Cloud Tasks / Pub/Sub / Redis / any new external infrastructure **within this doc's commits**. P3 (Stage A turn-replay safety + Stage B Cloud Tasks dispatch) is now specced in the companion `PLAN-p3-autoscale.md` and ships pre-launch, but strictly as separate commits AFTER everything in this doc is implemented and green. Do not interleave. Note: `PLAN-p3-autoscale.md` Stage A.3 amends this doc's P1.1 attempt semantics (attempts count executions, not leases) — if implementing both docs in one pass, apply the amendment directly.
- Any change to the public API surface, OpenAPI schema, event shapes, or SDK-facing behavior. (P2.5 changes only the internal transport of preview frames; the `event_deltas` parameter, frame format, and SSE reconciliation stay byte-identical.)
- Any change to `app/runtime/deepagents_engine.py` or the lease algorithm in `app/runtime/runner.py`. `app/runtime/vma_preview_bus.py` also stays unchanged — P2.5 adds a wrapper module around it, and the only `runner.py` edit allowed is the one-line `emit_preview` import swap specified there.

## Current architecture (verified facts — do not re-derive, do not "fix")

- One Cloud Run service (`service.production.yaml`, `service.staging.yaml` at repo root) runs the FastAPI API **and** an embedded worker: the lifespan in `app/factory.py` spawns `vma_worker_concurrency` copies of `run_worker()` (`app/worker.py`) plus an E2B janitor task. Production pins `minScale=1 / maxScale=1`, `VMA_EMBEDDED_WORKER_ENABLED=true`, `VMA_WORKER_CONCURRENCY=5`.
- Work queue = Postgres rows (`resource_type="environment_work"`) in `app/runtime/work_queue.py`. Leases carry `worker_id + lease_id + generation` with heartbeat and expiry; takeover of expired leases happens via `_lease_next_work` → `lease_work` (bumps `attempt` and `generation`).
- Session-level execution lease lives in `session.status_details["execution_lease"]`; `app/runtime/runner.py` re-verifies it before persisting every event (`_persist_runtime_event`) and before finalizing. Zombie workers are fenced at the DB.
- LangGraph checkpoints: `app/runtime/checkpoints.py` currently opens a **new** `AsyncPostgresSaver.from_conn_string(...)` and runs `setup()` **per turn**.
- SSE streaming polls durable events from the DB (works across instances); preview frames are process-local by design.
- Embedded worker IDs are already unique per process (`embedded-{index}-{uuid4().hex}` in `app/factory.py`). Rate limits and quotas are Postgres counters (`app/governance.py`) — already multi-instance safe. Do not touch these.
- Deploys: Cloud Build (`cloudbuild.yaml`) and `scripts/gcloud/2-deploy-production.sh` / `3-deploy-staging.sh` run a migration Cloud Run **Job** first, then `gcloud run services replace <manifest>`. `tests/test_cloud_run_config.py` pins the entire topology and must be updated in lockstep.

**Lock-ordering invariant (critical):** every existing code path that locks both rows takes the **session row first, then the work row** (`runner.py` turn start; `_persist_runtime_event`). Never write code that holds a work-row `FOR UPDATE` while acquiring a session-row lock — that inverts the order and can deadlock. P1.1 below is designed around this.

---

## P1.1 — Cap work retry attempts (bug fix: retries are currently unbounded)

A work item whose executor keeps crashing is re-leased forever (lease expiry → takeover → `attempt` increments without limit). Add a cap.

### Config (`app/config.py`)
```python
vma_work_max_attempts: int = Field(default=3, ge=0)   # 0 disables the cap
```
Place near the other `vma_worker_*` settings.

### `app/runtime/work_queue.py`

**Do not change `lease_work`'s signature.** Enforce the cap inside `execute_work_item`, immediately after `execution_lease = _execution_lease(work)` and **before** `data["started_at"] = _utcnow_iso()`:

```python
attempt = int(data.get("attempt") or 0)
max_attempts = int(get_settings().vma_work_max_attempts)
if max_attempts > 0 and attempt > max_attempts:
    data["error"] = {
        "type": "max_attempts_exceeded",
        "message": f"Work item exceeded {max_attempts} execution attempts",
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    data["finished_at"] = _utcnow_iso()
    await res_q.update_resource(db, work, data=data, status="error")
    await _release_work_quota(db, work, actor_id=effective_worker_id)
    await db.commit()                       # tx1: touches ONLY the work row
    await _terminate_session_for_exhausted_work(
        session_id=str(data.get("session_id") or ""),
        organization_id=work.organization_id,
        work_id=work.id,
        attempt=attempt,
        max_attempts=max_attempts,
    )                                       # tx2: touches ONLY the session row
    return "exhausted"
```

New helper (module level). It must open its **own** `session_scope()` — two separate transactions is the deadlock-free design, not an accident:

```python
async def _terminate_session_for_exhausted_work(
    *, session_id: str, organization_id: str, work_id: str, attempt: int, max_attempts: int
) -> None:
    if not session_id:
        return
    async with session_scope() as db:
        session = await sessions_q.get_session(
            db, session_id, organization_id=resolve_organization_id(organization_id), for_update=True
        )
        if session is None or session.status == SESSION_TERMINATED:
            return
        details = dict(session.status_details or {})
        details.pop("execution_lease", None)
        stop_reason = {
            "type": "error",
            "error_type": "max_attempts_exceeded",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "work_id": work_id,
        }
        await sessions_q.update_session(
            db, session, status=SESSION_TERMINATED, status_details=details, stop_reason=stop_reason
        )
        await events_q.append_event(
            db, session,
            event_type="session.error",
            payload=session_error_payload(
                "Session turn exceeded the maximum number of execution attempts",
                error_type="max_attempts_exceeded",
                retry_status="terminal",
                attempt=attempt,
                max_attempts=max_attempts,
            ),
        )
        await events_q.append_event(
            db, session,
            event_type="session.status_terminated",
            payload={"type": "session.status_terminated", "status": SESSION_TERMINATED, "stop_reason": stop_reason},
        )
        await db.commit()
```

New imports in `work_queue.py`: `from app.db.queries import events as events_q`, `from app.session_errors import session_error_payload`. (`sessions_q`, `SESSION_TERMINATED`, `resolve_organization_id` are already imported.)

Semantics: `attempt` increments on every lease, so cap=3 allows three executions (original + two takeovers); the fourth lease is detected here and finalized without running. The exhausted row reaches `status="error"`, so `_lease_next_work` and `get_active_session_work` stop seeing it — no zombie rows.

Known, accepted gap: a crash between tx1 and tx2 leaves the work errored but the session unterminated; the session is not stuck (a later user turn enqueues fresh work). Document this in a code comment on the helper.

### P1.1 tests (`tests/test_work_queue.py` or a new file, mirroring existing patterns)
- Postgres-or-SQLite fixture: enqueue work, force-lease it 4 times (reuse the expired-lease manipulation pattern from the existing "expired lease recovery" test), call `execute_work_item` → returns `"exhausted"`; assert work `status=="error"` with `error.type=="max_attempts_exceeded"`, session `status=="terminated"` with matching `stop_reason`, a `session.error` and a `session.status_terminated` event appended, and the active-work governance counter released (same assertion style as existing quota tests).
- `vma_work_max_attempts=0` disables the cap (4th attempt still executes).

## P1.2 — Reuse the LangGraph checkpoint saver per process

Today every turn opens a fresh psycopg connection **and** runs `setup()` (migration check) against Supabase. Pool it.

### `pyproject.toml`
Change `psycopg[binary]` → `psycopg[binary,pool]` (verify with `python -c "import psycopg_pool"`).

### Config
```python
vma_checkpoint_pool_max_size: int = Field(default=5, ge=1)
```

### `app/runtime/checkpoints.py`
Keep the public API: `checkpoint_saver()` stays an `@asynccontextmanager` so `deepagents_engine.py:153` needs no change. Memory and SQLite branches stay exactly as they are (fresh per call — tests rely on this). Only the Postgres branch changes:

```python
_pg_saver = None          # module state
_pg_pool = None
_pg_dsn: str | None = None
_pg_lock = asyncio.Lock()

# postgres branch inside checkpoint_saver():
saver = await _shared_postgres_saver(_postgres_dsn(database_url))
yield saver               # no per-turn close

async def _shared_postgres_saver(dsn: str):
    global _pg_saver, _pg_pool, _pg_dsn
    if _pg_saver is not None and _pg_dsn == dsn:
        return _pg_saver
    async with _pg_lock:
        if _pg_saver is not None and _pg_dsn == dsn:
            return _pg_saver
        await close_checkpoint_saver()      # dsn changed (tests): drop old pool
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=int(get_settings().vma_checkpoint_pool_max_size),
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()                 # once per process
        _pg_pool, _pg_saver, _pg_dsn = pool, saver, dsn
    return _pg_saver

async def close_checkpoint_saver() -> None:
    global _pg_saver, _pg_pool, _pg_dsn
    pool, _pg_saver, _pg_pool, _pg_dsn = _pg_pool, None, None, None
    if pool is not None:
        await pool.close()
```

Critical detail: the connection kwargs (`autocommit=True`, `prepare_threshold=0`, `dict_row`) replicate what `AsyncPostgresSaver.from_conn_string` configures internally; omitting them breaks the saver subtly. Passing the **pool** (not a single connection) is what makes concurrent turns in one process safe.

Note: the module already has a function named `_postgres_dsn(value)`; keep it and avoid naming collisions with the new globals.

### `app/factory.py`
In the lifespan `finally` block, after stopping workers: `await close_checkpoint_saver()` (import from `app.runtime.checkpoints`; safe no-op when never opened).

### P1.2 tests
- Guarded by `VMA_TEST_POSTGRES_URL` (marker `postgres`, mirror `tests/test_postgres_governance.py`): enter `checkpoint_saver()` twice → same object; monkeypatch-count `AsyncPostgresSaver.setup` → called once; `close_checkpoint_saver()` then reopen → new instance.
- SQLite path: two calls still yield fresh, working savers (regression guard).

## P1.3 — Janitor advisory lock (one cleanup runner across instances)

`run_sandbox_janitor` runs in every instance; `cleanup_expired_session_sandboxes` (`app/runtime/sandbox_lifecycle.py`) would run N× concurrently after P2. Guard it:

```python
_JANITOR_LOCK_KEY = 0x564D414A  # arbitrary stable constant

async def cleanup_expired_session_sandboxes(*, limit: int = 25) -> int:
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return await _cleanup_expired_session_sandboxes(limit=limit)  # current body, renamed
    async with engine.connect() as conn:
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _JANITOR_LOCK_KEY})
        ).scalar()
        if not acquired:
            return 0
        try:
            return await _cleanup_expired_session_sandboxes(limit=limit)
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _JANITOR_LOCK_KEY})
```

Advisory locks are **connection-scoped**: lock and unlock must run on the same held connection (`engine.connect()` block), not through the pooled session helpers. `get_engine` is in `app/db/engine`.

### P1.4 — Harden the worker polling loop (small)

`run_worker` (`app/worker.py`) has no exception guard: one raised `WorkLeaseError` (more likely with multiple instances racing) or transient DB error kills the embedded worker coroutine silently. Wrap the loop body (`_next_runnable_work` + `execute_work_item`) in `try/except Exception`: log via `logger.exception("worker_loop_iteration_failed", ...)`, `await asyncio.sleep(poll_interval_seconds)`, continue. Preserve `once=True` behavior (let exceptions propagate when `once` is set, so tests keep failing loudly).

### P1.3/P1.4 tests
- Postgres-guarded: hold the advisory lock on a raw connection, call `cleanup_expired_session_sandboxes` → returns 0 without running (monkeypatch the inner body to record calls).
- Worker loop: make `lease_next_work_for_worker` raise once then succeed; `run_worker` (once=False, with stop_event) survives and executes the next item.

---

## P2 — Split into API and worker Cloud Run services

## P2.1 — Service role flag

### Config
```python
vma_service_role: Literal["combined", "api", "worker"] = "combined"
```
Default `combined` = today's behavior; local dev and the test suite must be zero-diff.

### `app/factory.py`
- Lifespan: start the janitor and embedded workers only when `settings.vma_service_role in {"combined", "worker"}` (existing gates — `vma_sandbox_provider=="e2b"`, `vma_embedded_worker_enabled` — still apply on top).
- `create_app()`: when role == `"worker"`, build a minimal app: same lifespan, `install_error_handlers`, the request-id middleware, and ONLY `/health` + `/health/db`. Skip all business routers, CORS, the public-GA middleware, and the OpenAPI customization. Extract the two health handlers into a helper so api/combined and worker share one definition.
- `scripts/export_openapi.py` runs with the default role and must produce an unchanged schema.

## P2.2 — Manifests

Create `service.worker.production.yaml` and `service.worker.staging.yaml` by copying the existing manifests, then apply this matrix. API manifests are edited in place:

| Setting | API prod | API staging | Worker prod | Worker staging |
|---|---|---|---|---|
| `metadata.name` | (keep) | (keep) | `<api-name>-worker` | `<staging-api-name>-worker` |
| `minScale` / `maxScale` | 1 / **3** | 1 / 2 | **2 / 3** | 1 / 1 |
| `containerConcurrency` | 40 (keep) | keep | **10** | 10 |
| `cpu-throttling` | `"false"` (keep) | keep | `"false"` | `"false"` |
| `VMA_SERVICE_ROLE` | **`api`** | `api` | **`worker`** | `worker` |
| `VMA_EMBEDDED_WORKER_ENABLED` | **`false`** | `false` | `true` | `true` |
| `VMA_WORKER_CONCURRENCY` / `VMA_WORKER_POLL_INTERVAL_SECONDS` / `VMA_WORKER_LEASE_SECONDS` | remove | remove | `5` / `0.5` / `120` | same |
| `VMA_WORK_MAX_ATTEMPTS` | — | — | `3` | `3` |
| `VMA_DB_POOL_SIZE` / `VMA_DB_MAX_OVERFLOW` | keep current | keep | **`5` / `2`** | `5` / `2` |
| Everything else (image, SA, secrets `vma-*`, probes, resources, `WEB_CONCURRENCY=1`, timeout) | unchanged | unchanged | copy from API | copy from staging API |

The worker service must never receive a public invoker binding: `scripts/gcloud/5-allow-public.sh` stays API-only — add a guard that exits with an error if pointed at a `*worker*` service.

## P2.3 — Deploy pipeline

- `cloudbuild.yaml` and `scripts/gcloud/2-deploy-production.sh` / `3-deploy-staging.sh`: keep the order *build → migrate Job (`--wait`) → replace API → replace worker*, same image tag for both replaces.
- `scripts/gcloud/status.sh` and `preflight.sh`: report/check both services and both manifests.
- `scripts/gcloud/README.md`: add a short scaling note — capacity = worker instances × `VMA_WORKER_CONCURRENCY`, manifests are the source of truth for instance counts — and link to the full operator runbook at `private-docs/scaling-runbook.md` (already written; do not duplicate its content).

## P2.4 — Test updates

`tests/test_cloud_run_config.py` pins the topology; update it in the same commit as the manifests:
- API manifest assertions: `maxScale: 3` (prod), `VMA_SERVICE_ROLE=api`, `VMA_EMBEDDED_WORKER_ENABLED=false`, worker-tuning env vars absent.
- New worker manifest assertion block: name suffix, `VMA_SERVICE_ROLE=worker`, embedded worker pins present, `minScale: 2` / `maxScale: 3` (prod), `containerConcurrency: 10`, `cpu-throttling: false`, identical `vma-*` secret set, probes present, `VMA_WORK_MAX_ATTEMPTS=3`, `VMA_DB_POOL_SIZE=5`.
- Migration-gate ordering: job execute `--wait` precedes **both** `services replace` invocations in cloudbuild and deploy scripts.
- `5-allow-public.sh` worker guard asserted.

New `tests/test_factory_roles.py`:
- role=`worker` → `/v1/agents` (and one more business route) return 404, `/health` returns 200, lifespan spawns embedded workers when enabled.
- role=`api` → business routes mounted, lifespan spawns no worker/janitor tasks even with `VMA_EMBEDDED_WORKER_ENABLED=true`.
- role=`combined` (default) → current behavior (routers + workers), guarding local/test parity.

## P2.5 — Cross-instance preview broker (Postgres `pg_notify`)

Preserves token-level `event_deltas` streaming after the split: workers publish preview frames through Postgres `NOTIFY`; API instances `LISTEN` and feed received frames into their local `vma_preview_bus`, so the SSE code path needs zero changes. `NOTIFY` is fire-and-forget, which matches the existing best-effort preview contract exactly — durable events still reconcile any dropped frame. Ship this in the same release as P2 so hosted typewriter streaming never regresses.

### Config
```python
vma_preview_broker: Literal["process_local", "pg_notify"] = "process_local"
```
Fail fast at lifespan startup if `pg_notify` is configured while `database_url` is not Postgres.

### New module `app/runtime/preview_broker.py`

Constants: `_CHANNEL = "vma_preview"`; `PREVIEW_INSTANCE_ID = uuid4().hex` (process identity); max payload 7500 bytes (Postgres `NOTIFY` caps at 8000).

**Publish path** — `async def publish_preview(session_id, frame, *, organization_id)`:
1. Always `await vma_preview_bus.publish(...)` first (local delivery: combined role and same-instance SSE clients).
2. When the broker is `pg_notify`, hand the frame to a coalescer instead of notifying per frame. A fast model can emit 50–100 chunks/sec per turn; raw per-chunk NOTIFY across 15 concurrent turns would hammer Supabase.

**Coalescer** (the one design-sensitive piece):
- Per `(organization_id, session_id)` FIFO buffer of pending frames; a single flusher task wakes every ~25 ms and flushes a session when its oldest pending frame is ≥50 ms old or its buffer exceeds 4 KB. 50 ms is imperceptible; this caps NOTIFY at ≤ ~20/sec per streaming turn.
- When flushing, merge only **adjacent** `event_delta` frames sharing the same `event_id` and delta index by concatenating `delta.content.text`. Never reorder frames within a session. An `event_start` frame flushes its session immediately (start must precede its deltas).
- Each flushed frame: `SELECT pg_notify(:channel, :payload)` on a connection from the existing SQLAlchemy engine (`app/db/engine`); payload JSON `{"i": PREVIEW_INSTANCE_ID, "o": org, "s": session, "f": frame}`. Drop oversized payloads whole (log at debug) — never truncate mid-JSON. Flush all pending buffers on shutdown.

**Listener** — runs only when broker is `pg_notify` AND `vma_service_role in {"combined", "api"}` (workers never serve SSE):
- Lifespan background task holding ONE dedicated `asyncpg` connection per process (asyncpg is already the production driver; strip the `+asyncpg` suffix from the SQLAlchemy URL). `conn.add_listener(_CHANNEL, cb)`; the callback schedules an async task that parses the payload, **drops frames where `payload["i"] == PREVIEW_INSTANCE_ID`** (loopback suppression — local publish already delivered those), and otherwise republishes into the local `vma_preview_bus` for that org/session topic.
- Reconnect forever with capped backoff (1s → 30s) on connection loss, logging each reconnect. Factory lifespan cancels the task and closes the connection on shutdown.
- Supavisor note: the production DSN is the **session-mode** pooler (port 5432), which supports LISTEN/NOTIFY; do not point the listener at a transaction-mode endpoint.

**Call-site change** (the only `runner.py` edit in this project): `emit_preview` in `run_session_turn` calls `publish_preview` from the new module instead of `publish_vma_preview`.

### Manifests / pipeline
- Add `VMA_PREVIEW_BROKER=pg_notify` to all four hosted manifests (API + worker, production + staging); assert the pin in `tests/test_cloud_run_config.py`.
- Connection budget: +1 dedicated LISTEN connection per API/combined instance (documented in `private-docs/scaling-runbook.md`); the publisher uses the existing engine pool.

### P2.5 tests
- Coalescer unit tests (SQLite fine, notify sink monkeypatched): merges adjacent same-event text deltas; never reorders within a session; `event_start` flushes immediately; 50 ms / 4 KB flush rules; oversized frames dropped whole.
- Loopback: a listener ignores frames carrying its own `PREVIEW_INSTANCE_ID`; same-process publish+subscribe delivers exactly once.
- Postgres-guarded end-to-end (`VMA_TEST_POSTGRES_URL`, marker `postgres`): publisher with instance id A sends a frame; a listener constructed with instance id B receives it through real NOTIFY and a local bus subscriber observes it.
- `tests/test_event_preview_streaming.py` passes untouched (default `process_local` is byte-identical to today).

---

## Verification (run all before handing back)

1. `pytest -q` — full suite green (SQLite default; fixtures set `DATABASE_URL` automatically).
2. Postgres-backed suites (needs a scratch DB whose name ends in `_test`):
   `VMA_TEST_POSTGRES_URL=postgresql+asyncpg://.../vma_test pytest -q -m postgres`
3. OpenAPI unchanged: the docs/schema tests (`test_public_openapi_schema.py`, `test_documentation_surface.py`) pass without snapshot edits.
4. Role smoke:
   - `VMA_SERVICE_ROLE=worker uvicorn votrix_managed_agents:create_app --factory --port 8081` → `/health` 200, `/v1/agents` 404.
   - `VMA_SERVICE_ROLE=api VMA_EMBEDDED_WORKER_ENABLED=true uvicorn ... --port 8082` → startup logs contain no `vma-embedded-worker` tasks.
   - Default (no env): existing local flow works — create a session, send a turn, inline execution completes (unchanged `should_execute_inline` behavior).
5. `bash scripts/gcloud/preflight.sh` passes (or its dry checks, if it requires gcloud auth, are documented as skipped).
6. Preview broker smoke (two processes, one shared Postgres): start a worker-role process and an api-role process with `VMA_PREVIEW_BROKER=pg_notify`; create a session and send a turn through the API process; `curl -N '.../v1/sessions/<id>/events/stream?event_deltas=agent.message'` against the API process shows incremental `event_delta` frames while the turn streams, followed by the complete durable `agent.message`.

## Acceptance checklist

- [ ] 4th lease of a failing work item → work `error/max_attempts_exceeded`, session terminated with `session.error` + `session.status_terminated` events, quota released; cap=0 disables.
- [ ] No transaction ever holds a work-row lock while acquiring a session-row lock (review the exhaustion path specifically).
- [ ] One `AsyncPostgresSaver` + one connection pool per process; `setup()` once; lifespan closes it; SQLite/memory paths untouched.
- [ ] Janitor runs at most once concurrently across connections; non-Postgres unaffected.
- [ ] Worker poll loop survives transient exceptions (`once=True` still propagates).
- [ ] `combined` role is byte-for-byte today's behavior; test suite needed no fixture changes for it.
- [ ] Worker app exposes only health endpoints; API app runs no background execution.
- [ ] Manifests match the matrix; `test_cloud_run_config.py` updated and green; migration job still gates both replaces; worker never gets public invoker.
- [ ] README runbook documents manual scaling; no new external infrastructure introduced anywhere.
- [ ] With `pg_notify`, an SSE client on instance X receives preview frames for a turn executed on instance Y; same-instance delivery is never duplicated (loopback suppression); worker role runs no listener; NOTIFY rate is bounded by coalescing.
- [ ] Public OpenAPI byte-identical; no changes under `app/runtime/deepagents_engine.py` or `vma_preview_bus.py`; `runner.py` changes limited to the P2.5 `emit_preview` import swap; lease logic untouched.
