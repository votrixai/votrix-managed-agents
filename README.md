# Votrix Managed Agents

This repository contains the active Votrix Managed Agents service under `app/`
and its isolated test suite under `tests/`. The superseded implementation was
removed from the main branch and remains recoverable from Git history.

> **Source-of-truth order (2026-08-23):** use `app/` and `tests/` for active
> behavior, the generated OpenAPI document for the public HTTP contract, and
> the public guides for supported workflows. The dated
> [rewrite status](docs/rewrite-status.md) summarizes the same implementation;
> superseded behavior belongs only to Git history.

## What the rewrite implements

- Organization-scoped Agents with immutable versions.
- E2B-backed Environments whose package recipes build reusable images.
- Accounts that own encrypted OpenRouter inference keys, spending limits, and
  provider usage snapshots.
- Sessions that pin an Agent version and keep one E2B sandbox for their
  lifetime.
- An append-only Session event log, resumable SSE, interrupt handling, and a
  one-turn-at-a-time admission lease.
- Deep Agents 0.6.12 with LangGraph checkpoints, filesystem tools, Skills,
  custom-tool pauses, and a small server-owned model catalog.
- Files and Skill archives in private S3-compatible object storage.
- Inline turn execution for development and Cloud Tasks push dispatch for
  deployed environments.
- Async SQLAlchemy persistence on SQLite or PostgreSQL.

Implemented resource routes authenticate a database-backed `x-api-key` and
derive the Organization from that credential. A caller cannot select a tenant
with a separate header. Organization routes remain placeholders. MCP servers
are stored but not loaded by the runtime, and `app.worker` also owns the lease
sweeper. See [Current gaps](#current-gaps).

## Architecture

```text
HTTP client
    |
    | x-api-key
    v
API-key lookup ── organization_id
    |
    v
FastAPI routers ── Pydantic wire models
    |
    v
Services ── use-case rules, transactions, external orchestration
    |
    +──────────────> E2B sandbox / image API
    +──────────────> S3-compatible object storage
    +──────────────> Cloud Tasks (cloud dispatch only)
    |
    v
SQLAlchemy queries and models
    |
    +── resource/event state: SQLite or PostgreSQL
    +── agent graph state: separate LangGraph checkpoint tables/file

During a turn:

Session event -> lease claim -> inline call or signed Cloud Tasks callback
              -> Deep Agents graph -> model and sandbox tools
              -> durable output events -> SSE readers woken by PostgreSQL
                                          notifications or polling fallback
```

The active code is deliberately layered:

| Layer | Location | Responsibility |
| --- | --- | --- |
| App composition | `app/server.py`, `app/main.py` | Build FastAPI and install error handlers |
| HTTP | `app/routers/`, `app/models/` | Parse requests and shape responses |
| Use cases | `app/services/` | Enforce domain rules and own commits |
| Persistence | `app/db/models/`, `app/db/queries/` | Store rows and run tenant-scoped queries |
| Agent runtime | `app/runtime/` | Build Deep Agents graphs and translate graph output to public events |
| Infrastructure adapters | `app/utils/` | E2B, object storage, IDs, and timing |

Read [Votrix core architecture](docs/votrix-core-architecture.md) for the full
request, Session, dispatch, sandbox, and persistence flows.

## Run locally

Requirements:

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)

Install, migrate, and start the active app:

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

OpenAPI is available at `http://127.0.0.1:8000/openapi.json`.

The active Alembic history starts a new lineage (`down_revision = None`). Run
it against a fresh database. It does not upgrade a database stamped with the
superseded lineage or reconcile its `alembic_version`; preserving such data
requires a separately designed migration using the repository's Git history.

The database defaults to a local SQLite file. Resource routes require a
database-backed Organization API key. Bootstrap one for local development and
capture the one-time plaintext from the command output:

```bash
uv run python -m scripts.bootstrap_api_key \
  --organization-id org_local \
  --organization-name "Local"

curl http://127.0.0.1:8000/v1/agents \
  --header "x-api-key: vma_..."
```

The key itself chooses the Organization; do not send a separate Organization
header.

### Runtime credentials

A Session is provisioned with E2B when it is created, and every Session spends
through an Account-owned OpenRouter inference key. An end-to-end local run
therefore needs:

```dotenv
E2B_API_KEY=...
OPENROUTER_MANAGEMENT_KEY=...
VMA_ENCRYPTION_KEY=...
```

After bootstrapping the local Organization above, provision its default Account
and encrypted inference key once:

```bash
uv run python -m scripts.backfill_default_accounts
```

Files, Skill packages, attached inputs, and collected outputs also need private
S3-compatible storage:

```dotenv
S3_ENDPOINT_URL=https://...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...
```

Model IDs come from the hard-coded catalog in `app/models/llm.py`. A caller
selects a model ID, while the selected Session Account supplies the encrypted
OpenRouter inference key used for the turn. `FIRECRAWL_API_KEY` is additionally
required only for Agents that declare the web toolset.

### Turn dispatch

The default runs each turn in the API request:

```dotenv
TURN_DISPATCH=inline
```

Cloud mode commits the input events and sends a named, OIDC-signed Cloud Task
to the internal processing route:

```dotenv
TURN_DISPATCH=cloud
TASKS_PROJECT=...
TASKS_LOCATION=...
TASKS_QUEUE=...
TASKS_SERVICE_ACCOUNT=...
WORKER_URL=https://...
```

