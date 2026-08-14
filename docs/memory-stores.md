---
title: Memory Stores
description: Keep a shared workspace available across Sessions.
---

A Memory Store is a persistent shared workspace for Agents. Attach one when a
Session starts to let the Agent read useful context and add knowledge that a
later Session can use.

Typical uses include brand guidance, research notes, customer context, and
project state that should outlive one Session.

## Create a Store

```json
{
  "name": "Brand context",
  "description": "Voice, audience, and approved product facts",
  "metadata": {
    "team": "content"
  }
}
```

The API manages the Store itself separately from the files inside it. Store
properties use the Store URL; path-addressed file writes use `/files/` below
that Store.

## Update Store properties

Use `PATCH /v1/memory_stores/{memory_store_id}` to change the Store's display
properties without touching its files:

```json
{
  "name": "Editorial context",
  "description": "Voice, audience, and current campaign facts"
}
```

The existing `POST` form remains accepted for client compatibility. Renaming a
Store changes the mount path derived for future Sessions; existing Sessions
keep the name and mount path captured when they were created.

## Write and delete files

The part after `/files/` is the relative path inside the Store. `PUT` creates
the file or replaces all of its bytes, creating parent directories when needed:

```bash
curl --request PUT \
  --header "x-api-key: $VMA_API_KEY" \
  --header "content-type: application/octet-stream" \
  --data-binary @context.md \
  "$VMA_API_URL/v1/memory_stores/memstore_.../files/brand/context.md"
```

The response reports the relative path, byte size, and SHA-256 digest that were
written. Delete the same path with:

```text
DELETE /v1/memory_stores/{memory_store_id}/files/brand/context.md
```

Deletion is idempotent and returns `204`, including when the file is already
absent. Paths must be normalized relative paths: empty, absolute, `.` and `..`
segments, empty segments, and control characters are rejected. One file may be
at most 100 MiB.

When the Store has no live mount, VMA writes through its Volume. When it is
already mounted, VMA writes through one idle Session's exact mount path. A file
mutation returns `409 conflict` while any attached Session is working, avoiding
a control-plane write racing the Agent.

File listing and download are intentionally not part of this first surface.
Agents read files through the mounted filesystem.

## Attach it to a Session

Add the Store to `resources` when creating a Session:

```json
{
  "agent_id": "agent_...",
  "environment_id": "env_...",
  "resources": [
    {
      "type": "memory_store",
      "memory_store_id": "memstore_...",
      "access": "read_write",
      "instructions": "Read the brand context before writing. Save durable project notes here."
    }
  ]
}
```

`read_write` is the supported access mode. Attachment happens when the Session
is created and remains fixed for that Session. You can attach up to eight
Memory Stores.

The optional `instructions` tell the Agent how the Store should be used. Keep
them specific—for example, what to read before starting and what is worth
preserving for later.

## Store lifecycle safety

Updating a Store changes its name, description, or metadata. Existing files
are unaffected; use the `/files/` routes to change them.

The public API deliberately does not expose Store-level archive or delete
operations. Destroying a Store would destroy its complete persistent Volume,
and archival is not safe to expose without a corresponding restore and
retention contract. Operator-controlled lifecycle tooling can be added with
those safeguards separately. The path-addressed `DELETE` above removes only
the explicitly named file.

Start with [Create Memory Store](/docs/api/memory-stores/create_memory_store_v1_memory_stores_post),
then use the Memory Stores section of the [API Reference](/docs/api) for every
other operation and field.
