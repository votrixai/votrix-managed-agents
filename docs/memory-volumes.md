---
title: Memory Stores on E2B Volumes
description: The active rewrite's persistent Memory Store and Session mount contract.
---

Snapshot: 2026-08-01

The active runtime represents a Memory Store as a VMA-owned logical resource
backed by one provider Volume. For the current provider, that is a native E2B
Volume mounted when the Session Sandbox is created.

## Ownership model

`memory_stores.id` is the public identity. Provider details remain internal:

```text
MemoryStore
  id                    memstore_...       public VMA identity
  organization_id       tenant boundary
  volume_provider       e2b                adapter selector
  volume_locator        {volume_id, volume_name}
  provisioning_status   provisioning | ready | failed | deleting | deleted
```

The locator is provider-specific on purpose. E2B uses the immutable Volume ID
for destruction and the Volume name as the Sandbox mount handle. A future R2
adapter could store a bucket and prefix in the same column, but an R2 prefix is
not natively mountable by E2B; it would need a separate materialize/writeback
adapter or filesystem gateway.

Creating a Store commits its control-plane row before calling E2B. Provider
success records the locator and makes the Store attachable. Provider failure
is retained internally so an operator or future reconciler can inspect it; it
is not returned by normal retrieve or list operations.

## Session attachment

A Session declares a Memory Store in `resources`:

```json
{
  "type": "memory_store",
  "memory_store_id": "memstore_...",
  "access": "read_write",
  "instructions": "Read context.md before content work."
}
```

Attachment is creation-only. The service resolves the Store in the Session's
Organization, snapshots its name and description, derives a stable path such
as `/mnt/memory/content-creator`, and passes this native E2B mapping at Sandbox
creation:

```python
volume_mounts={
    "/mnt/memory/content-creator": "vma-production-memstore-...",
}
```

At most eight Memory Stores may be attached. The same Store cannot appear
twice, and two Stores whose names resolve to the same slug are rejected. A
later Store rename affects future Sessions only; an existing Session keeps the
snapshotted name, description, and mount path returned in its resources.

## How runtime updates persist

There is no end-of-turn copy or database writeback loop. The agent's ordinary
file tools operate on the mounted directory. E2B persists writes below the
exact mount path directly into the Volume, so another Session that later mounts
the same Volume sees those bytes. Writes elsewhere under `/mnt/memory` are not
part of a Store.

The runtime adds every Store's exact path, access mode, description, and
per-attachment instructions to the system prompt. Client Agent Skills must use
the absolute path—for example:

```text
/mnt/memory/content-creator/context.md
```

`mnt/memory/...` without the leading slash is relative to the Sandbox workdir
and does not address the mounted Store.

## Current boundary

- E2B Volumes are a private-beta capability and must be enabled for the E2B
  project behind `E2B_API_KEY`.
- No new deployment secret is required. `E2B_API_KEY` is reused, and `APP_ENV`
  separates deterministic provider names between environments.
- The pinned E2B SDK has no read-only Volume mount option. `read_only` requests
  are rejected rather than represented as secure; tool filtering cannot stop
  the shell from writing to a writable filesystem.
- The filesystem is the content source of truth in this MVP. The Claude-style
  Memories API, immutable versions, content hashes, audit attribution, limits,
  and point-in-time restore are not implemented here.
- Concurrent read-write Sessions share normal filesystem semantics. There is
  no optimistic-concurrency layer above E2B Volume writes.
- A Store that has been attached is archived instead of provider-deleted until
  Session/Sandbox teardown has an explicit lifecycle contract.

The opt-in real-provider proof is
`VMA_TEST_E2B_VOLUMES=1 pytest tests_live/test_memory_volume.py`.
