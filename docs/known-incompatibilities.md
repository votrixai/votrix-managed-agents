---
title: Known Incompatibilities
description: Important behavioral and infrastructure differences from Claude Managed Agents.
---

Snapshot: 2026-07-20
Runtime kernel: Deep Agents 0.6.12

This is the explicit gap ledger between VMA and [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview). It covers differences that can be hidden by a compatible route or response shape but materially affect execution, security, durability, or operations.

VMA is an independent implementation. The goal is to make incompatibilities visible and actionable, not to imply that another model provider or a self-hosted process reproduces Anthropic's private harness and managed infrastructure.

Availability and readiness labels match the [compatibility matrix](./compatibility-matrix.md). They state whether a capability is public, preview-only, operator-configured, constrained, or unavailable; they do not treat Claude equivalence as the same question.

## Summary

| Difference | Availability and VMA readiness | Operational consequence |
| --- | --- | --- |
| Cross-process live preview broker | Hosted infrastructure — supported | Hosted API/worker services use PostgreSQL `pg_notify`; local/self-hosted deployments default to the in-process transport. Preview frames remain best-effort and non-replayable. |
| Distributed turn ownership | Runtime — constrained | Database work and Session execution leases support the maintained multi-instance worker fleet and fence stale terminal writes, but provider and sandbox side effects are not exactly-once. |
| E2B remote sandbox | Operator-configured — constrained | Optional isolated execution exists, but E2B is hosted unless deployed through BYOC and is not Claude's managed sandbox service. |
| Dynamic Session files and output export | Public beta subset — constrained | E2B supports bounded append-only inputs, direct output snapshots, and mounted-file block translation, but not arbitrary mount mutation or model-independent Claude-identical multimodal behavior. |
| Provider behavioral parity | Provider-dependent — equivalence not claimed | Tool calls, streaming, reasoning, multimodal input, usage, and failures vary by model/provider. |
| `deepseek-reasoner` runtime | Not supported | The current Deep Agents adapter rejects it because the harness requires tool calling. |
| Exact declared subagent roster | Runtime — constrained | Deep Agents can add a built-in general-purpose subagent when `task` is exposed. |
| Claude durable multiagent threads | Not offered | Deep Agents synchronous subagents are ephemeral; background subagents use another protocol. |
| MCP OAuth lifecycle | Not available | Stored/matched access tokens are not automatically refreshed or revoked. |
| Anthropic system skills | Not offered | Their private packages and behavior are unavailable to VMA. |
| Claude memory mount/writeback | Repository preview — no writeback | E2B receives a one-time bounded memory seed; sandbox edits persist there but are not written back to managed Memory Store versions. |
| Production deployment scheduler | Operator tick only — no running service | An idempotent tick exists, but no always-on scheduler service is included. |
| Webhook delivery | Not offered | Cryptographic helpers exist, but endpoint management and delivery do not; webhooks are not a public-beta product promise. |
| User-profile enrollment and attribution | Repository preview — lifecycle only | Profile CRUD exists, but enrollment, trust grants, and forwarding a profile ID to model providers do not. |
| Organization RBAC/SSO and Postgres RLS | Hosted owner/superadmin subset | Hosted Supabase owners and platform superadmins are supported, but member roles, group mapping, SSO policy, and Postgres RLS are not. |
| Tenant quotas and raw ledgers | Public beta — supported | Request, active-work, daily token, and stored-byte limits plus append-only audit/usage facts provide the public-beta baseline, not enterprise policy or priced billing. |
| Billing and payments | Deferred product — not offered | Operator-provisioned, upstream-limited Organization keys can fund trials, but price books, authoritative balances/reservations, top-ups, refunds, Stripe, and invoices are outside this release. |

## API and SDK surface

### Strict SDK tests are not semantic parity

Covered resources are parsed with strict validation by a pinned official Anthropic Python SDK. This establishes useful field and union compatibility for tested calls. It does not verify:

- Every request field, filter, pagination edge, error type, or future SDK release.
- Equivalent session timing, background execution, tool behavior, or model output.
- Anthropic infrastructure guarantees, data handling, retention, or rate limits.
- Compatibility with every language SDK.

The route-by-route inventory is [Managed Agents API coverage](./managed-agents-api-coverage.md).

### Native VMA SDK and Anthropic compatibility are separate contracts

New VMA integrations can use the independently packaged native Python clients:

```python
from votrix import AsyncVotrix
from votrix import Votrix
```

