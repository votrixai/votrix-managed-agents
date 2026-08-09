---
title: Session Events
description: The thirteen session event types, the four a client can send, and the rules that are not visible in the schema.
---

A session is an append-only log of events. You append; the agent appends; both
of you read the same log back. The OpenAPI schema tells you what each event
*looks like*. This page is about the part a schema cannot express — when the
agent stops, what it is waiting for, and what it does with your answer.

Every rule below is covered by a test in `tests_live/`, run against a real
model in a real container.

## The eight rules

| Rule | What goes wrong without it |
| --- | --- |
| **A batch stops as a batch** | A call that needed no permission is waiting too. Its result cannot arrive until you answer the ones that did. |
| **Answer everything at once** | A partial answer fails inside the turn, not at the request. |
| **Answer in any order** | Each answer names its call, so order is yours to choose. |
| **One id, end to end** | The same `tool_use_id` string appears in four places. Nothing is translated. |
| **`is_error` invites a retry** | A failed tool result reaches the model as a failure, and a model told its tool failed will usually try again. |
| **Deliverables live in `outputs/`** | A file written anywhere else is real, readable by the agent, and never collected. |
| **An interrupt is not instant** | Cancellation lands between the model's steps, not inside one. |
| **Idle marks the graph stop** | Output collection and the final row release currently happen just after the event is committed. |

The rest of this page is those eight, one at a time.

## The events

What you send:

| Type | Fields |
| --- | --- |
| `user.message` | `content[]` |
| `user.interrupt` | — |
| `user.tool_confirmation` | `tool_use_id`, `result`, `deny_message?` |
| `user.custom_tool_result` | `custom_tool_use_id`, `content[]`, `is_error?` |

What the agent produces:

| Type | Fields |
| --- | --- |
| `agent.message` | `content[]` |
| `agent.thinking` | `content[]` |
| `agent.tool_use` | `tool_use_id`, `name`, `input`, `evaluated_permission` |
| `agent.custom_tool_use` | `tool_use_id`, `name`, `input` |
| `agent.tool_result` | `tool_use_id`, `content[]`, `is_error` |

Session lifecycle:

| Type | Fields |
| --- | --- |
| `session.status_running` | — |
| `session.status_idle` | `stop_reason` |
| `session.status_terminated` | — |
| `session.error` | `error` |

Read back, every event also carries `id`, `seq` and `processed_at`. Events are
flat: `type` selects the shape and the rest of the object *is* the event. There
is no `payload` envelope.

## Rule 1 — a batch stops as a batch

One model reply can contain several tool calls. If **any** of them needs a
decision, the whole reply stops before **any** of them runs.

```json
{ "type": "agent.tool_use",        "tool_use_id": "toolu_01A", "name": "read_file", "evaluated_permission": "allow" }
{ "type": "agent.tool_use",        "tool_use_id": "toolu_01B", "name": "execute",   "evaluated_permission": "ask" }
{ "type": "agent.custom_tool_use", "tool_use_id": "toolu_01C", "name": "get_crm_record" }
{ "type": "session.status_idle",   "stop_reason": { "type": "requires_action", "tool_use_ids": ["toolu_01B", "toolu_01C"] } }
```

`toolu_01A` is **not** in `tool_use_ids` and has **no result yet**. It needs
nobody's permission and it is still waiting, because the agent paused upstream
of every tool. Its result arrives only after you answer `B` and `C`.

A client that renders `evaluated_permission: "allow"` as "running now" will
show a spinner that never resolves.

## Rule 2 — answer everything, in one request

`tool_use_ids` is the complete list. Send every answer in a single
`POST /v1/sessions/{id}/events`. A short list is not rejected at the door — it
is passed to the agent, which counts it and fails the turn.

Answers cannot be mixed with anything else in the same batch. A batch is either
all answers, one or more messages, or one interrupt.

## Rule 3 — order is yours

Every answer names the call it belongs to, so send them in whatever order suits
you. They are put back into the order the agent asked for.

```json
{
  "events": [
    { "type": "user.tool_confirmation",  "tool_use_id": "toolu_01B", "result": "deny", "deny_message": "No shell in this session." },
    { "type": "user.custom_tool_result", "custom_tool_use_id": "toolu_01C", "content": [{ "type": "text", "text": "Northwind Traders, tier gold" }] }
  ]
}
```

Two different event types answering one pause, in one request, in the reverse
of the order asked for. All three are fine.

## Rule 4 — one id, end to end

`tool_use_id` is the engine's own identifier for the call. The same string
appears in all four places, with no mapping step anywhere:

```
agent.tool_use.tool_use_id
  → session.status_idle.stop_reason.tool_use_ids[]
    → your user.tool_confirmation.tool_use_id
      → agent.tool_result.tool_use_id
```

Store it, send it back unchanged, match results on it. It is **not** the `id`
of the `agent.tool_use` event — those are different strings.

## Rule 5 — `is_error` invites a retry

A custom tool has no implementation on this side; your reply is the only way
one ever completes. Setting `is_error` changes what the model is told:

| You send | The model is told |
| --- | --- |
| `is_error` absent or `false` | the tool ran and returned your content |
| `is_error: true` | the call did **not** produce a result, and here is why |

