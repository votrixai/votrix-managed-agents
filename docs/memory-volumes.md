---
title: Memory Stores on E2B Volumes
description: How Store properties, path-addressed files, and Session mounts map to E2B Volumes.
---

Snapshot: 2026-08-14

One logical VMA Memory Store owns one native E2B Volume. The Volume survives
individual Sandbox lifecycles and is mounted when a Session is created.

## Store and Volume responsibilities

```text
MemoryStore row                         E2B Volume
-------------------------------         -----------------------------
id, organization_id                     directories and file bytes
name, description, metadata             persistent filesystem state
archive/delete lifecycle                provider volume id and name
```

`PATCH /v1/memory_stores/{id}` changes the Store's control-plane properties.
It does not rename the provider Volume or change file bytes. The Volume name is
derived from the stable Store ID when the Store is created.

The provider locator is private. E2B uses its volume ID for standalone content
operations and deletion, and its stable volume name when creating a Sandbox
mount.

## Session attachment

A Session attaches a Store at creation:

```json
{
  "type": "memory_store",
  "memory_store_id": "memstore_...",
  "access": "read_write",
  "instructions": "Read context.md before content work."
}
```

The Store is mounted at a path such as:

```text
/mnt/memory/content-creator
```

The runtime prompt names that exact path. Writes below it persist directly in
the Volume. A leading slash matters: `mnt/memory/...` is relative to the
Sandbox working directory and is not the mount.

Names, descriptions, instructions, and mount paths are Session snapshots. A
later Store rename affects future Sessions only. The same underlying Volume is
used either way.

## Path-addressed file mutation

The public file surface addresses one relative path at a time:

```text
PUT    /v1/memory_stores/{id}/files/{path}  create or replace raw bytes
DELETE /v1/memory_stores/{id}/files/{path}  remove, idempotently
```

VMA selects the E2B operation from the Store's current mount state:

```text
lock Store and attached Sessions
              |
              +-- usable idle mount --> write/remove at its exact Sandbox path
              |
              +-- no usable mount ----> use standalone Volume content API
              |
              `-- attached work active -> 409 conflict
```

E2B documents standalone Volume SDK methods for Volumes that are not mounted.
Routing an attached Store through its Sandbox honors that boundary. Store and
Session row locks serialize the decision with first attachment and prevent a
new Agent turn from starting during the provider operation.

The Volume is the only content source of truth. There is no relational file
projection, document-version table, tree reconciliation, or dual-write. A
successful `PUT` response reports the path, size, and SHA-256 of the submitted
bytes; a successful `DELETE` returns `204`.

## Provider and concurrency boundaries

- E2B Volumes are private beta and must be enabled for the project behind
  `E2B_API_KEY`.
- One file write is limited to 100 MiB; one relative path is limited to 1,024
  UTF-8 bytes and cannot traverse with `.` or `..`.
- Archived Stores reject file API mutations and new attachments.
- E2B 2.31.0 does not expose a read-only Volume mount, so an already-mounted
  Session retains write access after archival.
- VMA serializes its own API writes against its own Agent turns. External E2B
  clients and background processes inside a Sandbox are outside that lock, so
  there is no global compare-and-swap guarantee across every possible writer.
- A provider failure returns `503 memory_store_unavailable`; the API never
  reports a successful file mutation after a failed E2B call.

The opt-in provider proof is:

```bash
VMA_TEST_E2B_VOLUMES=1 pytest tests_live/test_memory_volume.py
```