`AsyncVotrix` keeps the familiar resource-oriented GA API while `Votrix`
provides the synchronous API-key, model-provider, Vault, and native
model-Credential administration subset. Both expose provider discovery and
provider-based model-Credential creation. The SDK also supplies cursor
pagination, true streamed downloads, reconnecting SSE on the async client,
bounded replay-safe retries, and typed stable error codes. The official
`AsyncAnthropic` client remains useful for the overlapping Claude Managed Agents
wire surface. A native SDK test does not establish Anthropic compatibility, and
an Anthropic strict-response test does not establish support for VMA extensions.
Applications must keep resource IDs and persistence separated by control-plane
provider during migration.

### Session overrides cannot make providers behaviorally identical

VMA supports Claude's three create-time Agent forms, including
`agent_with_overrides` for model, system, tools, MCP servers, and Skills. Each
provided field is a full replacement: `system: null` clears the system prompt,
empty arrays clear tools, MCP servers, or Skills, and null is rejected for those
array fields. `model: null` is rejected. The resolved configuration is pinned in
the Session without changing the base Agent version. Custom Skill `latest`
references are resolved to a concrete version before sandbox bootstrap. After
creation, only tools and MCP servers are mutable while the Session is idle.

This wire and lifecycle compatibility does not make a DeepSeek, MiniMax,
OpenRouter, OpenAI, or other model reproduce Claude's tool calls, reasoning,
streaming, usage accounting, or errors. VMA also permits a Session Vault
credential to supply the selected model provider's API key, which is a VMA
extension rather than a Claude Managed Agents inference feature.

Consequence: a request accepted by the newest official SDK may fail validation or may not affect graph compilation exactly as it would on Claude.

### Event union and steering are incomplete

VMA persists an append-only event log and implements common user, agent, tool, status, outcome, and resource shapes. The complete Claude event union, all span nesting, processed-time transitions, mid-run steering, interrupt redirection, and every validation rule are not present.

Consequence: clients should tolerate unknown/missing optional events and must not infer Claude-identical span traces from VMA events.

### Beta headers can drift

VMA recognizes the Managed Agents, skills, user-profile, and native VMA beta headers used by its contract suite. Claude can introduce feature-specific beta headers and change beta behavior independently. Header acceptance in VMA must be updated deliberately rather than treated as automatic upstream support.

## Live streaming and process topology

### Preview delivery is deployment-selectable and best-effort

Durable Session events are stored in the database and replayed by sequence
number. Preview frames are a separate low-latency channel for live token/tool
deltas:

- `VMA_PREVIEW_BROKER=process_local` is the default for local development and
  preserves the original same-process behavior.
- The checked-in production and staging services set
  `VMA_PREVIEW_BROKER=pg_notify`. Workers publish Organization/Session-scoped
  frames through PostgreSQL `NOTIFY`; API instances `LISTEN` and feed them into
  their local SSE bus.
- An API process suppresses its own loopback notification, so a same-instance
  publish is delivered once rather than duplicated.

PostgreSQL `NOTIFY` is fire-and-forget, and VMA deliberately coalesces high-rate
deltas. A disconnect, oversized frame, subscriber overflow, or process failure
can therefore drop an ephemeral preview. Preview frames are not replayed.
Clients must reconcile with the complete durable event carrying the same event
identity; durable events remain the source of truth.

Hosted PostgreSQL uses separate connection modes. `DATABASE_URL` and
`VMA_CHECKPOINT_DATABASE_URL` use the Supavisor transaction-mode endpoint on
port `6543`; preview publishers therefore use the ordinary transaction-mode
application pool. `VMA_LISTEN_DATABASE_URL` uses the session-mode endpoint on
port `5432` for each API process's lifetime `LISTEN` connection and for the
janitor's session-scoped advisory lock. Budget one additional connection per
API (or combined-role) process, plus the transient janitor lock connection.
The migration Job receives a separate session/direct URL rather than either
runtime URL. Redis, NATS, and Pub/Sub are not required by the maintained
preview topology.

### Turn execution is database-fenced, not exactly-once

The Session Events endpoint now supports an optional `Idempotency-Key`. A successful keyed request stores its canonical request hash and exact response in the same transaction as its input events and queued work. Reusing the key with the same body replays that response; reusing it with a different body returns `409`. `votrix-backend` supplies this header and reuses it across SDK and 529 transport retries.

Session creation separately uses a generic tenant idempotency table scoped by
Organization, operation, key hash, and request fingerprint; the native SDK
generates a key when callers do not supply one. The mechanism is intentionally
not presented as a guarantee for every mutation.

