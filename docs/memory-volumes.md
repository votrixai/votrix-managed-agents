---
title: Memory Stores on E2B Volumes
description: How a VMA Memory Store is provisioned, mounted, exposed to Deep Agents, and indexed after writes.
---

Snapshot: 2026-08-01

The active E2B adapter represents one logical VMA Memory Store with one native
E2B Volume. The Volume survives individual Sandbox lifecycles and is mounted
when a Session Sandbox is created.

## Ownership and provider binding

```text
MemoryStore
  id                    memstore_...       public VMA identity
  organization_id       tenant boundary
  volume_provider       e2b                adapter selector
  volume_locator        {volume_id, volume_name}
  provisioning_status   provisioning | ready | failed | deleting | deleted
```

`volume_locator` is provider-specific. E2B uses `volume_id` to reconnect or
destroy the Volume and `volume_name` as the Sandbox mount handle. A future R2
adapter can store a bucket and prefix in the same field, but R2 is not natively
mountable by E2B and would require a materialize/writeback adapter or filesystem
gateway.

Creating a Store commits its control-plane row before provisioning E2B. A
successful provider call records the locator and marks the Store ready. A
failure remains internal for operator diagnosis. Store deletion destroys the
Volume and purges Memories and Versions, but is rejected after the Store has
ever been attached because existing Session/Sandbox teardown is a separate
lifecycle boundary; use archive for those Stores.

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

The service resolves the Store inside the Session's Organization, snapshots
its display fields, derives a filesystem-safe path, and passes a native mapping
to E2B:

```python
volume_mounts={
    "/mnt/memory/content-creator": "vma-production-memstore-...",
}
```

At most eight Stores may be attached. Attachment is creation-only. Duplicate
Stores and colliding mount slugs are rejected. A later Store rename affects
future Sessions only; an existing Session keeps the returned mount path and
snapshotted description.

E2B 2.31.0 does not expose a read-only Volume mount. VMA therefore rejects
`access: "read_only"` instead of pretending that tool filtering is a security
boundary: the `execute` tool could otherwise write through the mount.

## How the agent discovers memory

VMA adds a Memory Stores section to the runtime system prompt with each exact
mount path, name, description, access mode, and per-Session instructions. It
does not inject every Memory's body into the context window.

A client Skill such as:

```md
## Session Start

Read `/mnt/memory/content-creator/context.md` before content work.
```

causes Deep Agents to discover the Skill and, when it applies, use `read_file`
against the persistent mount. The leading `/` matters: `mnt/memory/...` is
relative to `/home/user` and does not reference the mounted Store.

## Write and indexing flow

```text
agent filesystem tool
        |
        v
/mnt/memory/<slug>/<path>  -- native mount -->  E2B Volume
        |
        | successful write_file / edit_file / execute boundary
        v
hash mounted tree -> read changed UTF-8 files -> PostgreSQL Memory head
                                           `-> immutable session_actor Version
```

The E2B mount persists bytes immediately. VMA reconciliation supplies the CMA
API projection and audit history:

- unchanged hashes transfer no content;
- new or changed files create `created` or `modified` Versions;
- missing indexed paths create `deleted` Versions;
- non-UTF-8, invalid-path, oversized, and over-limit files remain on the
  Volume but are rejected from the Memory API projection and reported through
  a Session error event at final reconciliation.

Reconciliation runs after successful filesystem-mutating tool results and at
turn exit. It observes the final state of a shell command, not every syscall
inside that command.

## Direct Memory API flow

Direct API create/update/delete calls use E2B's Volume file API and then commit
the corresponding relational head and API-attributed Version. Basic and full
reads are served from PostgreSQL, so listing does not resume a Sandbox or scan
the provider on every request.

This dual write is not atomic. A provider success followed by a database crash
can leave an unindexed file until a later mounted reconciliation observes it.
Conversely, VMA does not commit a new head when the provider call fails.

E2B explicitly says standalone Volume SDK methods are intended for a Volume
that is not mounted. Direct API mutation while a persistent Session Sandbox
still has the Volume mounted has not been certified as a supported concurrent
provider operation and remains an experimental limitation.

## Current provider boundary

- E2B Volumes are private beta and must be enabled for the E2B project behind
  `E2B_API_KEY`.
- No separate Volume secret is required. Provider names reuse `E2B_API_KEY`
  and include `APP_ENV` for environment separation.
- Multiple read-write Sessions have no cross-Sandbox optimistic filesystem
  concurrency. Content-hash preconditions protect direct API updates against
  the indexed head, not arbitrary concurrent shell writes.
- Archiving blocks API writes and new attachments. An already-mounted Session
  cannot be made filesystem-read-only by the pinned E2B SDK.
- There is no durable dual-write outbox, Volume generation/fencing token, or
  automatic provider/database repair worker.

The opt-in real-provider proof is
`VMA_TEST_E2B_VOLUMES=1 pytest tests_live/test_memory_volume.py`.
