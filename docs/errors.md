---
title: Errors
description: Handle HTTP errors and failures reported through Session events.
---

VMA reports failures in two places:

- **HTTP errors** mean the request was not accepted.
- **`session.error` events** mean a message was accepted, but the Agent could
  not complete the turn.

## HTTP error shape

Most API errors use:

```json
{
  "error": {
    "type": "session_busy",
    "message": "The session is still working on the previous message."
  }
}
```

Use `error.type` for program logic. Treat `message` as text for logs or users;
its wording may change.

Authentication and schema-validation responses use a `detail` field instead.

## Common statuses

| Status | Type | Meaning |
| --- | --- | --- |
| `400` | `invalid_request_error` | The request conflicts with an endpoint rule. |
| `401` | `detail` response | The API key is missing or invalid. |
| `404` | `not_found` | The resource does not exist or is outside the key's Organization. |
| `409` | `conflict` | The resource cannot perform that action in its current state. |
| `409` | `session_busy` | The Session is already working; the new message was not accepted. |
| `413` | `request_too_large` | An uploaded payload exceeds its limit. |
| `422` | `detail` response | A field is missing, malformed, or outside its allowed range. |
| `503` | `sandbox_unavailable` | The Session sandbox is unavailable. |
| `503` | `memory_store_unavailable` | The requested Memory Store operation is temporarily unavailable. |

## Failures after a message is accepted

An Agent turn that fails reports:

```json
{"type":"session.error","error":{"type":"...","message":"..."}}
{"type":"session.status_idle","stop_reason":{"type":"error"}}
```

Wait for `session.status_idle` before deciding that the turn is over. The
Session remains usable after an ordinary turn error, so you may correct the
input or retry with a new message.

If the error is followed by `session.status_terminated`, that Session cannot
continue. Start a new Session instead.

## Retry guidance

- Retry temporary network failures with exponential backoff.
- Reuse `idempotency_key` when retrying Account creation.
- Do not immediately retry `session_busy`; wait for a status event and then
  check the Session again.
- Before retrying an uncertain write, retrieve the resource or event history
  to see whether the first request succeeded.
