# Amendment A1 — Supavisor Connection-Mode Split

Standalone amendment to `PLAN-horizontal-scaling.md` and `PLAN-p3-autoscale.md`.
The base documents are intentionally left untouched so an executor mid-flight
only needs to read THIS file. **Where this file conflicts with the base
documents, this file wins.** Apply these deltas in whichever commit implements
the section they amend.

## Why

Supavisor **session mode pins one Postgres backend connection per client** for
the client's lifetime, and backend (direct) connections — not pooler clients —
are the scarce Supabase resource (Micro 60, Small 90, Medium 120, Large 160,
XL 240 direct vs 200–1,000 pooler clients). An autoscaling fleet must not
consume backends linearly, so the current "everything on session mode :5432"
convention cannot ship. Runtime traffic moves to the **transaction pooler**;
only session-scoped Postgres features keep session-mode connections.

Verified already-compatible: `app/db/engine.py` sets `statement_cache_size=0`
and `prepared_statement_cache_size=0`, so the main asyncpg engine works on the
transaction pooler as-is.

## DSN topology (hosted; local/test unchanged)

| Purpose | Mode | Setting |
|---|---|---|
| Main engine (API, work queue, sessions/events CRUD, `pg_notify` publish) | transaction `:6543` | `DATABASE_URL` |
| LangGraph checkpoint pool | transaction `:6543` | `VMA_CHECKPOINT_DATABASE_URL` (now set explicitly in manifests) |
| Preview LISTEN + janitor advisory lock | session `:5432` | **new** `vma_listen_database_url` (empty → falls back to `database_url`) |
| Alembic migration Job | session/direct `:5432` | **new secret** `vma-database-url-direct(-staging)` injected as the Job's `DATABASE_URL` — pipeline-only change (`1-create-secrets.sh`, deploy scripts, `cloudbuild.yaml`); no code change |

`pg_notify` publishing through the transaction pooler is safe (NOTIFY fires on
commit; delivery is server-global regardless of issuing backend). LISTEN and
session-scoped advisory locks are NOT safe there — hence the dedicated session
DSN.

## Deltas to `PLAN-horizontal-scaling.md`

1. **Config (new setting):** `vma_listen_database_url: str = ""` in
   `app/config.py`.

2. **P1.2 checkpoint pool kwargs** — replace `"prepare_threshold": 0` with
   `"prepare_threshold": None`. Rationale: psycopg3's `0` means "prepare
   immediately"; server-side prepared statements are unsafe through the
   transaction pooler. `autocommit=True` and `dict_row` stay. The base doc's
   sentence "replicates what `from_conn_string` configures" now applies only
   to those two kwargs — the divergence on `prepare_threshold` is deliberate.

3. **P1.3 janitor advisory lock** — the lock/unlock connection MUST come from
   the `vma_listen_database_url` engine (session mode), not `get_engine()`'s
   main engine: a session-scoped advisory lock acquired through the
   transaction pooler lands on an arbitrary shared backend and mutual
   exclusion silently stops working. Add a small shared helper (e.g.
   `session_scoped_connection()`) used by both the janitor and the P2.5
   listener; it falls back to `database_url` when the setting is empty
   (local/test behavior unchanged).

4. **P2.2 manifest matrix** — pool pins change to:

   | | API (prod & staging) | Worker (prod & staging) |
   |---|---|---|
   | `VMA_DB_POOL_SIZE` / `VMA_DB_MAX_OVERFLOW` | 4 / 2 | 4 / 1 |
   | `VMA_CHECKPOINT_POOL_MAX_SIZE` | — | 3 |
   | `VMA_LISTEN_DATABASE_URL` | session-mode secret ref | session-mode secret ref |

   Worker 4+1 against 5 concurrent turns is deliberately tight: transaction-
   mode checkouts are per-statement-burst. The staging load test MUST watch
   SQLAlchemy pool-wait metrics; this is the first knob to raise on contention.

5. **P2.5 listener** — the dedicated asyncpg LISTEN connection connects to
   `vma_listen_database_url` (strip the `+asyncpg` suffix), not the main DSN.

6. **Docs touched by implementation:** update the "session mode (:5432)"
   guidance in `.env.production.example`, `.env.staging.example`, and
   `scripts/gcloud/README.md` — they currently instruct putting ALL traffic on
   5432.

## Deltas to `PLAN-p3-autoscale.md`

7. **B.6 queue config** — `maxConcurrentDispatches` starts at **≈25** (not
   ≈100). It is the third backpressure layer alongside worker `maxScale` and
   the per-instance turn limiter, and rises only with the connection budget in
   `private-docs/scaling-runbook.md`.

## Tests (add to the respective sections' suites)

- `tests/test_cloud_run_config.py`: pins for the new pool values,
  `VMA_LISTEN_DATABASE_URL` on all hosted manifests, and the migration Job's
  `vma-database-url-direct` secret.
- Unit: janitor and listener resolve the session DSN with correct fallback to
  `database_url` when unset.

## Acceptance additions

- [ ] Runtime + checkpoint traffic runs on the transaction pooler; LISTEN and
      the janitor lock run on the session DSN; the migration Job uses its own
      direct secret.
- [ ] `prepare_threshold=None` on the checkpoint pool; no server-side prepared
      statements cross the transaction pooler.
- [ ] Pool pins match this amendment's table; env examples and gcloud README
      no longer instruct all-traffic-on-5432.
