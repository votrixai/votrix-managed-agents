# Known Incompatibilities

Snapshot: 2026-07-11
Runtime kernel: Deep Agents 0.6.12

This is the explicit gap ledger between VMA and [Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview). It covers differences that can be hidden by a compatible route or response shape but materially affect execution, security, durability, or operations.

VMA is an independent implementation. The goal is to make incompatibilities visible and actionable, not to imply that another model provider or a self-hosted process reproduces Anthropic's private harness and managed infrastructure.

Status terms match the [compatibility matrix](./compatibility-matrix.md): **Implemented**, **Partial**, and **Gap**.

## Summary

| Difference | Status | Operational consequence |
| --- | --- | --- |
| Cross-process live preview broker | Gap | Separate workers cannot deliver token/tool deltas to web-process SSE subscribers. |
| Distributed per-session run ownership | Gap | Process-local locks cannot prevent duplicate turns across replicas. |
| E2B remote sandbox | Partial | Optional isolated execution exists, but E2B is hosted unless deployed through BYOC and is not Claude's managed sandbox service. |
| Provider behavioral parity | Inherently partial | Tool calls, streaming, reasoning, multimodal input, usage, and failures vary by model/provider. |
| `deepseek-reasoner` runtime | Unsupported | The current Deep Agents adapter rejects it because the harness requires tool calling. |
| Exact declared subagent roster | Partial | Deep Agents can add a built-in general-purpose subagent when `task` is exposed. |
| Claude durable multiagent threads | Gap | Deep Agents synchronous subagents are ephemeral; background subagents use another protocol. |
| MCP OAuth lifecycle | Gap | Stored/matched access tokens are not automatically refreshed or revoked. |
| Anthropic system skills | Gap | Their private packages and behavior are unavailable to VMA. |
| Claude memory mount/writeback | Gap | E2B receives a one-time bounded memory seed; sandbox edits persist there but are not written back to managed Memory Store versions. |
| Production deployment scheduler | Gap | An idempotent tick exists, but no always-on scheduler service is included. |
| Webhook delivery | Gap | Cryptographic helpers exist, but endpoint management and delivery do not. |
| RBAC, quotas, billing, and audit | Gap | Workspace API keys alone are not a hosted enterprise control plane. |

## API and SDK surface

### Strict SDK tests are not semantic parity

Covered resources are parsed with strict validation by a pinned official Anthropic Python SDK. This establishes useful field and union compatibility for tested calls. It does not verify:

- Every request field, filter, pagination edge, error type, or future SDK release.
- Equivalent session timing, background execution, tool behavior, or model output.
- Anthropic infrastructure guarantees, data handling, retention, or rate limits.
- Compatibility with every language SDK.

The route-by-route inventory is [Managed Agents API coverage](./managed-agents-api-coverage.md).

### Session overrides are incomplete

Claude supports create-time `agent_with_overrides` for model, system, tools, MCP servers, and skills. VMA's current session-create request accepts an agent ID or pinned agent reference. Selected session-local agent updates exist, but create-time and runtime override semantics are not complete or uniform.

Consequence: a request accepted by the newest official SDK may fail validation or may not affect graph compilation exactly as it would on Claude.

### Event union and steering are incomplete

VMA persists an append-only event log and implements common user, agent, tool, status, outcome, and resource shapes. The complete Claude event union, all span nesting, processed-time transitions, mid-run steering, interrupt redirection, and every validation rule are not present.

Consequence: clients should tolerate unknown/missing optional events and must not infer Claude-identical span traces from VMA events.

### Beta headers can drift

VMA recognizes the Managed Agents, skills, user-profile, and native VMA beta headers used by its contract suite. Claude can introduce feature-specific beta headers and change beta behavior independently. Header acceptance in VMA must be updated deliberately rather than treated as automatic upstream support.

## Live streaming and process topology

### Preview delivery is process-local

Durable session events are stored in the database and replayed by sequence number. Token/tool previews use `VmaProcessLocalPreviewBus`, which only reaches subscribers inside the same Python process.

If the graph runs in a worker while SSE is served by another process:

- `event_start` and `event_delta` preview frames are not delivered live.
- The client eventually receives durable events after they are persisted and polled.
- Dropped preview frames cannot be replayed because previews are intentionally ephemeral.

Closing the gap requires a tenant-scoped cross-process broker such as Redis Streams or NATS, with:

- Topics scoped by workspace and session.
- Per-run ordering and sequence metadata.
- Backpressure and bounded subscriber queues.
- Worker/web authentication.
- Reconciliation against the durable database event sequence.
- Cleanup and retention appropriate for transient frames.

### Run serialization is process-local

The current in-process `_running_sessions` lock prevents duplicate execution only inside one worker process. A database work lease improves visibility but is not by itself a distributed session mutex with fencing.

Two replicas can otherwise start work for one session, race checkpoint writes, duplicate provider cost, and emit conflicting events. Production needs a database advisory lock, Redis/etcd lease with fencing token, or equivalent ownership tied to the work item and checkpoint attempt.

### Cancellation is not end-to-end

Changing a public Session state or cancelling an async task does not prove that an in-flight MCP request or remote command has stopped. The current E2B path has no sandbox-operation lease, heartbeat, fencing token, or durable cancellation outbox. Turn exit attempts to pause the sandbox and Session deletion attempts to kill it, but production still needs cooperative command cancellation, hard termination, and distributed run ownership.

## Sandbox and environment behavior

### The optional E2B provider is not Claude's sandbox service

Claude cloud environments include Anthropic-managed isolated execution infrastructure and lifecycle behavior. VMA has three materially different choices:

- Default: Deep Agents `StateBackend`, with checkpointed files and no shell.
- Optional `sandbox-e2b` extra: `langchain-e2b==0.0.5` and `e2b==2.31.0`, selected with `VMA_SANDBOX_PROVIDER=e2b`, a server-owned `E2B_API_KEY`, and a required operator-owned `VMA_E2B_TEMPLATE`.
- Custom integration: `VMA_SANDBOX_FACTORY` returning an operator-provided backend.

The E2B path provisions exactly one isolated sandbox while creating a Session. It uploads the fixed initial bundle once, seals the immutable inputs, pauses with full-memory preservation, and later reconnects the same opaque external ID. Before every turn it recomputes the input identity and verifies the stored seal; it never re-uploads, repairs, or synchronizes files on resume. Deep Agents receives an `AsyncE2BSandbox` backend and invokes command and file operations through tool calls.