All five Cloud Tasks values are required when `TURN_DISPATCH=cloud`;
configuration validation fails when settings are first loaded if any is
missing. `WORKER_URL` may point to a separately scaled service running the same
FastAPI app. `python -m app.worker` does not execute turns; it only returns
expired `running` Sessions to `idle`.

## Main data flows

### Create a Session

1. The service resolves the Agent and requested immutable Agent version inside
   the current Organization.
2. It verifies that the Environment is ready.
3. It creates the Session row and records attached File rows.
4. It starts one E2B sandbox from the Environment image.
5. It installs referenced Skills and downloads attached Files into
   `/home/user/uploads`.
6. It commits only after the sandbox is usable.

The same sandbox ID is reused across turns and is never silently replaced. A
missing sandbox row, or one already marked failed/terminated, terminates the
Session. If the remote E2B sandbox disappears while its row still looks usable,
lazy reconnect currently surfaces a normal failed turn and returns the Session
to `idle`; that terminal-state mismatch is a known gap.

### Run a turn

1. `POST /v1/sessions/{id}/events` atomically claims an idle or expired Session.
2. Input events and the `running` lease are committed together.
3. Inline mode calls the runtime immediately; cloud mode enqueues a signed task.
4. Deep Agents restores the LangGraph checkpoint keyed by Session ID.
5. Each translated Agent event is committed separately, making it visible to
   polling and SSE readers while the turn is still running.
6. The graph currently commits `session.status_idle`, then output files are
   copied from `/home/user/outputs` before the Session row itself is released
   to `idle`.

An interrupt advances the Session generation. The old worker checks that
generation immediately before every event write, so it cannot append into a
newer turn. Expired-lease takeover does not currently advance the generation,
so it does not provide that same protection.

## Persistent state

The rewrite owns these relational tables:

- `organizations`, `organization_members`, and `vma_api_keys`
- `agents` and `agent_versions`
- `environments`
- `sessions`, `session_events`, `session_files`, and `session_sandboxes`
- `memory_stores`, `memories`, `memory_versions`, and `session_memory_stores`
- `files`
- `skills`

Every tenant-owned resource row carries `organization_id`; the `organizations`
root row is the exception. Implemented resource lookups are scoped by the
tenant column. Object keys begin with `organizations/{organization_id}/`.
LangGraph checkpoints are separate from the public event log: checkpoints are
the Agent's resumable execution state; `session_events` are the client-visible
record.

## Tests

The active unit and API suite is:

```bash
uv run pytest
```

It uses an in-memory SQLite database and stubs E2B, storage, models, and Cloud
Tasks where needed. `tests_live/` is the explicit external-service suite and is
not part of the default pytest path.

GitHub Actions is reserved for documentation deployment. Run the former CI
checks locally before merging:

```bash
./scripts/validate-local.sh
```

The script validates the backend on Python 3.12 and 3.13, the documentation
site, and the Cloudflare API router. Pass `backend`, `docs`, or `router` to run
only selected groups, for example:

```bash
./scripts/validate-local.sh backend docs
```

## Documentation

Current rewrite documentation:

- [Rewrite status](docs/rewrite-status.md)
- [Core architecture](docs/votrix-core-architecture.md)
- [API reference](docs/api/index.mdx)
- [Agents](docs/agents.md)
- [Environments](docs/environments.md)
- [Memory Stores](docs/memory-stores.md)
- [Memory Stores on E2B Volumes](docs/memory-volumes.md)
- [Agent versioning](docs/agent-versioning.md)
- [Session events](docs/session-events.md)
- [Event streaming](docs/streaming.md)
- [Errors](docs/errors.md)
- [Service limits](docs/limits.md)

To run the narrative documentation site:

```bash
cd website
npm install
npm run dev
```

Run `npm run openapi:sync` after changing an API route or model. The checked-in
schema and generated API reference are derived from `app.server.create_app`;
CI fails when the snapshot drifts from the active application.

## Current gaps

- Organization/owner and model endpoints are registered placeholders.
- MCP definitions are persisted but not loaded. The stored `multiagent` roster
  is not consumed, and the built-in general-purpose `task` subagent is disabled.
- Vaults, deployments, webhooks, quotas, and audit/usage ledgers have not been
  implemented in the active service.
- E2B is the only active sandbox backend; there is no no-shell local fallback.
- Event streaming reads durable rows and uses PostgreSQL notifications when
  configured, with polling as the loss-safe fallback. Token previews are
  ephemeral notifications and are replaced by the final durable message.
- Cloud Tasks reduces duplicate enqueueing, but the callback payload does not
  carry the task generation and external side effects are not exactly once.
- Cloud dispatch commits input before enqueueing and has no outbox or
  reconciler; an enqueue failure can leave an accepted turn waiting for lease
  expiry. Concurrent duplicate callbacks have no worker-side atomic claim, and
  a delayed old callback can consume its old event batch during a newer
  `running` turn.
- Claiming an expired lease does not increment `lock_version`; a still-alive old
  worker is therefore not fenced from the replacement turn.
- A remotely deleted E2B sandbox whose database row still looks usable fails a
  turn but is not currently converted into a terminal Session.
- The runtime currently commits `session.status_idle` before output collection
  and the final row release. An SSE client can briefly observe that event before
  outputs are visible or the next turn is accepted.

## License

Proprietary. Copyright Votrix. All rights reserved.
