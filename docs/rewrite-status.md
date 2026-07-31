---
title: Rewrite Status
description: What is active on the peteryue branch, what is archived, and which documentation is authoritative.
---

Snapshot: 2026-07-30

Branch: `peteryue`

The active service on this branch is a rewrite. Merge commit `1960056` retained
the new `app/` tree and moved the previous application and tests out of the
runtime path.

## Source of truth

| Area | Current | Historical or pending migration |
| --- | --- | --- |
| Application | `app/` | `app_archived/` |
| Default tests | `tests/` | `tests_archived/` |
| External integration tests | `tests_live/` | Previous contract suites under `tests_archived/contract/` |
| Database schema | Active Alembic initial migration plus its follow-up | Numbered pre-rewrite migrations retained as archived files |
| Architecture docs | This page and [Rewrite architecture](./votrix-core-architecture.md) | Pre-rewrite platform and deployment pages |

`app_archived/` is reference material. New active code must not import it.

## Active API implementation

The following resource families have working handlers backed by the active
service and query layers:

- Agents and immutable Agent versions;
- Environments and asynchronous E2B image builds;
- Sessions, Session events, event retrieval, and SSE;
- live and end-of-turn Session output capture;
- Files;
- Skill archives.

The following routes are registered but still contain placeholder handlers:

- Organizations and Organization owners;
- the model catalog.

Memory router/service/query modules exist as empty scaffolding and are not
mounted. The previous API-key, Vault, provider-registry, usage, quota, memory,
deployment, webhook, and environment-work APIs have not been ported.

## Active runtime

The rewrite currently uses:

- Deep Agents 0.6.12 and LangGraph;
- one E2B sandbox per Session;
- a hard-coded model catalog with one process-level key per provider;
- SQLite or PostgreSQL for relational state;
- a separate LangGraph checkpoint file or PostgreSQL checkpoint tables;
- private S3-compatible storage for File and Skill bytes;
- inline execution or Cloud Tasks push dispatch;
- polling of durable `session_events` for SSE.

It does not use the archived runtime's:

- Vault/BYOK credential binding;
- model-provider registry or OpenRouter routing profile;
- StateBackend/local-shell/custom sandbox factory;
- PostgreSQL `NOTIFY` preview broker;
- `environment_work` queue, poller, or hybrid reconciler;
- quota, audit, or usage ledgers.

## Security status

The active app is not ready for untrusted multi-tenant traffic.

`x-organization-id` is required and used to scope queries, but the caller is
trusted to choose it. `x-api-key` is accepted and not validated. This preserves
tenant predicates inside the data layer while authentication is rebuilt, but
it is not an authorization boundary.

The Cloud Tasks callback is the exception: in cloud mode it verifies a Google
OIDC token against the configured audience and service-account email.

## Repository integration status

These top-level integration points still target the archived implementation:

- `votrix_managed_agents/__init__.py`;
- `entrypoint.sh` and the API command in `run.sh`;
- the `vma-worker` console-script target and `scripts/start-worker.sh`;
- Cloud Run service manifests and their `/health` probes;
- Cloud Build and GCP deployment scripts;
- SDK contract tests and SDK documentation;
- the previously committed OpenAPI schema and its exporter.

Until each item is migrated and tested, use `uv run uvicorn app.main:app` for
the active application and do not treat the checked-in Cloud Run topology as a
deployable rewrite configuration.

## Documentation authority

Reviewed against the rewrite:

- [Rewrite architecture](./votrix-core-architecture.md)
- [Agent versioning](./agent-versioning.md)
- [Session events](./session-events.md)
- [Event streaming](./streaming.md)
- [Errors](./errors.md)
- [Service limits](./limits.md)

The remaining narrative pages were written for the archived app. They stay in
the repository because they contain product intent and migration context, but
their route inventories, configuration names, authentication claims, sandbox
paths, worker topology, and deployment instructions are not current behavior.

## Validation boundary

The default test path is `tests/`. It uses a fresh in-memory SQLite database and
stubs E2B, object storage, model calls, and Cloud Tasks. It covers the active
wire models, tenant predicates, the Session admission gate, explicit
interrupt/release generation fencing, event
streaming, Environment builds, Files, Skills, pagination, and tool policies.

Those tests do not prove:

- package and Docker entry points;
- Cloud Run manifests or health probes;
- PostgreSQL behavior;
- real E2B, object storage, Cloud Tasks, or model-provider integration;
- production authentication or authorization.

Use `tests_live/` deliberately for real-service validation; it is not collected
by the default pytest configuration.
