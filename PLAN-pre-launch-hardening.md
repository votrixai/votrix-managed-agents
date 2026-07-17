# VMA Pre-Launch Hardening — Implementation Handoff (W1 + W2)

Self-contained spec for a coding agent. Companion to `PLAN-horizontal-scaling.md` and `PLAN-p3-autoscale.md`, but **independent of both**: these two workstreams touch neither the work queue nor dispatch and can be built in parallel with, before, or after the scaling sequence — the only constraint is that both MUST be merged and green before the first production deploy (see `private-docs/pre-launch-checklist.md` for the full launch gate). Keep W1 and W2 as separate commits.

Rationale: these close the two pre-launch gaps whose post-launch failure modes are unrecoverable — a cross-tenant data leak (trust), and an encryption key that cannot be rotated once Organization BYOK credentials accumulate (forced re-onboarding of every affected Organization on any key incident).

---

## W1 — Tenant isolation denial matrix (~2–3 days)

Turns the unchecked `TODO.md` items ("two-Organization denial matrix", "audit ID-based lookup, pagination, and background execution paths") into an executable test suite, and fixes anything it catches.

### Deliverable: `tests/test_tenant_isolation_matrix.py`

Fixtures: two Organizations (`org_iso_a`, `org_iso_b`), each with its own API key, created through the same helpers existing tests use (see `tests/conftest.py` and the organization fixtures in existing router tests). Org A creates one instance of every resource; org B attacks it.

**Resource axis** (matrix rows — mirror the `TODO.md` list):
- Agents and Agent versions
- Environments
- Sessions, Session events, event streams (SSE), previews, runtime checkpoints
- Files (including presign/upload/download paths) and Skills (archives)
- Memory stores and Vault credentials
- Work items (`environment_work`) via the worker/poll routes
- Webhooks
- E2B sandbox bindings (creation is sealed at Session create; attack via Session routes)

**Attack axis** (matrix columns — every row × every applicable column):
1. Direct ID access: B issues GET/PATCH/DELETE against A's resource ID.
2. List/pagination: B's list endpoints must never return A's rows, including with attacker-supplied cursors/filters (a stolen pagination cursor from A's session must not leak when replayed by B).
3. Streaming: B opens A's `/v1/sessions/{id}/events/stream` and thread-stream variants.
4. Sub-resource traversal: B references A's resource as a parameter of B's own operations (e.g., B creates a Session pointing at A's agent_id/environment_id; B attaches A's file_id/skill_id; B's session resources.add with A's file).
5. Write-side spoofing: B submits events/tool results/idempotency keys against A's session; B's work ack/heartbeat/stop against A's work item (extends the existing anti-spoofing tests in `tests/test_work_queue.py:450-508`).

**Expected behavior**: assert the repo's existing non-disclosure convention — cross-tenant access must be indistinguishable from "does not exist" (404, not 403), so IDs are not confirmable by probing. First, determine the convention from one known-good path (e.g., sessions router); then enforce it uniformly. Any endpoint that returns 403 (existence leak) or 200 (actual leak) is a finding.

**Rules:**
- Table-driven: one parameterized matrix, not hundreds of hand-written tests; each cell failure must name resource × attack.
- Any failing cell is a real vulnerability: fix it in the same workstream (scope query by `organization_id` at the query layer, following the pattern in `app/db/queries/*`), and note it in the commit message. Do not weaken an assertion to make the matrix pass.
- Also add one background-path test: a queued work item for org A executed by the trusted global worker must attribute usage/quota/events only to org A even when the poller runs without request context (extends the existing cross-tenant lease test).
- RLS (Postgres row-level security) stays deferred per `TODO.md` — do not add it here.

---

## W2 — Encryption key rotation (~1 day)

### Current state (verified)

`app/secret_cipher.py` encrypts Vault credential values (Organization BYOK model keys) with a single `vma_encryption_key` (AES-GCM, `enc:v1:` prefix, nonce+ciphertext base64). There is no multi-key support: rotating the key today makes every stored credential undecryptable. `vma_allow_plaintext_secrets_local` governs the local plaintext fallback and stays unchanged.

### Changes

**Config (`app/config.py`):**
```python
vma_encryption_keys_previous: Annotated[list[str], NoDecode] = Field(default_factory=list)
```
Comma-separated env parsing via a `field_validator`, same pattern as `vma_cors_origins`. `vma_encryption_key` remains the sole write key.

**`app/secret_cipher.py`:**
- `decrypt_secret`: try the primary key first, then each previous key in order. AES-GCM's auth tag makes trial decryption safe — a wrong key fails authentication, it cannot silently return garbage. Keep the `enc:v1:` wire format unchanged (no data migration, no new format to dual-read). Fail closed (raise, as today) only when every key fails.
- `encrypt_secret`: unchanged — always the primary key.

**Re-encryption sweep — `scripts/reencrypt_secrets.py`:**
- Locates every encrypted-at-rest value (grep the write sites of `encrypt_secret_values`; Vault credential rows are the known store) and rewrites each with the primary key, in batches, idempotent (values already decryptable by the primary key are skipped), logging counts only — never values.
- Exit non-zero if any row is undecryptable by all configured keys (that row predates the oldest configured key — surface it, do not delete).

**Rotation runbook** — add a short section to `private-docs/scaling-runbook.md`'s sibling doc or `private-docs/pre-launch-checklist.md` (checklist already stubs it): generate new key → set as `VMA_ENCRYPTION_KEY`, move old into `VMA_ENCRYPTION_KEYS_PREVIOUS` → deploy → run sweep → remove old key from previous list → deploy. Secrets flow through Secret Manager (`vma-encryption-key`), so this is two secret versions and two deploys.

### W2 tests
- Round-trip with rotated keys: encrypt under key1; configure key2 primary + key1 previous → decrypt succeeds; sweep re-encrypts; after dropping key1, value still decrypts (now under key2).
- Wrong/absent keys fail closed; plaintext-local mode unchanged; `enc:v1:` strings produced before the change remain readable.
- Sweep idempotency (second run touches zero rows) and undecryptable-row detection.

---

## Acceptance checklist

- [ ] Isolation matrix covers every resource row × applicable attack column; all cells green with the 404 non-disclosure convention; any fixes applied at the query layer and named in the commit.
- [ ] Stolen-cursor pagination replay and sub-resource traversal (A's IDs inside B's create calls) are explicitly covered.
- [ ] Multi-key decrypt: credentials written before a rotation remain readable through the previous-keys list; write path always uses the primary key; wire format unchanged.
- [ ] Re-encryption sweep is idempotent, batch-safe, logs no secret material, and fails loudly on undecryptable rows.
- [ ] No public API/OpenAPI change; no schema migration; existing suites untouched and green.
