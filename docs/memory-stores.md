---
title: Memory Stores
description: CMA-compatible text memories, immutable versions, and E2B-backed runtime persistence.
---

Snapshot: 2026-08-01

VMA implements the Claude Managed Agents Memory Store HTTP surface on top of
two coordinated representations:

- An E2B Volume contains the live UTF-8 files mounted into Session sandboxes.
- PostgreSQL contains Store lifecycle state, each Memory's current indexed
  head, and immutable Memory Version history.

`memory_stores.id` is always the public VMA identity. E2B IDs and names remain
private provider locators and are never authorization boundaries or public
Memory Store IDs.

## Data model

```text
memory_stores
  id, organization_id, name, description, metadata
  volume_provider, volume_locator, provisioning_status
  archived_at, deleted_at

memories
  id, organization_id, memory_store_id, path
  content, content_sha256, content_size_bytes
  current_version_id, created_at, updated_at

memory_versions
  id, organization_id, memory_store_id, memory_id
  operation, path, content, hash, size
  created_by, redacted_at, redacted_by, created_at
```

A `memories` row is the current API-visible head. Deleting a Memory removes
that head but leaves its `memory_versions` rows. Deleting the Store permanently
removes both heads and history after its provider Volume is destroyed.

## Memory API semantics

The active routes cover Store create/retrieve/update/list/archive/delete,
Memory create/retrieve/update/list/delete, and Memory Version
list/retrieve/redact.

- Paths are slash-prefixed, case-sensitive strings. They must contain at least
  one segment, be NFC-normalized, contain no empty, `.`, or `..` segments and
  no control/format characters, and be at most 1,024 UTF-8 bytes.
- Content is UTF-8 text capped at 102,400 bytes. A Store may contain at most
  2,000 live Memories.
- A path is unique within one Store. A create never overwrites; rename rejects
  a destination collision.
- Basic views omit content while retaining its byte size and SHA-256. Full
  views include content.
- `path_prefix` must end in `/`. `depth=1` returns immediate Memories mixed
  with list-time `memory_prefix` rollups; omitted `depth` or `depth=0` returns
  the whole subtree.
- Full-view list pages are capped at 20 entries. Page tokens are opaque to
  clients.

API create/update/delete changes the E2B file first and then commits the
relational head and its version. An update can pass the official
`precondition: {type: "content_sha256", content_sha256: "..."}` and delete can
pass `expected_content_sha256`. A stale update returns `409` unless the stored
path and content already equal the requested result, in which case it is an
idempotent no-op.

## Immutable version history

Every non-no-op create, update, rename, and delete appends one
`memory_version`:

- `created` and `modified` versions snapshot path, content, hash, and size.
- A `deleted` version preserves the deleted path and returns null content,
  hash, and size.
- API writes use an `api_actor`; sandbox writes use a `session_actor`.
- Versions remain retrievable after their live Memory is deleted.
- Redaction clears path, content, hash, and size from a historical version
  while retaining the audit row. The current head of a live Memory cannot be
  redacted.

VMA currently retains versions until the Store is deleted. It does not yet
apply Claude's age-based historical-version retention policy.

## Runtime writeback

When a read-write Store is attached, its Volume is mounted below
`/mnt/memory/<store-slug>`. The system prompt includes its exact returned
`mount_path`, name, description, access, and attachment instructions. File
contents are not automatically copied into the model context; the agent reads
the relevant files with its filesystem tools, usually under guidance from a
Skill.

After a successful `write_file`, `edit_file`, or `execute` tool result, VMA
hashes files below every attached mount. It reads only new or changed files,
then creates session-attributed heads and versions and records files that
disappeared as deletions. A final reconciliation also runs when the turn ends,
is cancelled, or fails.

This is a tool-boundary snapshot, not a filesystem journal. Several writes
inside one shell command produce one indexed final state, and concurrent
read-write Sessions use the Volume's normal last-writer behavior.

## Consistency boundary

The Volume and relational database cannot participate in one transaction.
Store-row locks serialize VMA writers, and final runtime reconciliation repairs
many interrupted writeback cases, but a process crash between provider and
database commits can temporarily leave the filesystem and API projection out
of sync. There is no durable provider-operation outbox or automatic orphan
reconciler yet.

E2B documents direct Volume SDK file operations for use while a Volume is not
mounted. VMA's API mirror path uses those operations, so direct Memory API
mutation while an existing E2B Sandbox still owns that mount remains an
experimental provider boundary and is not claimed as certified concurrent
behavior.

See [Memory Stores on E2B Volumes](./memory-volumes.md) for mount lifecycle and
provider limitations.