Provider auto-resume is disabled so every reconnect passes through VMA authorization. Secure access is enabled and public traffic is disabled. Archive preserves the sandbox, deletion kills it, and best-effort retention cleanup defaults to 30 days. E2B remains an external dependency unless the operator deploys [E2B BYOC](https://e2b.dev/docs/byoc), and VMA makes no guarantee that its performance, failure behavior, isolation implementation, or lifecycle is behaviorally equal to [Claude Managed Agents sessions](https://platform.claude.com/docs/en/managed-agents/sessions). See [sandbox runtime](./sandbox-runtime.md).

The hardened template is the trusted computing base for guest filesystem confinement. VMA verifies only that the template's default execution user matches the configured non-root guest and that passwordless sudo is unavailable; it does not scan or harden the whole Linux image. E2B may expose its own sandbox/team/template identifiers inside the guest runtime even though VMA omits the external sandbox ID from control-plane API responses.

### Sandbox lifecycle has no recovery protocol

VMA records one external E2B sandbox ID as opaque private database state; it is not a public resource identifier or an authorization boundary. Database transactions and provider side effects cannot be atomic. A failed Session create attempts to kill a sandbox it already created, but VMA has no provisioning generations, snapshots, operation leases, heartbeat/fencing, durable outbox, or automatic orphan discovery/recovery. A process crash can therefore leave provider and database state inconsistent, and operators must inspect failed creates and E2B inventory.

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

The environment work API and `vma-worker` lease and execute work. They do not create a container or VM, verify a worker image, attest policy, or isolate tenants. The worker must still use an approved sandbox provider. Worker identity, RBAC, fencing, and sandbox attestation remain gaps.

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

### Approval is partial

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

Claude multiagent sessions instead expose durable thread resources and event streams while sharing session infrastructure. VMA thread response objects therefore remain partial even when their shapes validate.

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

### Static connection support is partial

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

## Files, skills, and memory

### Public mount paths are portable; enforcement depends on the provider

R2 or another S3-compatible object store supplies uploaded files and custom Skill archives when a Session is created. VMA materializes the fixed bundle into the one E2B sandbox exactly once. Read-only uploads default below `/mnt/session/uploads`, Skills live below `/skills/custom`, and memory seeds live below their `/mnt/memory` mount. Immutable inputs cannot overlap `/workspace` or a read-write memory root. Once the seal exists, Session-resource mutations are rejected and the control plane never performs per-turn upload, repair, deletion, or managed-file synchronization.

The built-in E2B adapter rejects traversal, symlinks, aliases, and path collisions, then makes control-plane-owned immutable files root-owned and verifies their digest before each turn. This protects those files only. The required operator-owned hardened template must confine all other guest writes to `/workspace` and approved read-write memory roots; VMA checks the non-root/no-passwordless-sudo prerequisites but does not implement general Linux hardening.

E2B Session creation currently rejects `github_repository` resources because VMA does not yet implement secure one-time checkout and credential handling. The response union remains available for compatibility on non-E2B control-plane resources, but it must not be interpreted as a completed E2B mount.

Generated sandbox artifacts are not discovered or exported automatically. A file created only inside E2B can disappear when retention cleanup or Session deletion kills the sandbox. Production malware quarantine and content-disarm policy are also absent.

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

- The same workspace-scoped internal thread ID.
- The same pinned agent revision.
- The same E2B external ID, fixed create-time input identity, and immutable-file seal.
- A compatible graph and middleware topology.
- Decisions ordered against the saved interrupt action requests.

Deploying a changed graph and resuming an old checkpoint can fail or behave differently. Production needs revision-aware workers and a checkpoint migration/retention policy.

### Checkpoint isolation is application-enforced

LangGraph checkpointers primarily key state by thread configuration. VMA must never authorize checkpoint access from a caller-supplied ID alone. Public session lookup must be workspace-scoped and map to a server-owned opaque thread ID. Stronger deployments should add database RLS, per-tenant schemas, or a checkpointer wrapper that incorporates tenant context.

### Compaction differs

Deep Agents uses model-aware summarization and compact message state. Its token thresholds, retained messages, media handling, offload paths, and summary prompt differ from Claude's managed compaction. Summaries can change behavior and may contain sensitive content that must follow the same retention policy as the conversation.

## Deployments and webhooks

### Deployment scheduling is not operated automatically

VMA persists deployment resources, validates cron/timezone data, computes upcoming timestamps, creates manual runs, and provides an idempotent due-schedule function. No always-on scheduler invokes that function in the open core.

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

### Authentication is workspace-level only

The open core supports configured or database-backed API keys that resolve to a workspace. It does not include users, organizations, memberships, invitations, service accounts, roles, SSO, SCIM, support access, or policy evaluation.

Every key mapped to a workspace effectively has broad core API authority unless an injected hosted layer adds finer policy.

### RBAC is absent

There are no built-in read/run/write/admin grants across agents, environments, vaults, memories, or deployments. A hosted layer must authorize each operation before core query/mutation and prevent privilege escalation through resources such as vault IDs and sandbox environments.

### Quotas and rate limits are absent

VMA records some usage and validates selected resource-count/size limits, but it does not enforce tenant budgets for:

- Concurrent sessions or sandboxes.
- Model tokens, requests, or cost.
- Tool/MCP calls.
- Sandbox CPU, memory, disk, or egress.
- File/object storage.
- Event retention.
- Deployment frequency.

Provider rate-limit errors may trigger retries, but that is not quota policy.

### Billing is absent

There is no usage ledger with authoritative pricing, credits, invoices, plans, seats, taxes, refunds, or spend alerts. Session usage fields are diagnostic data, not a billing source of truth.

### Audit is absent

Structured application logs and resource timestamps are not an immutable audit trail. VMA lacks actor-attributed records for every read/write/run/approval/secret access, retention controls, export, tamper evidence, and administrator/support access history.

### Compliance is deployment-specific

VMA claims no certification or parity with Anthropic's security/compliance posture. Data residency, backups, deletion verification, incident response, malware handling, vulnerability management, encryption/key custody, subprocess isolation, and third-party provider agreements remain operator responsibilities.

## Closing and documenting gaps

When implementing a gap:

1. Add tests for the internal behavior and the official SDK wire contract where applicable.
2. Add cross-workspace and restart/retry tests.
3. Update the relevant focused document.
4. Change the row in [compatibility matrix](./compatibility-matrix.md) only when the stated production property is true.
5. Keep this ledger entry until the Claude behavior, remaining variance, and operational assumptions are all explicit.

If exact equivalence is impossible—because a provider lacks a capability or an Anthropic system component is private—document the supported replacement and fail clearly rather than silently pretending parity.