This request-level guarantee does not yet make runtime execution exactly-once.
A new Backend process does not yet persist a logical `/chat` request identity.
Every work attempt now receives a unique lease ID and monotonically increasing
generation, heartbeats its lease, and must still own that generation before it
can commit terminal work/session events. Expired leases are recoverable, and a
stale worker is fenced from finalizing a newer attempt. Those guarantees protect
one durable work item; they are not yet a general distributed mutex covering
all per-Session checkpoint writes and every side effect.

The maintained Cloud Run deployment uses database-backed work and Session
execution leases across multiple API and worker instances. Checkpoint state is
shared in PostgreSQL, stale generations are fenced before durable event and
terminal writes, cleanup is serialized with a PostgreSQL advisory lock, and
work retries have a terminal attempt cap. Named Cloud Tasks drive the private
worker service from one to eight production instances or one to two staging
instances, with at most five turns per instance. A permanent PostgreSQL
reconciler recovers missed dispatches and expired leases.

These guarantees do not turn remote effects into exactly-once operations. A
worker can lose its lease after a provider request or sandbox command has
started, and recovery may repeat an effect that lacks its own idempotency key.
Keep external tools idempotent where possible and treat durable work/event
state—not Cloud Tasks or preview delivery—as authoritative. Cloud Tasks
provides per-turn push dispatch and request-driven worker autoscaling; the
PostgreSQL reconciler remains the correctness fallback rather than the primary
scale signal.

### Cancellation is not end-to-end

Changing a public Session state or cancelling an async task does not prove that an in-flight MCP request or remote command has stopped. The current E2B path has no sandbox-operation lease, heartbeat, fencing token, or durable cancellation outbox. Turn exit attempts to pause the sandbox and Session deletion attempts to kill it, but production still needs cooperative command cancellation, hard termination, and distributed run ownership.

## Sandbox and environment behavior

### The optional E2B provider is not Claude's sandbox service

Claude cloud environments include Anthropic-managed isolated execution infrastructure and lifecycle behavior. VMA has three materially different choices:

- Default: Deep Agents `StateBackend`, with checkpointed files and no shell.
- Optional `sandbox-e2b` extra: `langchain-e2b==0.0.5` and `e2b==2.31.0`, selected with `VMA_SANDBOX_PROVIDER=e2b`, a server-owned `E2B_API_KEY`, and a required operator-owned `VMA_E2B_TEMPLATE`.
- Custom integration: `VMA_SANDBOX_FACTORY` returning an operator-provided backend.

The E2B path provisions exactly one isolated sandbox while creating a Session.
It uploads the initial bundle, seals the immutable inputs at revision `0`,
pauses with full-memory preservation, and later reconnects the same opaque
external ID. While the active Session is idle—including an idle
`requires_action` custom-tool window—`sessions.resources.add` may append one
read-only direct file at `/mnt/session/uploads/<filename>` and advance the
sealed manifest by one revision. Existing mounts cannot be replaced, updated,
or deleted. Before every turn VMA recomputes the latest committed input identity
and verifies the stored seal; resume never re-uploads, repairs, or synchronizes
inputs. Deep Agents receives an `AsyncE2BSandbox` backend and invokes command
and file operations through tool calls.

