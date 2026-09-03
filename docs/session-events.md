---
title: Session Events
description: Send work, follow progress, answer actions, and know when a turn stops.
---

Every Session has an ordered event history. You add user events; the Agent adds
messages, tool activity, and status events. List or stream the same history at
any time.

Every returned event includes an `id`, a per-Session `seq`, a `type`, and
`processed_at`. The `type` selects the remaining fields; there is no separate
payload wrapper.

## Event types

| Direction | Type | Purpose |
| --- | --- | --- |
| You → Agent | `user.message` | Send text or images and begin or continue work. |
| You → Agent | `user.interrupt` | Stop the current turn. |
| You → Agent | `user.tool_confirmation` | Allow or deny a tool action that needs approval. |
| You → Agent | `user.custom_tool_result` | Return the result of a custom tool. |
| Agent → You | `agent.message` | A user-facing response. |
| Agent → You | `agent.thinking` | A progress update. |
| Agent → You | `agent.tool_use` | A tool action and its permission decision. |
| Agent → You | `agent.custom_tool_use` | A request for your application to run a custom tool. |
| Agent → You | `agent.tool_result` | The result of a tool action. |
| Status | `session.status_running` | The Agent is working. |
| Status | `session.status_idle` | The current turn stopped. |
| Status | `session.status_terminated` | The Session cannot continue. |
| Status | `session.error` | The current turn encountered an error. |

## Send a message

```json
{
  "events": [
    {
      "type": "user.message",
      "content": [
        {"type": "text", "text": "Create a one-page project brief."}
      ]
    }
  ]
}
```

`POST /v1/sessions/{session_id}/events` accepts the message and starts a turn.
While the Agent is working, another message returns `409 session_busy` and is
not added to the event history. Wait for `session.status_idle` before sending
the next message.

Image input uses LangChain's standard image content blocks. Use `url` for a
public image:

```json
{
  "events": [{
    "type": "user.message",
    "content": [
      {"type": "text", "text": "What is in this image?"},
      {"type": "image", "url": "https://example.com/image.png"}
    ]
  }]
}
```

For private image bytes, use `base64` together with `mime_type`:

```json
{"type":"image","base64":"iVBORw0KGgo...","mime_type":"image/png"}
```

VMA passes these blocks to LangChain unchanged. Model support, format limits,
and size limits are enforced by the selected model provider. Images already in
the Session sandbox should be opened with the Agent's native `read_file` tool.

## Know why a turn stopped

Read `session.status_idle.stop_reason.type`:

| Value | Meaning |
| --- | --- |
| `end_turn` | The Agent finished the current turn. |
| `requires_action` | The Agent needs one or more answers from you. |
| `interrupted` | You stopped the turn. |
| `error` | The turn failed; a preceding `session.error` has details. |

## Answer requested actions

When `stop_reason.type` is `requires_action`, its `tool_use_ids` list is the
complete set of actions waiting for answers. Answer all of them in one request.
Each answer carries the matching tool-use ID, so the order of the answers does
not matter.

```json
{
  "events": [
    {
      "type": "user.tool_confirmation",
      "tool_use_id": "toolu_...",
      "result": "allow"
    },
    {
      "type": "user.custom_tool_result",
      "custom_tool_use_id": "toolu_...",
      "content": [
        {"type": "text", "text": "Customer tier: gold"}
      ]
    }
  ]
}
```

Return a custom tool failure with `is_error: true`. The Agent may explain the
failure, choose another approach, or request the tool again with a new ID, so
always match by ID rather than tool name.

## Collect output Files

The Agent should save deliverables under `outputs/` in its sandbox. Eligible
files there are collected when the turn stops and can be listed with:

```text
GET /v1/files?scope_id={session_id}
```

A file elsewhere in the sandbox is working material and does not become an
output File. Use `POST /v1/sessions/{session_id}/live/files` when you need to
capture an `outputs/` file before the turn finishes.

## Interrupt or terminate

`user.interrupt` is accepted while the Agent is working. The turn ends at its
next safe stopping point with `stop_reason.type: "interrupted"`.

After `session.status_terminated`, the Session cannot accept more work. Create
a new Session to continue.

See [Event Streaming](/docs/streaming) for reliable live delivery and resume
behavior.
