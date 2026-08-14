---
title: Core Architecture
description: Active service layers, state ownership, Session lifecycle, and runtime topology.
---

Snapshot: 2026-07-30

Active source: `app/` and `tests/`

This page describes the active implementation on the main branch. Superseded
code and migrations are available through Git history rather than being shipped
inside the runtime repository tree.

## Repository boundary

The runtime repository has one authoritative architecture:

```text
app/                 active service
tests/               active isolated test suite
tests_live/          active external-service test suite
```

Git history preserves the superseded implementation when migration archaeology
is needed; it is not imported, packaged, tested, or published by the active
service.

## System shape

```text
                         HTTP / SSE
                             |
                             v
                    +-----------------+
                    | FastAPI routers |
                    +-----------------+
                      | parse / render
                      v
                    +-----------------+
                    |    services     |
                    | use-case rules  |
                    +-----------------+
                      |       |       |
             SQL rows |       |       | external orchestration
                      v       |       +-----------> Cloud Tasks
              +---------------+       +-----------> E2B
              | queries / ORM |       +-----------> S3-compatible storage
              +---------------+
                      |
                      v
             SQLite or PostgreSQL

Turn execution:

Session input event
  -> atomic Session lease
  -> inline execution OR OIDC-signed Cloud Tasks callback
  -> Deep Agents graph
       -> configured model provider
       -> E2B filesystem and shell tools
       -> LangGraph checkpoint store
  -> durable Session events
  -> polling SSE clients
```

The rewrite is a single Python package with explicit layers, not a collection
of independently deployed domain services. Cloud mode may run the same ASGI app
behind separate API and callback URLs, but both processes use the same code and
database.

## Layer model

### Composition

`app/server.py` builds the FastAPI application, includes every active router,
and maps domain exceptions to HTTP responses. `app/main.py` exposes the ASGI
object as `app`.

The supported development entry point is:

```bash
uv run uvicorn app.main:app --reload
```

The `votrix_managed_agents` package export and top-level `entrypoint.sh` both
resolve to this same application factory.

### HTTP layer

`app/routers/` owns transport concerns:

- paths, methods, headers, query parameters, and response codes;
- FastAPI dependency injection;
- conversion from ORM rows to explicit public response models;
- SSE framing.

`app/models/` owns Pydantic request and response shapes. All API models use
`extra="forbid"`, so unknown request fields are rejected rather than ignored.

Routers call services and do not issue domain queries directly, with small
read-only conversions as the exception. A router should not decide when a
transaction commits or know how E2B and object storage work.

### Service layer

`app/services/` owns use cases and transaction boundaries:

- Agent creation, immutable updates, and archive rules;
- Environment recipe builds and readiness checks;
- File and Skill validation and storage coordination;
- Session creation, event admission, dispatch, turn execution, and output
  collection.

Services raise domain exceptions such as `NotFound`, `Conflict`,
`SessionBusy`, and `SandboxUnavailable`. `app/server.py` is the only layer that
turns those failures into HTTP status codes.

Successful mutating service calls commit their own transaction. This keeps
their behavior the same when called from HTTP, the Cloud Tasks callback, a
worker loop, or a test.

### Persistence layer

`app/db/models/` contains SQLAlchemy rows. `app/db/queries/` contains reads,
writes, atomic compare-and-set operations, and cursor pagination.

The query layer:

- accepts explicit `organization_id` for public resource lookups;
- returns rows or `None`, leaving public error semantics to services;
- flushes changes but normally leaves the final commit to the service;
- uses database-side updates where concurrency matters.

`app/db/engine.py` selects:

- `NullPool` for SQLite and non-PostgreSQL development URLs;
- a bounded, observed queue pool for PostgreSQL;
- `NullPool` for PostgreSQL only when `VMA_DB_POOL_SIZE=0`.

Connection checkout and SQL statement durations are recorded without logging
query parameters.

### Runtime layer

`app/runtime/engine.py` adapts public Agent and event concepts to Deep Agents:

- constructs the configured LangChain chat model;
- builds one Deep Agents graph per turn;
- restores the graph by Session ID from LangGraph checkpoints;
- maps user events to a fresh graph input or an interrupt resume command;
- translates LangChain messages and tool calls into public Session events;
- calculates the final `stop_reason` from graph state.

The runtime does not own public Session state. It writes through an emitter
provided by `app/services/sessions.py`, and that emitter checks the Session
generation before every write.

### Infrastructure adapters

`app/utils/` contains concrete infrastructure code:

- `sandbox.py`: E2B images, sandboxes, transfers, Skills, and output discovery;
- `volume.py`: provider Volume lifecycle and native Memory Store mounts;
- `storage.py`: private S3-compatible objects and signed URLs;
- `id_generator.py`: prefixed, time-sortable UUIDv7 identifiers;
- `timing.py`: structured duration reporting.