Provider auto-resume is disabled so every reconnect passes through VMA authorization. Secure access is enabled and public traffic is disabled. Archive preserves the sandbox, deletion kills it, and best-effort retention cleanup defaults to 30 days. E2B remains an external dependency unless the operator deploys [E2B BYOC](https://e2b.dev/docs/byoc), and VMA makes no guarantee that its performance, failure behavior, isolation implementation, or lifecycle is behaviorally equal to [Claude Managed Agents sessions](https://platform.claude.com/docs/en/managed-agents/sessions). See [sandbox runtime](./sandbox-runtime.md).

The hardened template is the trusted computing base for guest filesystem confinement. VMA verifies the configured non-root guest, absence of passwordless system sudo, a trusted system Python, and non-writable `/usr/bin` and `/usr/lib`; root bootstrap uses isolated `/usr/bin/python3 -I -S`. It does not scan or harden the whole Linux image. E2B recreates guest-writable `/usr/local`, `/code`, and `/home/user` paths at runtime, while `/tmp` and `/var/tmp` remain writable sticky scratch. They are untrusted Session-local paths rather than durable VMA storage. E2B may also expose its own sandbox/team/template identifiers inside the guest runtime even though VMA omits the external sandbox ID from control-plane API responses.

### Sandbox lifecycle has no recovery protocol

VMA records one external E2B sandbox ID as opaque private database state; it is
not a public resource identifier or an authorization boundary. Database
transactions and provider side effects cannot be atomic. A failed Session
create attempts to kill a sandbox it already created. During file append, VMA
advances E2B's compare-and-swap seal before committing the resource and latest
manifest in PostgreSQL. A crash in that window is recoverable only by retrying
the exact same file bytes and mount path; the provider accepts the expected old
seal or that exact already-advanced seal. Unrelated append/resume attempts fail
closed until recovery. VMA has no provisioning generations, snapshots,
operation leases, heartbeat/fencing, durable outbox, or automatic orphan
discovery/recovery. Operators must inspect failed creates/appends and E2B
inventory.

When retention cleanup is enabled, VMA's in-process janitor makes eligible paused sandboxes best-effort cleanup candidates after a configurable threshold, which defaults to 30 days. This is not an E2B retention guarantee or exact deletion deadline. Scale-to-zero and suspended API services also suspend the loop, so overdue cleanup waits for a later API start. Once the janitor or Session deletion kills a sandbox, its filesystem and process state cannot be resumed; conversation events follow their separate database retention policy.

### Environment policy support depends on the provider

VMA validates and summarizes network, package, and resource fields. The E2B adapter maps supported `none`, `limited`, and `unrestricted` egress policies, enables secure access, and disables public traffic. Limited mode passes destinations through E2B's `allow_out`; VMA does not add a separate `deny_out` or deny-all rule, so the effective guarantee depends on E2B's service semantics. Other environment declarations do not automatically acquire Claude semantics:

- CPU, memory, disk, and preinstalled packages are selected at the E2B template level rather than sized independently for every session request. VMA compares requested values with the operator-declared `VMA_E2B_TEMPLATE_RESOURCES` profile but does not independently attest that profile in this MVP.
- Package caches and installed environment state are not shared across session sandboxes. A custom template is required for a common prepared image.
- Package changes made by the guest remain only in that one sandbox until deletion or cleanup.
- `networking.allow_mcp_servers` does not create sandbox egress exceptions because VMA's MCP clients currently execute in the control plane.

A custom factory that ignores `allowed_hosts`, package-manager policy, memory, disk, CPU, or timeout can still produce an unrestricted sandbox even if the Environment response says otherwise. Every provider must fail closed when it cannot implement a required policy.

### Filesystem tool policy is not shell policy

Deep Agents filesystem permissions apply to built-in file tools. Shell commands can access files without those tools, and direct backend methods bypass middleware. Deep Agents 0.6.12 explicitly cannot generally combine path permissions with an execution backend because `execute` can bypass them.

Only the sandbox can enforce actual filesystem/process/network authority.

### Self-hosted work queue is not self-hosted isolation

The environment work API, embedded consumer, and `vma-worker` durably lease and
execute work. Each attempt has a unique lease ID and generation; ack,
heartbeat, execution, and terminal writes verify current ownership, and expired
leases can be recovered. This fences a stale worker from completing a newer
attempt.

The queue does not create a container or VM, verify a worker image, attest
policy, or isolate tenants. The worker must still use an approved sandbox
provider. Strong worker identity/RBAC, sandbox attestation, dead-letter policy,
and operation-specific idempotency/cancellation for remote side effects remain
gaps.

## Model and provider behavior

### Deep Agents is the harness, not Claude's harness

VMA uses Deep Agents middleware for planning, filesystem tools, summarization, checkpoint state, human interrupts, skills, and delegation. Prompt text, compaction thresholds, tool descriptions, error recovery, and loop behavior differ from Anthropic's private managed harness.

Deep Agents 0.6.12 is a beta dependency. Provider/harness profiles are process-global, and some APIs have announced 0.7 removals. VMA must pin versions and keep the integration behind its own adapter.

### Provider capabilities vary

Anthropic, OpenAI, DeepSeek, and custom LangChain providers differ in:

- System-message and prompt-cache semantics.
- Context-window reporting and compaction behavior.
- Tool schema support, tool choice, and parallel calls.
- Streaming chunk and tool-argument fragmentation.
- Reasoning content and whether it can be exposed or replayed.
- Native structured output and JSON-schema dialect.
- Image/audio/document content blocks.
- Token and cost accounting.
- Timeout, retry, cancellation, and rate-limit errors.
- Retention, region, safety, and abuse controls.

The VMA capability record is an operator assertion used for routing; it does not test or emulate a provider. See [model providers](./openai-compatible-providers.md).

### DeepSeek reasoner does not support tools in VMA

VMA explicitly changes the capability of `deepseek-reasoner` to `tool_calls=false`. The current adapter rejects all non-tool-calling models before graph compilation because the Deep Agents harness itself depends on tools; disabling the public toolset does not make this model runnable. Use `deepseek-chat` or another tool-capable model.

Even with `deepseek-chat`, event content, reasoning, structured output, and usage are not Claude-identical.

### OpenAI-compatible does not mean Responses-compatible

Many compatible endpoints only implement Chat Completions. VMA therefore defaults `ChatOpenAI` to `use_responses_api=false`. Enabling Responses against an endpoint that lacks it will fail. Native OpenAI retention controls also need explicit operator configuration; selecting the OpenAI provider does not automatically satisfy a data-retention requirement.

### Hosted tools are replacements

VMA's `web_fetch` is a bounded public-URL HTTP client, and `web_search` calls an operator-configured JSON endpoint. Ranking, citations, crawl policy, geographic availability, abuse controls, and result formats do not match Anthropic's hosted tools.

## Tools and human-in-the-loop continuation

### Tool schemas and results differ

VMA maps Claude-facing names to Deep Agents names:

```text
bash -> execute
read -> ls + read_file
write -> write_file
edit -> edit_file
glob -> glob
grep -> grep
```

Arguments, path handling, result strings, truncation, error messages, and event timing can differ. Clients should consume the public VMA events, not depend on raw Deep Agents tool output.

### Approval coverage is limited

`always_ask` maps to LangGraph human-in-the-loop interrupts for supported tools, and VMA can represent approval/denial continuation events. Remaining risks include:

- Restart-safe persistence and exact matching of every result to its interrupt.
- Edit/respond decision parity.
- Multiple simultaneous interrupts and decision ordering.
- MCP approval event parity.
- Provider retries after an approved tool call.
- Cancellation while waiting for action.

Approval expresses user intent; it does not grant sandbox authority. The backend must independently reject forbidden operations.

### Custom tools remain caller-owned

A custom tool call must stop and wait for an application-provided result. VMA has the event and runtime mapping, but every crash/retry/idempotency path is not yet equivalent to Claude. Applications should use stable tool-call IDs and make result submission idempotent.

## Multiagent behavior

### Roster versions are pinned, execution is not equivalent

VMA pins unversioned coordinator roster entries when an agent version is created. That prevents roster drift. At runtime those entries become Deep Agents declarative `SubAgent` objects.

A Deep Agents synchronous subagent:

- Receives a new task message rather than the parent conversation.
- Runs ephemerally for one call.
- Returns one final message or structured response to the coordinator.
- Does not expose a durable independently steerable VMA session thread.

Claude multiagent sessions instead expose durable thread resources and event streams while sharing session infrastructure. VMA thread response objects therefore remain a shape-only repository preview even when their shapes validate.

### Built-in general-purpose subagent can expand the roster

Deep Agents adds a general-purpose subagent by default when the `task` tool is active. VMA can hide `task` when an agent has no multiagent roster. When a roster exists, however, the upstream built-in may appear alongside the declared agents.

Disabling it requires an active Deep Agents harness profile. Harness profiles are process-global and are not safe to mutate per tenant or per request. Exact declared-roster behavior needs one of:

- A small adapter/upstream change exposing a per-graph opt-out.
- A process-stable profile policy that applies to every compiled graph.
- A VMA-owned delegation tool that replaces the upstream `task` surface.

Until then, tool availability and cost can differ from the declared Claude roster.

### Background subagents use a different protocol

Deep Agents `AsyncSubAgent` starts work through LangGraph Agent Protocol and exposes `start_async_task`, `check_async_task`, `update_async_task`, `cancel_async_task`, and `list_async_tasks`. Claude multiagent threads use the Managed Agents session/thread/event API.

VMA does not currently bridge these protocols. Enabling upstream async subagents directly would expose different IDs, auth, status, cancellation, and event semantics. Remote output also re-enters the parent as untrusted prompt content and needs tenant-authenticated transport.

## MCP and vault credentials

### Static connection support is limited

VMA validates MCP server/toolset references, matches session-vault credentials by normalized server URL, strips secrets from public run state, and can supply authorization headers to remote MCP clients. Missing credentials emit a session error rather than preventing resource creation.

These MCP clients currently execute in the control-plane process, not through `AsyncE2BSandbox`. Consequently, an environment's `allow_mcp_servers` flag does not translate into an E2B outbound-network rule, and sandbox egress policy does not contain an MCP tool's own network authority.

The following remain incomplete:

- OAuth authorization/enrollment flow.
- Access-token expiry checks and refresh-token exchange.
- Client-secret authentication at the token endpoint.
- Refresh rotation and atomic credential replacement.
- Revocation and disconnect.
- Server metadata/capability discovery and drift handling.
- Health probes, reconnect/backoff, and circuit breaking.
- Fine-grained tenant permission checks per MCP tool.
- Complete approval and error event parity.

Claude manages OAuth refresh for supported vault credentials. VMA currently does not. An expired token will remain expired until an external component updates it.

### Secret storage is not a managed vault

`VMA_ENCRYPTION_KEY` enables AES-256-GCM encryption for recognized credential fields. Plaintext persistence is limited to explicitly allowed local/test mode; other environments fail closed when writing a secret without a key. Even with encryption, VMA does not provide KMS-backed envelope encryption, key rotation, per-tenant keys, access audit, secret versioning, or break-glass policy.

Production should supply a managed secret provider and never place provider keys or MCP headers in public events, previews, checkpoints, traces, or sandbox environments.

For model BYOK, native callers create a model Credential with a registered
provider ID and the key; VMA performs the private provider-to-credential-slot
mapping. Callers do not supply `secret_name` or `api_key_env`. Session creation
then resolves attached Vaults in declared `vault_ids` order and fixes the first
matching Credential. VMA persists only the selected Credential ID and reloads
that exact record for later turns. Archiving or deleting it fails the Session's
next turn closed; it does not switch the Session to another Vault or to the
server-owned key. If no matching Vault Credential exists at creation, the
Session binds the server-owned key source. The trusted caller expresses
personal/shared preference through `vault_ids` ordering rather than a VMA
policy enum. Model keys are decrypted only for the control-plane model client
and are not injected into E2B.

## Files, skills, and memory

### Public mount paths are portable; enforcement depends on the provider

Private R2 or another private S3-compatible object store supplies uploaded
files and custom Skill archives. VMA requires no bucket public URL and serves
downloads through the authenticated Files API. Public GA hides the
presign/complete routes; beta callers use the bounded authenticated upload.
Organization stored-byte quota is checked for File and Skill writes, but storage
retention and garbage-collection policy still need operational ownership.

VMA materializes Skills, Memory Store seeds, and create-time
files into the one E2B sandbox during Session creation. A later file may be
added only through the bounded append protocol: the Session and sandbox must be
idle/paused, the path must be one direct filename under
`/mnt/session/uploads`, and the existing manifest must remain an unchanged
subset of the next revision. VMA checks Organization ownership plus the copied
object's size and SHA-256 before touching E2B. Skills and memory remain fixed;
existing file mounts cannot be updated or removed. Immutable inputs cannot
overlap `/workspace`, `/mnt/session/outputs`, or a read-write memory root. The
control plane never performs per-turn input upload, repair, deletion, or
continuous synchronization.

The built-in E2B adapter rejects traversal, symlinks, aliases, and path collisions, then makes control-plane-owned immutable files root-owned and verifies their digest before each turn. This protects those files only. Persistent private Agent data belongs in `/workspace` or approved read-write memory roots, and generated files intended for export belong directly in `/mnt/session/outputs`; E2B also supplies untrusted Session-local writable scratch and package paths. The required operator-owned template must protect the system roots used by VMA; VMA checks the non-root/no-passwordless-sudo boundary plus its trusted system interpreter and directories, without implementing general Linux hardening.

E2B Session creation currently rejects `github_repository` resources because VMA does not yet implement secure one-time checkout and credential handling. The response union remains available for compatibility on non-E2B control-plane resources, but it must not be interpreted as a completed E2B mount.

Managed Agents image/document blocks may reference only an immutable file
already mounted in the same Session. VMA accepts the source upload ID or its
Session-scoped copy ID, keeps the public event unchanged, and converts verified
JPEG/PNG/GIF/WebP, PDF, or UTF-8 text bytes into LangChain standard model input.
It rejects arbitrary URL/inline sources and cross-Session IDs. Image/PDF inline
behavior still depends on the selected model profile declaring multimodal
input. A text-only profile such as the default DeepSeek/OpenRouter route gets a
sandbox-path marker and must use sandbox tools; that is not Claude-equivalent
vision or native document understanding.

`/mnt/session/outputs` is a guest-owned mutable root. At the end of an E2B graph
execution, VMA discovers only bounded direct regular files there, rejects
nested paths, directories, symlinks, hardlinks, and out-of-root entries, then
snapshots each new `(Session, path, SHA-256)` version into R2-compatible storage
as a downloadable Session-scoped File. Exact rediscovery is idempotent; changed
bytes at the same path produce another immutable File record. These artifacts
are available through `files.list(scope_id=session_id)`, metadata, and download.
Files left elsewhere in the sandbox are not exported and can disappear when
retention cleanup or Session deletion kills it. Production malware quarantine
and content-disarm policy are also absent.

### Custom skills use Deep Agents semantics

VMA validates custom archives, versions them, and can materialize their top-level directory. Deep Agents reads Agent Skills `SKILL.md` metadata and injects skill guidance progressively.

Differences from Claude include:

- Prompt wording and when a skill is disclosed to the model.
- Skills are pinned and uploaded only when the Session is created; changing one requires a new Session.
- Available interpreters, binaries, network access, and filesystem layout.
- Security scanning beyond basic archive validation and local EICAR checks.
- No guarantee that identical skill files produce identical model behavior.

Skill instructions are trusted prompt input and can inject behavior. Tenant boundaries and publication policy must be enforced outside the model.

### Anthropic system skills are unavailable

The public API can accept references whose type is `anthropic`, but `_skill_archives_for_runtime` deliberately skips them. VMA does not possess Anthropic's private system-skill content or hosted integrations.

A compatible response containing a system-skill reference therefore does not mean that skill ran. Operators need an explicit local replacement with independently licensed content and must label it as a replacement, not the Anthropic implementation.

### Memory runtime semantics differ

The VMA Memory Store API has path records, limits, optimistic content preconditions, immutable versions, deletion history, and filters. When an E2B-backed Session is created, the adapter loads a bounded subset—up to eight mounted stores, up to twenty records per store, and up to 1,000 characters per record—into that sandbox once. It creates an `AGENTS.md` source listing those files for Deep Agents memory guidance.

It does not currently provide:

- A complete seed containing every memory path rather than the bounded selection.
- Dedicated agent read/write/list/delete Memory Store tools and durable writeback. E2B protects `read_only` seeds, while `read_write` edits persist only inside that Session's sandbox.
- Automatic persistence of model edits back into memory versions.
- Actor/session attribution for writes produced by the graph.
- Conflict resolution between concurrent sessions.
- Semantic/vector retrieval.
- Claude's exact memory instructions or dreaming/research-preview behavior.

Deep Agents' `MemoryMiddleware` reads the generated `AGENTS.md`. Changes made to read-write seeded files survive later turns through the same sandbox, but they are not written back to database Memory Store versions. The sandbox filesystem and managed Memory Store API must not be described as equivalent.

## Checkpoints, resumption, and state

### LangGraph checkpoints are internal

VMA can select a durable Postgres checkpointer and uses an opaque thread ID stored on the session. A checkpoint contains LangGraph/Deep Agents state, not a Claude session export. It is not a public API contract and can change when dependency versions or graph topology change.

For E2B Sessions, LangGraph state and the persistent E2B sandbox solve different problems. LangGraph restores the agent loop; reconnecting the exact stored external ID restores the existing runtime filesystem and process memory retained by full-memory pause. VMA has no sandbox generation, provider-snapshot, or automatic migration layer. Neither SDK object is persisted. Cleanup after the configured retention threshold can end sandbox resumability without deleting conversation history.

An interrupted run must resume with:

- The same Organization-scoped internal thread ID.
- The same pinned agent revision.
- The same E2B external ID, fixed Skills/Memory identity, and latest committed
  append-only input digest, manifest revision, and immutable-file seal.
- A compatible graph and middleware topology.
- Decisions ordered against the saved interrupt action requests.

Deploying a changed graph and resuming an old checkpoint can fail or behave differently. Production needs revision-aware workers and a checkpoint migration/retention policy.

### Checkpoint isolation is application-enforced

LangGraph checkpointers primarily key state by thread configuration. VMA must never authorize checkpoint access from a caller-supplied ID alone. Public session lookup must be Organization-scoped and map to a server-owned opaque thread ID. Stronger deployments should add database RLS, per-tenant schemas, or a checkpointer wrapper that incorporates tenant context.

### Compaction differs

Deep Agents uses model-aware summarization and compact message state. Its token thresholds, retained messages, media handling, offload paths, and summary prompt differ from Claude's managed compaction. Summaries can change behavior and may contain sensitive content that must follow the same retention policy as the conversation.

## Deployments and webhooks

### Deployment scheduling is not operated automatically

VMA persists deployment resources, validates cron/timezone data, computes upcoming timestamps, creates manual runs, and provides an idempotent due-schedule function. No always-on scheduler invokes that function in the Votrix core.

A production service still needs:

- Leader election or a managed scheduler.
- Catch-up and misfire policy.
- Distributed idempotency/fencing.
- Retry and dead-letter handling.
- Jitter/concurrency limits.
- Deployment/run usage attribution and quotas.
- Monitoring and alerting.

Creating a scheduled deployment therefore does not guarantee that it will fire unless the operator runs the scheduler integration.

### Webhook delivery is absent

`app.webhooks` can sign, verify, freshness-check, and unwrap Standard Webhooks-style envelopes. VMA does not provide:

- Endpoint registration or ownership verification.
- Event subscription filters.
- Delivery attempts and retry schedule.
- Idempotency and replay controls.
- Secret rotation.
- Failure counters and endpoint disabling.
- Delivery observability.

Applications must poll or stream session events until a webhook delivery service is added.

## Hosted and enterprise controls

### Authentication is Organization-level only

Every supported VMA environment uses database-backed API keys that resolve to
exactly one Organization. Keys are hashed at rest, return plaintext only during
create/rotate, can expire or be revoked independently, and use the small `api`,
`api_keys:manage`, and `worker` scope set. A trusted CLI bootstraps the first
management key without a process-global key or anonymous authentication path.

This does not add users, organizations, memberships, invitations, human/service
account identity, roles, SSO, SCIM, support access, or policy evaluation.
Scopes separate ordinary API access, key administration, and worker routes;
they are not resource-level RBAC.

### RBAC is absent

There are no built-in read/run/write/admin grants across agents, environments, vaults, memories, or deployments. A hosted layer must authorize each operation before core query/mutation and prevent privilege escalation through resources such as vault IDs and sandbox environments.

### Public-beta quotas are intentionally narrow

VMA atomically enforces Organization defaults/overrides for requests per minute,
active queued/running work, daily model tokens, and stored File/Skill bytes.
Quota denials return `429`, stable codes, reset metadata, and rate/quota headers.
Active-work reservations are released idempotently on terminal work states, and
provider-reported actual token usage is appended exactly once with
provider/model attribution.

Provider usage is known only after a turn. A turn admitted while below the
daily limit can cross it; VMA records the complete usage and blocks later turns
until the UTC-day reset. This bounded one-turn overrun is deliberate. It is not
an authorization to discard usage or convert it into a monetary charge.

The beta does not enforce concurrent sandbox count, sandbox CPU/memory/disk or
egress, tool/MCP calls, event retention, deployment frequency, or monetary
spend. Postgres RLS is also absent; quota enforcement does not replace database
defense in depth.

### Billing and payments are deferred

The public beta is BYOK-first and does not require a billing product. A trusted
operator may provision an upstream-limited provider key for one Organization.
The append-only usage ledger records raw metric quantities with provider/model,
Session, and funding-source attribution for quota enforcement and analysis. It
is not a priced or monetary source of truth.

Price books, authoritative currency amounts, credit grants, atomic balance
reservation/settlement, top-ups, refunds, Stripe, invoices, plans, seats, taxes,
and spend alerts are explicitly deferred.

### Audit is a beta ledger, not an enterprise archive

VMA appends Organization-attributed authorization decisions, HTTP completion,
quota actions, and runtime governance events with actor, resource, outcome, and
request IDs. ORM guards and database triggers reject updates/deletes to audit
and usage ledger rows.

Coverage is not yet every read/write/run/approval/secret access; an invalid key
cannot be attributed to an Organization. Enterprise export, automated retention,
legal hold, external tamper anchoring, and administrator/support access history
remain absent.

### Compliance is deployment-specific

VMA claims no certification or parity with Anthropic's security/compliance posture. Data residency, backups, deletion verification, incident response, malware handling, vulnerability management, encryption/key custody, subprocess isolation, and third-party provider agreements remain operator responsibilities.

## Closing and documenting gaps

When implementing a gap:

1. Add tests for the internal behavior and the official SDK wire contract where applicable.
2. Add cross-Organization and restart/retry tests.
3. Update the relevant focused document.
4. Change the row in [compatibility matrix](./compatibility-matrix.md) only when the stated production property is true.
5. Keep this ledger entry until the Claude behavior, remaining variance, and operational assumptions are all explicit.

If exact equivalence is impossible—because a provider lacks a capability or an Anthropic system component is private—document the supported replacement and fail clearly rather than silently pretending parity.
