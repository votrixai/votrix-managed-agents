---
title: Errors
description: The error envelope this service actually returns, what each status means, and how runtime failures surface as session events.
---

Two ordinary things are called "errors" here and they behave differently:

- **Admission errors** — the call was refused before the turn was accepted.
  Non-2xx, nothing was written.
- **Runtime failures** — the call was accepted and the *turn* went wrong. The
  request returns `200`; the failure arrives as a `session.error` event.

The second kind is the one that surprises people. Accepting a message and
running it are separate steps, so a message that really was accepted must not
come back as a failed request just because the agent later fell over.

Cloud dispatch currently has one important exception. Input events and the
`running` lease are committed before Cloud Tasks enqueueing. If enqueueing
itself raises, the request can return an unhandled `500` even though the input
was written, and there is no outbox or reconciler to dispatch it afterward.
Inspect the Session/event log before retrying such a response.

## Envelope

Domain errors mapped by the service have this shape:

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

Explicit FastAPI errors, including an invalid Skill archive and internal
Cloud Tasks authentication failures, also use a `detail` body. An unhandled
exception may produce a plain `500` response.

## Statuses and types

| Status | `error.type` | When |
| --- | --- | --- |
| 404 | `not_found` | No such resource — or it belongs to another organization. The two are deliberately indistinguishable. |
| 409 | `conflict` | Well-formed but wrong for the current state: an interrupt sent with other events, a message to a terminated session. |
| 409 | `session_busy` | The session is mid-turn. There is no queue; nothing was appended. |
| 422 | *(framework `detail` shape)* | The request body does not match the schema, or a Skill archive is invalid. |
| 503 | `sandbox_unavailable` | The session has no usable container. |

### `session_busy` carries no retry hint

Deliberately. The running lease is renewed for as long as the worker lives, so
its remainder is not how long the turn has left — it is only how long a *dead*
worker would take to be noticed. A `Retry-After` built on that would be right
in the one case nobody cares about and wrong the rest of the time. Watch the
event stream for `session.status_idle`, then confirm the Session row is idle or
retry if the normal-turn cleanup window still returns `session_busy`.

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

### When the sandbox row says the Session cannot continue

If the sandbox row is missing or already marked failed/terminated, the Session
ends for good rather than returning to idle:

```json
{ "type": "session.error",              "error": { "type": "sandbox_unavailable" } }
{ "type": "session.status_terminated" }
```

Every later message is refused with `409`. Start a new session.

This terminal handling does not yet cover every remote failure. If the E2B
sandbox has disappeared but its database row still looks usable, lazy reconnect
raises inside the turn; the current generic failure path records
`session.error` plus `session.status_idle` and leaves the Session retryable.

## What is not implemented yet

The following are part of the intended public resource surface and are **not**
implemented there today. Do not write public clients that expect them; the
internal-callback exception is called out explicitly.

- Public-resource authentication errors (`401`, `403`). `x-api-key` is accepted
  and not yet checked; `x-organization-id` is taken at face value. The internal
  Cloud Tasks callback does use `401` and `403` for OIDC validation.
- Rate limiting and quota errors (`429`), and the `X-RateLimit-*` /
  `X-Quota-*` headers.
- `error.code` — a stable machine code distinct from `error.type`.
- `request_id` in the body, and the `request-id` / `x-request-id` headers.
- `Idempotency-Key` on writes.
- Automatic retry of a failed turn, and the `retry_status` field that would
  describe it. A failed turn is final; retrying is the client's decision.