These adapters may use provider-native identifiers internally. Response models
publish VMA IDs rather than dedicated object-key or E2B-ID fields. Short-lived
presigned transfer URLs may still contain an object key in their URL path; the
service does not expose long-lived bucket credentials.

## Tenant and authentication boundary

Every implemented public resource is scoped by an Organization:

```text
request
  -> x-api-key lookup -> key.organization_id
     OR
  -> Supabase bearer verification
     + x-organization-id
     + user.id membership lookup
  -> service organization_id argument
  -> query predicate
```

Object storage adds the same partition to each key:

```text
organizations/{organization_id}/{category}/{date}/{object}
```

The dependency in `app/routers/deps.py` resolves exactly one trusted tenant by
one of two identity paths. API consumers send `x-api-key`; VMA hashes the key,
loads its active database record, and takes `organization_id` only from that
record. The first-party Console BFF sends a Supabase bearer token and selected
Organization; VMA resolves the live Supabase user and requires a matching
`organization_members.user_id` row in that active Organization. A superadmin
may select any active Organization. When an API key is present, its tenant wins
and a caller-supplied Organization header cannot override it. Revoked, expired,
malformed, and unknown keys, invalid user tokens, and missing memberships all
fail before a resource query runs.

The internal turn-processing endpoint has a different boundary. In cloud mode
it validates a Google OIDC token for:

- the configured `WORKER_URL` audience; and
- the exact `TASKS_SERVICE_ACCOUNT` email.

In inline mode that endpoint returns `404`, because no external caller has a
legitimate reason to use it.

## Domain and data ownership

The active Alembic chain starts with `down_revision = None`, so it is intended
for a fresh database. It does not reset or automatically upgrade a database
stamped with the superseded lineage; preserving that data needs an explicit
migration built from Git history. The active lineage owns:

| Table | Purpose |
| --- | --- |
| `organizations` | Organization identity data |
| `organization_members` | User membership and `owner`, `admin`, or `member` role; first-party access is resolved by exact `user_id` |
| `vma_api_keys` | Hashed Organization credentials; API-key authentication derives the request tenant from the key record |
| `agents` | Stable Agent handle and active version pointer |
| `agent_versions` | Immutable Agent configuration snapshots |
| `environments` | E2B image recipe and build state |
| `sessions` | Conversation state, pinned version, lease, and generation |
| `session_events` | Append-only client-visible transcript |
| `session_files` | Fixed input File paths attached to a Session |
| `session_sandboxes` | The one E2B sandbox bound to a Session |
| `memory_stores` | Persistent Store properties and provider Volume binding |
| `session_memory_stores` | Creation-time Session mount snapshots |
| `files` | File metadata and private object key |
| `skills` | Validated Skill metadata and private archive key |

There are two different durable histories:

```text
session_events
  Client-facing event log, sequence ordered, listable and streamable.

LangGraph checkpoints
  Runtime graph state: messages, interrupts, and where execution resumes.
```

They are intentionally separate. Rebuilding the Agent's internal graph state
from public events is not supported, and LangGraph checkpoint rows are not a
public authorization or audit surface.

PostgreSQL uses its own checkpoint tables in the configured database. SQLite
uses a separate `*.checkpoints.sqlite3` file beside the resource database, so
LangGraph migrations never mix with the Alembic-managed schema.

## Resource semantics

### Agents and versions

An Agent is a mutable pointer to an immutable snapshot:

```text
agents.active_version -> agent_versions.version
```

Creation writes version `1`. An update must name the version it was based on;
a stale version returns `409`. Omitted fields keep their active values, while
provided fields replace the whole value. If the resulting snapshot is
identical, no new version is written.

A Session stores `agent_id` and `agent_version` at creation. Later Agent edits
do not change a running conversation.

### Environments and images

An Environment is an E2B image recipe:

- CPU and memory are part of the recipe.
- Packages may be declared for apt, Cargo, RubyGems, Go, npm, and pip.
- An empty recipe uses the provider's base image immediately.
- A non-empty recipe starts an asynchronous E2B template build.
- Reads refresh a pending build's state because E2B does not call back into VMA.
- A Session may be created only when its Environment is ready.

Renaming or changing a description does not rebuild the image. Changing CPU,
memory, or packages does. Existing Sessions keep their already-created
sandboxes.

### Files and Skills

File bytes and Skill archives live in private S3-compatible storage; SQL rows
carry metadata and opaque storage keys.

File upload is a single bounded multipart request. The service measures size
and SHA-256 from received bytes, writes the object, then writes the row.
Downloads are `307` redirects to short-lived signed URLs.

