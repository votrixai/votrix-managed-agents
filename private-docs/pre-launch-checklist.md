# Pre-Launch Checklist

Internal only. This is the gate for the FIRST production deploy. It complements
the engineering handoffs — everything here is either an operator task or a
decision to record, ranked by (importance × how dangerous it is to fix after
launch). Launch requires: all Tier 1 items checked, plus the three engineering
plans landed (`PLAN-horizontal-scaling.md`, `PLAN-p3-autoscale.md`,
`PLAN-pre-launch-hardening.md`), plus the four load-test scenarios at the end
of `PLAN-p3-autoscale.md`.

## Tier 1 — unrecoverable if discovered after launch

### 1. Tenant isolation audit  (owner: Codex — `PLAN-pre-launch-hardening.md` W1)
- [ ] Denial matrix suite merged and green (every resource × direct-ID /
      pagination / streaming / sub-resource traversal / write-spoofing).
- [ ] Any failing cell fixed at the query layer and documented.

Why pre-launch: a cross-tenant leak after launch is a trust event that no fix
undoes; before launch it is a test-writing exercise.

### 2. Encryption key rotation  (owner: Codex — `PLAN-pre-launch-hardening.md` W2)
- [ ] Multi-key decrypt + re-encryption sweep merged.
- [ ] One rotation rehearsed end-to-end in staging (two secret versions, two
      deploys, sweep, drop old key).

Why pre-launch: today `vma_encryption_key` is a single key with no rotation
path (verified in `app/secret_cipher.py`); once customer BYOK credentials
accumulate, any key incident forces re-onboarding every customer.

### 3. Backup / PITR + restore drill  (owner: operator)
- [ ] PITR (or equivalent continuous backup) enabled on the **production**
      Supabase project; retention window recorded here: ______
- [ ] One real restore drill performed to a scratch project; time-to-restore
      recorded here: ______
- [ ] R2 bucket versioning/lifecycle decision recorded (files/skills objects).

Why pre-launch: Postgres is the sole source of truth (events, checkpoints,
work queue, vault). Nothing in the repo or scripts currently mentions backups.
"Backups exist" and "restore works" are different facts — drill it.

### 4. API contract freeze decisions  (owner: operator; record answers here)
- [ ] Versioning policy for breaking changes (how `anthropic-version` /
      `votrix-managed-agents-beta` headers evolve; when the beta header is
      retired): ______
- [ ] Event retention as a **product contract** (how far back clients may
      replay a Session's events). Recommendation: launch with "full history",
      revisit at volume; implementation (batched deletes vs partitioning) is
      deferred until a finite window is chosen: ______
- [ ] ID formats, event shapes, error envelopes reviewed once against the
      public OpenAPI — after the first external integration they are frozen
      forever.

### 5. Region / data residency  (owner: operator; record the decision)
- [ ] Confirm the stack's regions (Cloud Run `us-central1`, Supabase AWS
      region, R2, E2B) against target-customer expectations, or explicitly
      record "no residency commitment at launch": ______

Why pre-launch: moving regions later is a full data migration with downtime —
a one-way door in practice.

## Tier 2 — decide now, implement incrementally

- [ ] **Metering auditability**: if charging at launch, fix the billing grain
      and keep usage records append-only (usage attribution is already
      idempotent per work item). Post-launch metering fixes are revenue
      disputes.
- [ ] **Canonical API domain**: CORS currently mixes `votrix.ai` and
      `votrixai.com` roots. Pick the permanent API hostname before any client
      SDK configuration points at it: ______
- [ ] **Deletion semantics**: the schema soft-deletes (`deleted_at`); define
      the hard-delete path (DB rows + R2 objects + E2B teardown) for customer
      data-deletion requests before there is real customer data.
- [ ] **Supabase compute tier + connection-mode split**: production compute
      tier recorded in `scaling-runbook.md`; Amendment A1 (transaction pooler
      for runtime/checkpoints, session DSN for LISTEN/janitor/migrations)
      deployed; backend usage under ~60% of the tier's direct-connection
      limit at target `maxScale`.

## Explicitly NOT launch blockers

Multi-region HA, Redis, dashboard depth beyond queue-wait/error alerts,
RBAC/org membership (deferred in `TODO.md`), WAF, Postgres RLS (deferred —
defense in depth after the application-level audit).

## Launch gate summary

1. `PLAN-horizontal-scaling.md` (P1 + P2 + P2.5) — landed, suites green.
2. `PLAN-p3-autoscale.md` (Stage A + Stage B) — landed, suites green.
3. `PLAN-pre-launch-hardening.md` (W1 + W2) — landed, suites green.
4. Load-test gate: all four scenarios in `PLAN-p3-autoscale.md` pass; final
   `maxScale` recorded in `scaling-runbook.md`.
5. This checklist: Tier 1 fully checked, Tier 2 decisions recorded.