The failure reaches the model as a failure rather than as data. What the model
then does about it is the model's choice, and it is **not predictable**: the
same failure has produced both of these.

It may report the failure and stop:

```json
{ "type": "agent.tool_result",  "tool_use_id": "toolu_01C", "is_error": true, "content": [{ "type": "text", "text": "CRM-DOWN: unavailable" }] }
{ "type": "agent.message",      "content": [{ "type": "text", "text": "The CRM lookup failed: CRM-DOWN: unavailable." }] }
{ "type": "session.status_idle", "stop_reason": { "type": "end_turn" } }
```

Or it may try the call again — a **new** `tool_use_id`, and another pause:

```json
{ "type": "agent.custom_tool_use", "tool_use_id": "toolu_01D", "name": "get_crm_record" }
{ "type": "session.status_idle",   "stop_reason": { "type": "requires_action", "tool_use_ids": ["toolu_01D"] } }
```

Handle both. Never assume the turn ends after you report a failure, and match
answers on the id you were given rather than on the tool's name.

If your tool is failing persistently, answering `is_error` every time can loop.
Return a normal result explaining the situation instead, or interrupt.

## Rule 6 — deliverables live in `outputs/`

The container has three directories that matter:

```text
/home/user/uploads   files you attached, read-only
/home/user/outputs   deliverables — collected when the turn ends
/home/user           everything else: scratch
```

When a turn ends, eligible files under `outputs/` are collected and become
Files you can list and download. Collection considers at most 50 files by
default and skips an individual file over 100 MiB by default; see
[Service limits](./limits.md). **Nothing outside `outputs/` is collected.** A
file at `/home/user/report.txt` is real, the agent can read it back, and it will
never appear in `GET /v1/files?scope_id={session_id}` — not because collection
failed, but because being a deliverable means being in `outputs/`.

Subdirectories are kept, and the path becomes the filename:

```json
{ "id": "file_...", "filename": "reports/2031/q1.txt", "scope": { "type": "session", "id": "ses_..." } }
```

`POST /v1/sessions/{id}/live/files` takes one file out mid-turn without waiting
for the turn to end; its `path` is relative to `outputs/`.

`POST /v1/sessions/{id}/live/uploads` moves in the other direction: it attaches
an existing durable File from `/v1/files` to an existing Session. Send
`{"file_id":"file_...","path":"optional/name.ext"}` while the Session is
idle. The path is relative to `uploads/` (and defaults to the File's filename),
the bytes land read-only at `/home/user/uploads/...`, and the response is the
new Session file resource. `/mnt/session/uploads/...` is an alias of the same
directory for shared CMA/VMA Agent contracts. The operation may wake a paused
E2B sandbox, but it never rebuilds a missing or terminated one. An exact retry
is idempotent; a busy Session or a different File already occupying the path
returns 409.

Automatic collection starts immediately after the runtime writes
`session.status_idle`. Because those are separate commits, a client reacting to
the event immediately can briefly receive a File list that does not yet contain
the turn's outputs.

## Rule 7 — an interrupt is not instant

`user.interrupt` is the one event accepted while the session is busy. It takes
effect on the agent's **next** write, and writes happen between steps — so an
interrupt sent while the model is composing a long answer waits for that answer
to finish generating.

The turn then ends with:

```json
{ "type": "session.status_idle", "stop_reason": { "type": "interrupted" } }
```

No later **Agent** event from the cancelled generation is accepted. The
cancelled worker still attempts output collection, so a system
`session.error` about collection can follow the idle event.

## Rule 8 — idle marks the graph stop

`session.status_idle` reports why the graph stopped. It arrives for normal
completion, a pause, an interrupt, and a failed turn:

| `stop_reason.type` | Meaning |
| --- | --- |
| `end_turn` | the graph finished |
| `requires_action` | waiting on you — see `tool_use_ids` |
| `interrupted` | you stopped it |
| `error` | it failed; the `session.error` event just before says why |

In the current rewrite, the event is not atomic with cleanup. On a normal turn
it is committed before output collection and before the Session row is released
to `idle`. An SSE client can therefore observe it, immediately send the next
message, and briefly receive `409 session_busy`; the output File list can lag in
the same window. Re-read the Session until its `status` is `idle`, or retry a
busy response. Waiting for `end_turn` specifically is still wrong because
`requires_action`, `interrupted`, and `error` also end the current graph run.

A Session whose sandbox row is missing or already marked failed/terminated ends
differently and for good: `session.error` with `sandbox_unavailable`, then
`session.status_terminated`, and every later message is refused with `409`.
A remote E2B sandbox that disappears while its row still looks usable currently
produces an ordinary failed turn and returns to `idle`; that mismatch is a known
runtime gap.

## While the agent is busy

There is no queue. A second message sent mid-turn is refused with `409` and
**nothing is appended** — a refused request leaves no trace in the log.

```json
{ "error": { "type": "session_busy", "message": "The session is still working on the previous message." } }
```

No retry hint comes with it. How long the agent still needs is not something
this service can know, and a header saying otherwise would send well-behaved
clients back at exactly the wrong moment. Watch the stream for
`session.status_idle`, then confirm the Session row is idle or retry a transient
`session_busy`.