Skill upload validates and normalizes a zip before storing it:

- the package must contain one valid `<name>/SKILL.md`;
- its frontmatter supplies the canonical name and description;
- unsafe paths, excessive expansion, too many entries, and invalid names are
  rejected;
- response models do not contain a dedicated storage-key field.

When a Session starts, its Skill archives and input Files move directly between
object storage and E2B through short-lived URLs. Those URLs may encode the
object key, but long-lived bucket credentials are not copied into the sandbox.

## Session lifecycle

### Creation

```text
POST /v1/sessions
  -> resolve Organization-scoped Agent
  -> pin active or requested Agent version
  -> resolve and validate ready Environment
  -> create Session row
  -> resolve and record fixed input Files
  -> start one E2B sandbox
  -> install Skills and inputs
  -> commit
```

Provisioning happens during Session creation, not on the first message. A
successful create proves the Environment and E2B sandbox are usable, but it
does not yet validate the stored toolset, model ID, or provider credential;
those can still fail on the first turn. Creation therefore needs E2B and can
take a network round trip.

The Session uses the same E2B sandbox for every turn. E2B may pause it after an
idle timeout; `LazyE2BBackend` reconnects before each backend operation. The
service never silently creates a replacement. A missing sandbox row, or one
already marked `terminated`/`failed`, ends the Session. If E2B reports a remote
sandbox missing while the row still looks usable, reconnect currently becomes
an ordinary failed turn and returns the Session to `idle`; it is not translated
into terminal Session state.

### Event admission and the single-turn gate

`POST /v1/sessions/{id}/events` accepts a batch as one turn trigger.

For ordinary events:

1. one SQL `UPDATE` claims an `idle` Session, or a `running` Session whose lease
   expired;
2. a busy Session returns `409` and writes nothing;
3. every input event receives an atomic, increasing `seq`;
4. the input and lease are committed;
5. the turn is dispatched.

There is no per-Session message backlog. A second message arriving during a
turn is refused rather than queued.

The expired-lease branch does not increment `lock_version`. If the old worker
is still alive after its heartbeat was delayed, both it and the replacement
turn retain the same generation; the old worker can continue renewing the lease
and emitting events. The explicit interrupt/release paths do increment the
generation, but lease takeover is not yet equivalently fenced.

`user.interrupt` is the exception: it must be sent alone and may reach a
`running` Session. It records the interruption, returns the Session to `idle`,
and increments `lock_version`.

### Dispatch

`TURN_DISPATCH` selects one of two paths.

#### Inline

The request calls `process_session` after committing its input. It returns only
after the turn completes or pauses. This needs no queue and is the default for
development.

#### Cloud Tasks

The API creates a task named from the Session ID and generation:

```text
turn-{session_id}-{lock_version}
```

Cloud Tasks sends the event batch to:

```text
POST /internal/sessions/{session_id}/process
```

with an OIDC token. A repeated enqueue with the same name is ignored.

This is enqueue-name deduplication, not delivery fencing or exactly-once
execution. The callback payload contains the event batch but not the generation
encoded in the task name. It only checks whether the Session is currently
`running`, then adopts the row's current generation. Consequently:

- two concurrent callbacks can both execute the same generation because the
  worker does not atomically claim it;
- a delayed callback for an older task can arrive while a newer turn is
  `running` and execute its old event batch as though it belonged to that turn;
- model calls, sandbox commands, and other external effects still need their
  own idempotency semantics.

There is also a dispatch-loss window: the API commits the input events and
`running` lease before it calls Cloud Tasks. An enqueue failure is not backed by
an outbox, reconciler, or compensating state change, so the accepted turn may
remain unprocessed until its lease expires.

### Runtime execution

For one turn, `process_session`:

1. reloads the Session without trusting a public Organization header;
2. checks that it is still `running`;
3. resolves the Session's sandbox and pinned Agent version;
4. starts a lease heartbeat on a separate database session;
5. builds the Deep Agents graph;
6. streams translated graph output through the fenced emitter;
7. collects new output files;
8. releases the Session to `idle`.

The turn budget is 600 seconds. The lease lasts 120 seconds and is renewed every
45 seconds.

Every emitted event is committed separately. The emitter refreshes `status`,
`lock_version`, and `last_event_seq` immediately before writing. If an explicit
interrupt, release, or other path advanced the generation, the stale worker
raises `SessionCancelled` and stops. This check cannot distinguish workers
after the unfenced expired-lease takeover described above.

### Checkpoints, tools, and pauses

The runtime requires the Agent to declare `agent_toolset_20260401`, which gives
Deep Agents its sandbox filesystem and shell tools. Tool permission settings
are converted to Deep Agents interrupts.

