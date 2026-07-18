---
title: Errors
description: The error envelope, stable error codes, request correlation, and session.error semantics.
---

## Envelope

Every non-2xx response body has one shape:

```json
{
  "type": "error",
  "error": {
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded",
    "message": "Human-readable description",
    "request_id": "req_..."
  },
  "request_id": "req_..."
}
```

`error.type` is the coarse Anthropic-compatible category; `error.code` is the
stable machine code to branch on. `message` wording may change; types and
codes will not.

## Types and codes by status

| Status | `error.type` | Default `error.code` |
|---|---|---|
| 400 | `invalid_request_error` | `invalid_request` |
| 401 | `authentication_error` | `authentication_failed` |
| 403 | `permission_error` | `permission_denied` |
| 404 | `not_found_error` | `resource_not_found`, `capability_not_available` |
| 409 | `conflict_error` | `resource_conflict` |
| 422 | `invalid_request_error` | `validation_failed` |
| 429 | `rate_limit_error` | `rate_limit_exceeded`, `active_work_quota_exceeded`, `model_token_quota_exceeded`, `storage_quota_exceeded`, `organization_quota_exceeded` |
| 5xx | `api_error` | `internal_error` |

Notes:

- A `404` on a resource ID you did not create is deliberate non-disclosure:
  resources belonging to another Organization are indistinguishable from
  resources that do not exist.
- The five `429` codes distinguish the per-key request rate limit from the
  Organization quotas — see [Rate limits and quotas](./rate-limits.md).

## Request correlation

Every response carries `request-id` and `x-request-id` headers (echoing yours
if you sent one). Include the ID when reporting issues; it correlates with
server-side audit records.

## `session.error` events

Runtime failures inside a Session surface as durable `session.error` events,
not HTTP errors. The payload carries a nested `error` object (the same
SDK-facing contract as HTTP errors) plus `retry_status`:

- `"retrying"` — a transient failure; VMA reschedules the turn automatically
  (a `session.status_rescheduled` event follows with the retry time). No
  client action is needed.
- `"terminal"` — the Session is terminated (a `session.status_terminated`
  event follows with the `stop_reason`). Start a new Session or resume.

## Retry guidance

| Situation | What to do |
|---|---|
| `429` | Back off per `Retry-After` / the rate-limit headers, then retry. |
| `5xx` | Safe to retry; use an `Idempotency-Key` on writes so replays are exact. |
| `409` | Read the current resource state first — the conflict is semantic, not transient. |
| `session.error` with `retrying` | Do nothing; keep the event stream open. |
