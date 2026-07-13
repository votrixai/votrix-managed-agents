# VMA TODO

## Multi-tenant API keys

Status: deferred until the core local control-plane, Skill, Session, model, and
E2B flows run end to end.

### MVP tenancy decision

- Treat the existing `Workspace` as the tenant and Organization boundary.
- Do not add a separate Organization-to-Workspace hierarchy yet.
- An Organization/Workspace may own multiple API keys for different callers,
  such as a production backend, developer laptop, CI, and third-party
  integration.
- Keep `VMA_API_KEY` only for local development, bootstrap, or break-glass use.
  Staging and production should ultimately authenticate with database-backed
  workspace keys or a trusted backend-issued identity.

### Existing foundations

- `workspaces` and `api_keys` database tables exist.
- API keys are stored as SHA-256 hashes; plaintext is returned only at creation.
- API key records already bind to `workspace_id`, expose a non-secret prefix,
  track `last_used_at`, and support archival.
- `DatabaseApiKeyAuthProvider` resolves a key to request workspace context.
- Core resources already carry `workspace_id` in the database.
- R2 object keys and E2B ownership include the workspace boundary.

### Required implementation

- [ ] Add an Alembic migration for API key authorization and lifecycle fields:
  - `scopes`
  - `expires_at`
  - `created_by`
  - explicit revocation metadata if archival is insufficient
- [ ] Make database-backed authentication the default for staging and
  production while preserving local anonymous/bootstrap behavior.
- [ ] Add authenticated API key management endpoints:
  - `POST /v1/api_keys`
  - `GET /v1/api_keys`
  - `GET /v1/api_keys/{api_key_id}`
  - `DELETE /v1/api_keys/{api_key_id}`
  - `POST /v1/api_keys/{api_key_id}/rotate`
- [ ] Return a newly generated plaintext key exactly once and never persist or
  log it.
- [ ] Start with a small scope model (`read`, `write`, `admin`) and expand to
  resource-specific scopes only when product requirements justify it.
- [ ] Enforce expiration, revocation, and scopes in request dependencies.
- [ ] Ensure callers cannot select a tenant with an untrusted
  `X-Workspace-ID`-style header.
- [ ] Define a secure bootstrap path for creating the first workspace admin key
  without leaving a permanent global production key.
- [ ] Add audit events for key creation, use, rotation, revocation, and failed
  authentication without recording plaintext credentials.

### Isolation audit and tests

- [ ] Add a two-workspace denial matrix proving Workspace A cannot read,
  mutate, stream, or delete Workspace B resources.
- [ ] Audit ID-based lookup, pagination, and background execution paths for:
  - Agents and Agent versions
  - Environments
  - Sessions, events, previews, and checkpoints
  - Files, Skills, and R2 object keys
  - Memory stores and Vaults
  - Deployments, scheduled runs, and workers
  - Webhooks
  - E2B sandbox bindings and cleanup
- [ ] Verify archived and expired keys receive `401` and insufficient scopes
  receive `403`.
- [ ] Verify key rotation does not interrupt unrelated keys belonging to the
  same workspace.
- [ ] Consider PostgreSQL row-level security as defense in depth after the
  application-level workspace audit is complete.

### Possible hosted identity path

If all customer traffic passes through `votrix-backend`, consider accepting a
short-lived backend-signed JWT containing `workspace_id`, audience, scopes, and
expiry instead of storing one long-lived backend API key per Organization.
Direct Claude-compatible SDK users can continue to receive workspace-scoped API
keys. Do not trust an unsigned tenant identifier forwarded by another service.

### Explicitly deferred

- Separate Organization and Workspace tables.
- Multiple Workspaces under one Organization.
- Organization membership and human-user RBAC.
- Cross-workspace Organization administrator roles.
- Billing and quota ownership at the Organization level.
- Automated migration of existing tenants into a future two-level hierarchy.

### Acceptance criteria

- Production can run without a permanent global `VMA_API_KEY`.
- Every authenticated request resolves exactly one trusted workspace context.
- Every API key belongs to one workspace; a workspace can own many keys.
- Plaintext API keys are shown once, hashed at rest, redacted from logs, and
  independently revocable.
- Cross-workspace access tests cover all durable and external resources.
- Existing Claude-compatible API and SDK request shapes remain unchanged.

