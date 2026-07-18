---
title: Event Streaming
description: Durable session event streams over SSE, resume cursors, and optional best-effort event deltas.
---

Session activity is an append-only event log. Streaming is a live view of that
log — the log is the source of truth, the stream is a delivery mechanism.

## Endpoints

```text
GET /v1/sessions/{session_id}/events/stream
GET /v1/sessions/{session_id}/stream                       # alias
GET /v1/sessions/{session_id}/threads/{thread_id}/stream   # thread-filtered
```

All three return `text/event-stream`. The thread variant rejects
`event_deltas` with `400` — deltas are only available on session-level streams.

## Wire format

Each durable event is one SSE message. The SSE `id` is the event's per-session
sequence number, `event` is the event type, and `data` is the full event
resource as JSON:

```text
id: 42
event: agent.message
data: {"id":"evt_...","type":"agent.message","seq":42,...}
```

Comment lines (`: ping`) are heartbeats; ignore them. If the Session does not
exist or is deleted, the stream emits one `event: error` message and closes.

## Cursors and resume

Two equivalent cursors select where the stream starts; both mean "events with
a sequence number strictly greater than the cursor":

- `Last-Event-ID: 42` request header. Because the SSE `id` is the sequence
  number, the browser `EventSource` reconnect behavior resumes correctly with
  no extra code. A non-numeric value is rejected with `400`.
- `?after_seq=42` query parameter. `after_seq=0` replays the full history.

If both are supplied, the larger value wins. **With no cursor at all, the
stream starts at the connection-time head**: you receive only events appended
after the stream opened. Open the stream before sending a turn, or pass an
explicit cursor.

The non-streaming equivalent is `GET /v1/sessions/{session_id}/events` with
the same `after_seq` parameter (pages of up to 1,000 events).

## Event deltas (optional preview)

Opt in to token-level previews of in-progress events:

```text
GET /v1/sessions/{id}/events/stream?event_deltas=agent.message
```

Both `event_deltas` and `event_deltas[]` spellings are accepted and repeatable.
Previewable types are `agent.message` and `agent.thinking`; any other value is
rejected with `400`. Two frame types interleave with durable events:

```text
event: event_start
data: {"type":"event_start","event":{"type":"agent.message","id":"evt_..."}}

event: event_delta
data: {"type":"event_delta","event_id":"evt_...","delta":{"type":"content_delta","index":0,"content":{"type":"text","text":"..."}}}
```

Delta frames carry **no SSE `id` line** — they never advance your cursor, and
they must never advance your application state either.

**Delivery contract: deltas are best-effort.** Frames may be coalesced,
dropped, or entirely absent for a given turn. The complete durable event
always follows and is the only authoritative record — deduplicate by event
`id` when you render previews. Build nothing that depends on a delta arriving.

## Reconnect pattern

```text
1. Track the last SSE id you processed (the durable seq).
2. On disconnect, reconnect with Last-Event-ID: <last seq>.
3. Treat any event you already processed (seq <= cursor) as a no-op.
```
