---
title: Rate Limits and Quotas
description: Per-key request rate limits, Organization quotas, response headers, and 429 handling.
---

Two independent mechanisms protect the platform. Both answer with `429` and a
`rate_limit_error` envelope; the `error.code` and headers tell them apart.
**Response headers are always the authoritative source for current limits** —
the numbers below are package defaults, and hosted deployments configure their
own values.

## Per-key request rate limit

A fixed one-minute window per API key. Every response includes:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Requests allowed per window |
| `X-RateLimit-Remaining` | Requests left in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |
| `Retry-After` | Seconds to wait (present on `429`) |

Package default: 120 requests/minute. On `429` with
`error.code: rate_limit_exceeded`, wait for `Retry-After` and retry.

## Organization quotas

Enforced atomically in the control plane, independent of request rate:

| Quota | Package default | `error.code` on denial |
|---|---|---|
| Concurrently active work (queued + running turns) | 5 | `active_work_quota_exceeded` |
| Model tokens per day | 1,000,000 | `model_token_quota_exceeded` |
| Stored bytes (files, skills, outputs) | 5 GiB | `storage_quota_exceeded` |

Quota denials carry:

| Header | Meaning |
|---|---|
| `X-Quota-Metric` | Which quota denied the request |
| `X-Quota-Limit` / `X-Quota-Remaining` | Limit and headroom for that metric |
| `X-Quota-Reset` | Unix timestamp when the window resets (windowed quotas) |
| `Retry-After` | Seconds to wait, when a reset is known |

## Handling guidance

- Treat `429` as backpressure, not failure: queue the work client-side and
  retry after `Retry-After`.
- `active_work_quota_exceeded` means enough turns are already queued or
  running for your Organization — it clears as turns finish, so prefer
  waiting on your open event streams over hot retrying.
- `model_token_quota_exceeded` is a daily window; retrying before
  `X-Quota-Reset` will not succeed.
- `storage_quota_exceeded` requires deleting stored resources (or a limit
  increase); retrying is never sufficient.
- All submission endpoints accept an `Idempotency-Key`, so a retry after `429`
  can never double-apply a turn.