The active runtime also supports:

- `read_image`, implemented with `gemini-3.6-flash`;
- custom tools that always pause for a client-supplied result;
- Skills loaded by Deep Agents from `/home/user/skills`;
- tool confirmations resumed through LangGraph `Command`.

Current limitations:

- MCP server definitions are accepted and stored but not loaded.
- the stored `multiagent` roster is accepted and versioned but not consumed;
  Deep Agents' built-in general-purpose `task` delegation remains available
  independently;
- `web_fetch` is not installed.
- `web_search` is installed when requested but currently raises
  `NotImplementedError`; it is not production-ready.
- Only text content blocks are accepted on the current event surface.

### Output collection

The sandbox workspace is:

```text
/home/user/uploads   fixed Session input Files
/home/user/skills    installed Skill packages
/home/user/outputs   deliverables collected after each turn
```

Output discovery is additive. The service hashes each eligible file and skips a
path whose latest collected version has the same digest. A changed file creates
a new File row rather than mutating the old ID.

On a normal turn, collection finishes before the Session row is released to
`idle`. The graph currently emits and commits `session.status_idle` before
control returns to `process_session` for collection, however. An SSE client can
therefore observe the idle event before output rows are visible or before the
next input can claim the Session. An interrupt also releases the row before the
cancelled worker performs its best-effort collection. This ordering is a
rewrite gap, not a readiness guarantee.

## Event delivery

`session_events` is the delivery source of truth. Sequence numbers are allocated
with an atomic database update so a user interrupt and worker output cannot
receive the same value.

The SSE route:

- replays from the beginning when no cursor is supplied;
- resumes after `after_seq` or `Last-Event-ID`;
- polls up to 100 durable events every 300 ms;
- opens a fresh short database session per poll;
- sends a keep-alive comment after 15 seconds of silence;
- closes after 30 minutes or when the Session terminates.

There is no process-local preview bus or PostgreSQL `NOTIFY` broker in the
rewrite. SSE may add up to one poll interval of latency, but reconnecting does
not lose already committed events.

## Model boundary

`app/models/llm.py` contains a hard-coded catalog for Anthropic, Google,
OpenAI, and DeepSeek models. `app/runtime/engine.py` maps each entry to its
LangChain integration.

Provider credentials are process settings:

```text
ANTHROPIC_API_KEY
GEMINI_API_KEY
OPENAI_API_KEY
DEEPSEEK_API_KEY
```

A caller chooses a known model ID but never supplies a credential. There is no
per-Organization Vault, BYOK binding, provider registry, fallback routing, or
usage ledger in the active app.

## Background process

`python -m app.worker` runs a sweeper, not a turn worker. Once a minute it finds
`running` Sessions with expired leases, appends `session.error`, and returns
them to `idle`.

The expired lease already lets the next input claim the Session. The sweeper
only makes failure visible before a client happens to send that input.

## Configuration boundary

`app/config.py` intentionally keeps a small settings surface:

- database URL and PostgreSQL pool sizing;
- one API key per supported model provider;
- S3-compatible storage credentials;
- E2B credentials and bounded sandbox/output settings;
- inline versus Cloud Tasks dispatch.

Unknown environment settings are ignored because Pydantic is configured with
`extra="ignore"`. The checked-in `.env.example` mirrors the active settings
surface; `app/config.py` remains the source of truth.

## Invariants

- Public resource queries must include `organization_id`.
- Public responses must be constructed explicitly and must not expose
  Organization IDs, storage keys, sandbox IDs, lease data, or lock versions.
- Agent versions are immutable; Sessions pin an exact version.
- A Session owns exactly one sandbox.
- The public admission gate accepts only one ordinary turn at a time; expired
  takeover and cloud callback execution are not independently fenced.
- An emitter whose captured generation differs from `lock_version` may not
  append an Agent event.
- Client-visible events are durable before SSE can publish them.
- LangGraph checkpoint state and public event state remain separate.
- Object keys start with an Organization partition.
- A File row is not written until its object exists.

## Current integration gaps

The active core is internally coherent, but the repository around it is still
mid-migration:

- authentication and authorization are not implemented;
- Organization and model route handlers are placeholders;
- several previous resource families have not been ported;
- cloud dispatch has neither an enqueue outbox, a worker-side atomic claim, nor
  task-generation validation;
- expired-lease takeover does not advance the generation;
- a remotely missing E2B sandbox is not made terminal unless its database row
  is already missing or marked unusable;
- `session.status_idle`, output collection, and row release are not yet one
  atomic completion boundary;
- production authentication and authorization remain unimplemented.

The exact inventory and documentation authority are maintained in
[rewrite status](./rewrite-status.md).
