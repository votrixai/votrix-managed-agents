# Superadmin Organization Onboarding SOP

Internal only. Do not copy this runbook into the public documentation tree.

## Preconditions

- The VMA database is migrated to the latest Alembic revision.
- VMA has `VMA_SUPABASE_URL` and `VMA_SUPABASE_PUBLISHABLE_KEY` configured.
- The operator signs in through Supabase with `app_metadata.super_admin = true`.
- The future owner has signed in at least once so their Supabase user UUID is known.

Use the superadmin's Supabase access token in the examples below. Never paste it into tickets, logs, or committed files.

```bash
export VMA_ADMIN_TOKEN="<short-lived Supabase access token>"
export VMA_BASE_URL="https://<private-or-production-vma-host>"
```

## 1. Create the Organization

Organization IDs must begin with `org_`. IDs and slugs are permanent identifiers; choose them deliberately.

```bash
curl -sS -X POST "$VMA_BASE_URL/internal/organizations" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "org_acme",
    "slug": "acme",
    "name": "Acme, Inc.",
    "metadata": {"organization_tier": "pilot"}
  }'
```

A `409` means the ID or slug already exists. Inspect the existing record rather than retrying with altered identifiers blindly.

## 2. Grant one or more owners

Repeat this request for every owner. Multiple owners are supported. Only a superadmin can add or remove owners in the first release.

```bash
curl -sS -X POST "$VMA_BASE_URL/internal/organizations/org_acme/owners" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "<Supabase user UUID>",
    "email": "owner@acme.example"
  }'
```

Confirm the complete owner list:

```bash
curl -sS "$VMA_BASE_URL/internal/organizations/org_acme/owners" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN"
```

The email is an operator-facing snapshot; authorization is based exclusively on `user_id`.

## 3. Create an Organization API key when required

Builder owners use Supabase identity and do not receive this key. Create an API key only for server-to-server workloads or an approved API consumer integration.

```bash
curl -sS -X POST "$VMA_BASE_URL/internal/organizations/org_acme/api-keys" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme production integration",
    "scopes": ["api"]
  }'
```

The plaintext `secret` is returned exactly once. Transfer it directly into the approved secret manager and remove it from shell history/output. Do not give `api_keys:manage` or `worker` unless the integration explicitly requires it.

List keys (only safe metadata is returned):

```bash
curl -sS "$VMA_BASE_URL/internal/organizations/org_acme/api-keys" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN"
```

Rotate a key and immediately store the new one-time `secret`:

```bash
curl -sS -X POST \
  "$VMA_BASE_URL/internal/organizations/org_acme/api-keys/<key_id>/rotate" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN"
```

Revoke a key that is no longer required:

```bash
curl -sS -X POST \
  "$VMA_BASE_URL/internal/organizations/org_acme/api-keys/<key_id>/revoke" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Integration retired"}'
```

For the initial deployment bootstrap, or if hosted superadmin authentication is not yet available, use `scripts/bootstrap_api_key.py` from a trusted operator environment. That path creates an Organization management key and must not be used by end users.

## 4. Verify owner access

Ask the owner to sign into the VMA Developer Console. Expected behavior:

- No organization creation option is shown.
- The new Organization appears in the switcher.
- The owner can manage Agents, Environments, Vaults, Skills, Files, and Sessions.
- API key management returns `403` for owner credentials.

The equivalent API verification uses the owner's Supabase access token:

```bash
curl -sS "$VMA_BASE_URL/v1/agents" \
  -H "Authorization: Bearer <owner access token>" \
  -H "X-Organization-Id: org_acme" \
  -H "votrix-managed-agents-beta: votrix-managed-agents-2026-04-01"
```

## 5. Remove owner access

```bash
curl -sS -X DELETE \
  "$VMA_BASE_URL/internal/organizations/org_acme/owners/<Supabase user UUID>" \
  -H "Authorization: Bearer $VMA_ADMIN_TOKEN"
```

An Organization may have zero owners while being provisioned or suspended. Removing an owner does not revoke separate Organization API keys; review those independently.

## Incident and offboarding checklist

1. Remove affected owner records.
2. Revoke or rotate any Organization API keys that may have been exposed through the authenticated `/v1/api_keys` lifecycle API or the trusted bootstrap procedure.
3. Review the Organization audit ledger for the affected window.
4. Archive the Organization only after confirming active work and retained data requirements.
