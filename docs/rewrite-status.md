---
title: Implementation Status
description: Current API, runtime, deployment, security, and validation status.
---

Snapshot: 2026-08-01

## Sources of truth

| Area | Authoritative source |
| --- | --- |
| Application | `app/` |
| Isolated tests | `tests/` |
| External integration tests | `tests_live/` |
| Database schema | `alembic/versions/` |
| HTTP contract | `app.server.create_app` and the generated [API reference](./api/index.mdx) |
| Runtime architecture | [Core architecture](./votrix-core-architecture.md) |

Superseded code, tests, migrations, and narrative documentation are retained in
Git history rather than shipped on the main branch.

## Active API

The active service implements:

- Agents and immutable Agent versions;
- Environments and E2B image builds;
- Sessions, durable events, event retrieval, and resumable SSE;
- Files and Skill archives;
- live and end-of-turn output capture;
- Memory Store lifecycle;
- CMA-compatible Memories and immutable Memory Versions;
- E2B Volume-backed, creation-time Memory Store mounts.

Organization and model-catalog routes remain placeholders. Vaults,
deployments, webhooks, quotas, audit/usage ledgers, and production API-key
management are not implemented.

## Memory runtime

Each ready Memory Store owns one E2B Volume. A Session attaches the Store at
creation and receives a native mount below `/mnt/memory/<slug>`. API writes are
mirrored to the Volume; successful filesystem mutations and final-turn
reconciliation index Sandbox changes into Memory heads and immutable Versions.

The pinned E2B SDK cannot enforce a read-only Volume mount, so the service
rejects `read_only` attachments. See [Memory Stores](./memory-stores.md) and
[Memory Stores on E2B Volumes](./memory-volumes.md).

## Runtime and infrastructure

The service uses:

- Deep Agents 0.6.12 and LangGraph checkpoints;
- one E2B sandbox per Session;
- SQLite or PostgreSQL relational state;
- private S3-compatible File and Skill storage;
- inline turns or OIDC-authenticated Cloud Tasks push dispatch;
- durable-row polling for SSE;
- an API-only Cloud Run release path with a dedicated migration job.

## Security boundary

The active app is not ready for direct untrusted multi-tenant traffic.
`x-organization-id` scopes queries but is trusted caller input. `x-api-key` is
accepted for attribution and is not validated. Cloud Tasks callbacks are the
exception: cloud mode verifies Google OIDC audience and service-account email.

## Validation boundary

The default test path uses a fresh SQLite database and stubs external
providers. `tests_live/` deliberately exercises real E2B, object storage,
models, and PostgreSQL where required. The generated OpenAPI snapshot is
compared against the active FastAPI app in CI, and the documentation site is
type-checked, linted, and statically built on every pull request.

Known production gaps include authentication/authorization, atomic external
provider/database sagas, Cloud Tasks enqueue outbox recovery, complete worker
fencing after lease expiry, and filesystem-level read-only Volume enforcement.
