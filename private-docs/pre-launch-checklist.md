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
path (verified in `app/secret_cipher.py`); once Organization BYOK credentials
accumulate, any key incident forces re-onboarding every affected Organization.

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
- [ ] Confirm the stack's paired regions (production Cloud Run `us-east4` with
      Supabase AWS `us-east-1`; staging Cloud Run `us-west2` with Supabase AWS
      `us-west-1`; plus R2 and E2B) against target-account requirements, or
      explicitly record "no residency commitment at launch": ______

Why pre-launch: moving the database region later is a data migration. Moving
the stateless Cloud Run services is a separate regional rollout and origin
cutover, so keep the deployment matrix explicit.

## Tier 2 — decide now, implement incrementally

- [ ] **Metering auditability**: if charging at launch, fix the billing grain
      and keep usage records append-only (usage attribution is already
      idempotent per work item). Post-launch metering fixes are revenue
      disputes.
- [x] **Canonical domains** (decided and cut over 2026-07-19; status, exact path allowlist,
      certificate rules, and ordered cutover are in
      `private-docs/domains.md`): `api.vma.votrixai.com` is the production API,
      `vma.votrixai.com` is the builder frontend,
      `docs.vma.votrixai.com` is VMA documentation, and
      `staging-api.vma.votrixai.com` / `staging.vma.votrixai.com` mirror the
      API/frontend split. Bare `api.votrixai.com` and `docs.votrixai.com`
      remain reserved for the umbrella/main product. The Cloud Run `run.app`
      URL remains the operator entry behind superadmin JWT; `admin.vma` exists
      only if the complete Access + admin route + origin-cloaking bundle ships.
      - [ ] Complete the remaining operator acceptance item in
            `private-docs/domains.md`: authenticated browser/SDK/SSE and
            real-turn checks on the permanent domains.
- [ ] **Deletion semantics**: the schema soft-deletes (`deleted_at`); define
      the hard-delete path (DB rows + R2 objects + E2B teardown) for Organization
      data-deletion requests before there is real Organization data.

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
