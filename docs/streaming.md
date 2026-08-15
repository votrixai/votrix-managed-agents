---
title: Event Streaming
description: Follow Session events over SSE and resume after a disconnect.
---

Streaming is a live view of a Session's ordered event history. Every streamed
event can also be retrieved with
`GET /v1/sessions/{session_id}/events`.

## Endpoint

```text
GET /v1/sessions/{session_id}/events/stream
```

The response uses `text/event-stream`. An unknown or inaccessible Session
returns `404` before the stream opens.

## Event format

Each SSE frame uses the event sequence as `id`, the Session event type as
`event`, and the complete event object as JSON in `data`:

```text
id: 42
event: agent.message
data: {"id":"evt_...","type":"agent.message","session_id":"sess_...","seq":42,"content":[...]}
```

Lines beginning with `: keep-alive` carry no Session data and can be ignored.

## Choose where to start

Both cursors mean “send events with a sequence greater than this value”:

- `?after_seq=42`
- `Last-Event-ID: 42`

If both are supplied, `after_seq` wins. Without either cursor, the stream
replays the Session from its first event.

## Reconnect safely

1. Save the highest SSE `id` you have processed.
2. Reconnect with that value as `Last-Event-ID` or `after_seq`.
3. Ignore any event whose `seq` is not greater than your saved value.

The stream stays open across turns. It closes when the Session terminates, the
client disconnects, or the response reaches 30 minutes. Reconnect with your
last sequence to continue.

## Page the same history

```text
GET /v1/sessions/{session_id}/events?after_seq=42&limit=100
```

Events are returned oldest first. The response includes `has_more`, `last_id`,
and `last_event_seq` so a client can continue paging or switch to streaming.
