---
title: Event Streaming
description: Following a session's event log over SSE, and resuming a dropped connection without losing or repeating anything.
---

Session activity is an append-only event log. Streaming is a live view of that
log — the log is the source of truth, the stream is a delivery mechanism.
Everything the stream delivers can also be fetched with
`GET /v1/sessions/{session_id}/events`, and the two always agree.

What the events *mean* is in [Session Events](/docs/session-events). This page
is only about getting them.

## Endpoint

```text
GET /v1/sessions/{session_id}/events/stream
```

Returns `text/event-stream`. An unknown session fails the request with `404`
before the stream opens, rather than opening a connection that then says
nothing.

## Wire format

One SSE message per event. The SSE `id` is the event's per-session sequence
number, `event` is the event type, and `data` is the whole event as JSON:

```text
id: 42
event: agent.message
data: {"id":"evt_...","type":"agent.message","seq":42,"processed_at":"...","content":[...]}
```

Comment lines (`: keep-alive`) appear when the session has been quiet and carry
no meaning — ignore them. They exist because proxies close connections that go
silent, and a turn can think for minutes without emitting anything.

## Where the stream starts

Two cursors, both meaning "events with a sequence number strictly greater than
this":

- **`?after_seq=42`** — a query parameter.
- **`Last-Event-ID: 42`** — a request header. Because the SSE `id` *is* the
  sequence number, a browser `EventSource` sends this by itself after a
  dropped connection, and resumes correctly with no extra code.

If both are present, **`after_seq` wins** — it is the explicit one. A
`Last-Event-ID` that is not a number is ignored, and the stream starts from the
beginning: wasteful, but it can never silently skip events you have not seen.

**With no cursor at all, the stream replays the session from the start.** A
page that was refreshed gets the entire transcript from this one call. Pass
`after_seq` when you already have history and only want what is new.

## Lifetime

The stream stays open across turns. It closes when the session terminates, when
you disconnect, or after thirty minutes — at which point reconnect with the
last id you saw and you will miss nothing.

## Reconnect pattern

```text
1. Track the last SSE id you processed.
2. On disconnect, reconnect with Last-Event-ID: <that id>.
3. Treat any event with seq <= your cursor as a no-op.
```

Step 3 costs nothing and makes the client correct even if a proxy or a
framework replays a frame.

## Paging the same log

```text
GET /v1/sessions/{session_id}/events?after_seq=42&limit=100
```

Oldest first, up to 1,000 per page (20 by default), with `has_more` and
`last_id` for paging onward. A client that tracks `last_event_seq` from the
session resource can ask for what it has not seen without having seen the last
event.
