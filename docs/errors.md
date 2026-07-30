---
title: Errors
description: The error envelope this service actually returns, what each status means, and how runtime failures surface as session events.
---

Two different things are called "errors" here and they behave differently:

- **Request errors** — the call was refused. Non-2xx, nothing was written.
- **Runtime failures** — the call was accepted and the *turn* went wrong. The
  request returns `200`; the failure arrives as a `session.error` event.

The second kind is the one that surprises people. Accepting a message and
running it are separate steps, so a message that really was accepted must not
come back as a failed request just because the agent later fell over.

## Envelope

Every non-2xx response raised by this service has one shape:

```json
{
  "error": {
    "type": "session_busy",
    "message": "The session is still working on the previous message."
  }
}
```

`error.type` is the machine-readable part; branch on it. `message` is for
humans and its wording may change.

Request-validation failures are produced by the framework before any handler
runs and use its own shape instead:

```json
{ "detail": [{ "loc": ["body", "events", 0, "content"], "msg": "...", "type": "..." }] }
```

## Statuses and types

| Status | `error.type` | When |
| --- | --- | --- |
| 404 | `not_found` | No such resource — or it belongs to another organization. The two are deliberately indistinguishable. |
| 409 | `conflict` | Well-formed but wrong for the current state: an interrupt sent with other events, a message to a terminated session. |
| 409 | `session_busy` | The session is mid-turn. There is no queue; nothing was appended. |
| 422 | *(framework shape)* | The request body does not match the schema. |
| 503 | `sandbox_unavailable` | The session has no usable container. |

### `session_busy` carries no retry hint

Deliberately. The running lease is renewed for as long as the worker lives, so
its remainder is not how long the turn has left — it is only how long a *dead*
worker would take to be noticed. A `Retry-After` built on that would be right
in the one case nobody cares about and wrong the rest of the time. Watch the
event stream and act on `session.status_idle` instead.

## `session.error` events

A turn that fails writes two events, in this order:

```json
{ "type": "session.error",       "error": { "type": "UnsupportedEventError", "message": "..." } }
{ "type": "session.status_idle", "stop_reason": { "type": "error" } }
```

The first says what went wrong. The second says the turn is over — and it is
the one to wait for, because a client waiting for `session.status_idle` must
never be left waiting by a turn that failed. The session stays usable; send the
next message whenever.

`error.type` here is the name of the underlying exception, not a stable API
code. Log it, show it, but do not branch on it.

### When the session cannot continue

If the container is gone, the session ends for good rather than returning to
idle:

```json
{ "type": "session.error",              "error": { "type": "sandbox_unavailable" } }
{ "type": "session.status_terminated" }
```

Every later message is refused with `409`. Start a new session.

## What is not implemented yet

The following are part of the intended surface and are **not** in the service
today. Nothing returns them; do not write client code that expects them.

- Authentication errors (`401`, `403`). `x-api-key` is accepted and not yet
  checked; `x-organization-id` is taken at face value.
- Rate limiting and quota errors (`429`), and the `X-RateLimit-*` /
  `X-Quota-*` headers.
- `error.code` — a stable machine code distinct from `error.type`.
- `request_id` in the body, and the `request-id` / `x-request-id` headers.
- `Idempotency-Key` on writes.
- Automatic retry of a failed turn, and the `retry_status` field that would
  describe it. A failed turn is final; retrying is the client's decision.
