# Memory Stores

Memory Store data lives in the relational database. In production this means a Postgres-compatible database through `DATABASE_URL`.

Object storage is not the source of truth for memory records. S3-compatible storage should only be used for optional large binary memory attachments, exported snapshots, or artifact bundles.

## Current MVP Semantics

- `memory_store` rows store store-level metadata.
- `memory` rows store structured records with `path`, `path_key`, `content`, metadata, and the current optimistic `version`.
- `memory_version` rows store version snapshots with actor and operation metadata.
- Paths are unique inside a memory store.
- String paths follow the Anthropic SDK contract: they must start with `/`, be at most 1024 bytes, contain no empty, `.`, or `..` segments, contain no control/format characters, and already be NFC-normalized.
- Listing with `depth` returns list-time `memory_prefix` rollups for directories deeper than the requested depth. Prefixes are not stored resources and have no lifecycle.
- Individual memory content is capped at 100KB, and each store is capped at 2000 live memories.
- Updates may pass official `precondition: {type: "content_sha256", content_sha256: "..."}` or OSS-compatible `if_version` / `expected_version`; stale values return `409` unless the requested content/path already matches the stored memory, in which case the update is treated as an idempotent no-op.
- Every non-no-op create, update, and delete creates an immutable memory version; versions survive after their memory is deleted.
- Deletes may pass `expected_content_sha256`; stale values return `409`.
- Deleted memory-version responses preserve the deleted path but return `null` for `content`, `content_sha256`, and `content_size_bytes`.
- Redaction removes the snapshot `content` from the targeted memory version. The current live head version cannot be redacted.
- Archived stores remain readable, but reject writes and cannot be attached to new sessions.

## Path Examples

```json
{
  "path": ["customers", "acme"],
  "content": "ACME prefers email.",
  "actor": "api"
}
```

The official string form is:

```json
{
  "path": "/customers/acme",
  "content": "ACME prefers email."
}
```

Array paths are accepted as an OSS compatibility extension and normalize to the same slash-prefixed string.

## Storage Boundary

Use Postgres for the memory store itself. Use S3-compatible object storage only for optional large binary artifacts related to a memory, such as an exported archive or external attachment.
