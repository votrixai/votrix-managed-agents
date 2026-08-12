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

The API manages the Store itself: create, list, retrieve, update, archive, and
delete. Its contents are read and written by an Agent from a Session sandbox;
there are no separate HTTP endpoints for individual documents inside a Store.

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

## Update, archive, and delete

- Updating a Store changes its name, description, or metadata. Existing
  contents are unaffected.
- Archiving prevents updates and new attachments while preserving the Store
  for Sessions that already reference it.
- Deletion permanently removes a Store and is refused with `409 conflict` if
  any Session references it. Archive an attached Store instead.

Start with [Create Memory Store](/docs/api/memory-stores/create_memory_store_v1_memory_stores_post),
then use the Memory Stores section of the [API Reference](/docs/api) for every
other operation and field.
