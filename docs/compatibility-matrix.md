---
title: Compatibility Matrix
description: Current implementation status and material gaps across the VMA public surface.
---

Snapshot: 2026-07-11
Runtime kernel: Deep Agents 0.6.12

VMA targets the public resource and SDK shape described by the [Claude Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview). It does not claim identical model behavior or managed-infrastructure semantics.

Status meanings:

- **Implemented**: code exists in this repository and the covered behavior has direct tests.
- **Partial**: a route or runtime path exists, but important behavior, durability, security, or exact Claude semantics remain incomplete.
- **Gap**: no production implementation is present.

Passing the official Anthropic Python SDK's strict response validation proves wire compatibility only for the tested requests. It does not promote a partial runtime feature to implemented. See [API route coverage](./managed-agents-api-coverage.md) and [known incompatibilities](./known-incompatibilities.md).

## Public control plane

| Area | Status | Current VMA behavior | Material difference or remaining work |
| --- | --- | --- | --- |
| `/v1` resource paths | Implemented | Public paths cover the major agent, environment, session, event, file, skill, vault, memory, deployment, and user-profile families. | The route inventory is not a guarantee that every request field and error code matches every future SDK release. |
| Beta and version headers | Implemented | Accepts the native VMA beta header and Claude-compatible `anthropic-beta: managed-agents-2026-04-01`; compatibility requests require `anthropic-version`. | Memory and future preview headers may evolve independently upstream. |
| Workspace isolation | Implemented | API keys resolve to `CurrentWorkspace`; core queries and object keys are workspace-scoped. | Organization membership, role inheritance, SSO, and policy evaluation are outside the Votrix core. |
| Agent create/retrieve/list/archive | Implemented | Agent resources are mutable pointers to immutable version snapshots. Archived agents cannot start new sessions. | Exact upstream validation can change during beta. |
| Agent update/version guard | Implemented | Updates require the active version, reject stale writes, preserve omitted fields, replace arrays, merge metadata, and avoid no-op versions. | None known for the covered fields; see [agent versioning](./agent-versioning.md). |
| Tool and MCP declaration validation | Implemented | Validates aggregate limits, custom tool names/schemas, MCP server count, unique names, and `mcp_toolset` references. | Validation does not establish a live MCP connection. |
| Multiagent roster pinning | Implemented | Unversioned roster references resolve to immutable referenced versions; roster depth is limited. | Execution and event-thread behavior are partial. |
| Environment CRUD | Implemented | Stores and validates cloud, self-hosted, and local environment configuration; the optional E2B adapter maps supported egress and lifecycle policy. | E2B resources and preinstalled packages are template-level, package state is not shared across sessions, and custom providers must enforce their own policy. |
| Session CRUD and version pinning | Implemented | Session creation pins an agent version; covered create/retrieve/list/update/archive/delete responses pass strict SDK tests. | Claude create-time `agent_with_overrides` semantics are not complete. |
| Session state machine | Partial | Persists `idle`, `running`, `rescheduling`, and `terminated`, including retry metadata and `requires_action` as an idle stop reason. | Distributed serialization, complete steering semantics, durable cancellation, and all failure transitions remain incomplete. |
| Session sandbox lifecycle | Partial | With E2B selected, Session creation provisions, seeds, seals, and pauses exactly one sandbox. Every turn reconnects its private external ID, verifies the fixed create-time input identity and immutable-file seal, never re-uploads files, and pauses with full-memory preservation; archive preserves it, delete kills it, and best-effort janitor cleanup defaults to 30 days. | There are no sandbox generations, snapshots, operation leases/heartbeats, durable outbox, or orphan recovery. Database/provider effects are not atomic, scale-to-zero delays cleanup, and the lifecycle is not Anthropic's managed service. |
| Session-local agent changes | Partial | Idle-session updates can replace selected agent configuration without creating a new agent version. | Full override semantics are incomplete. For an E2B Session, any change to create-time Skills, inputs, or initial memory sources makes resume fail and requires a new Session. |
| Event append/list | Implemented | Append-only events have monotonic per-session `seq`; covered input validation and pagination pass strict SDK tests. | The complete Claude event union and every predecessor/processing transition are not implemented. |
| SSE replay and live stream | Partial | Durable events replay from the database; same-process runtimes can publish transient `event_start` and `event_delta` previews. | Preview delivery is process-local, so a separate worker cannot stream live deltas to the web process without a broker. |
| File API | Partial | Upload/list/retrieve/download/delete, workspace deduplication, upload limits, local EICAR checks, and S3-compatible source-of-truth storage exist. | Production malware scanning, retention, generated-file discovery/export, and complete remote-sandbox mount lifecycle remain incomplete. |
| Session resources | Partial | File, GitHub repository, and Memory Store response unions are persisted. E2B resolves supported File and Memory Store inputs at Session creation, uploads them once, seals immutable content, and blocks resource mutation afterward; E2B File inputs must be read-only below `/mnt/session`, and immutable inputs cannot overlap mutable workspace or read-write memory roots. | E2B Session creation rejects `github_repository` because secure one-time checkout/token handling is not implemented. Resume never repairs or re-syncs files, and guest write confinement depends on the required hardened E2B template. |
| Skills API | Partial | Validates and versions custom skill archives containing one top-level directory and `SKILL.md`. | Runtime uses Deep Agents skill semantics; Anthropic-provided system skills are not supplied locally. |
| Vault and credential API | Partial | Credential unions, redaction, AES-GCM secret persistence with production fail-closed configuration, workspace scoping, and MCP URL matching exist. | No complete OAuth enrollment, refresh, revocation, health probe, KMS policy, or enterprise secret-manager implementation. |
| Memory-store API | Partial | Path memories, limits, optimistic preconditions, immutable versions, deletion history, and filtering are implemented; E2B can receive a bounded one-time seed at Session creation. | Read-write sandbox edits persist only in that sandbox and are not written back to managed Memory Store versions or exposed as Claude-compatible bidirectional memory. |
| Multiagent session threads | Partial | Thread resources and pinned roster snapshots can be created and queried. | Deep Agents synchronous delegation is ephemeral and does not reproduce Claude's durable per-agent thread streams. |
| Outcomes | Partial | Outcome request shape, stored metadata, and compatible evaluation-end event shape exist. | No independent production grader loop with Claude-equivalent iteration semantics. |
| Deployments and runs | Partial | CRUD, pause/unpause, manual runs, cron validation, upcoming timestamps, session linkage, and an idempotent due-schedule function exist. | No continuously operated scheduler, distributed fencing, retry service, or delivery SLO. |
| Webhooks | Gap | Signing, verification, timestamp freshness, and envelope helpers exist for tests and integrations. | Endpoint registration, event fan-out, retries, idempotency, rotation, and failure disabling are absent. |
| User profiles | Partial | Resource lifecycle and enrollment-token response shapes exist. | Identity binding, verification, grants, and hosted access policy are absent. |

## Deep Agents execution plane

| Area | Status | Current VMA behavior | Material difference or remaining work |
| --- | --- | --- | --- |
| Runtime kernel | Implemented | Session turns always compile an immutable VMA agent snapshot into a run-scoped Deep Agents graph, stream it, and persist translated output. Real-graph tests cover execution and checkpoint resume; control-plane tests inject a deterministic executor at the internal adapter boundary. | Deep Agents is a beta library and is not itself a durable multi-tenant service. Exact Claude behavior remains partial. |
| Model-provider resolution | Implemented | Server-owned configuration constructs `ChatAnthropic`, `ChatOpenAI`, or `ChatDeepSeek` without mutating process environment; additional approved providers use `VMA_MODEL_PROVIDERS`. | Provider capabilities remain heterogeneous. See [model providers](./openai-compatible-providers.md). |
| DeepSeek | Partial | `deepseek-chat` is available through the native LangChain integration. | `deepseek-reasoner` is rejected because the Deep Agents harness requires tool calling; reasoning/event formats also differ from Claude. |
| Checkpoint backend selection | Implemented | LangGraph checkpointers select Postgres in production-style configurations, a separate SQLite database locally, or explicit in-memory mode. | Checkpoints do not replace the VMA event log. Tenant safety still depends on server-owned opaque thread IDs. |
| Checkpoint/interrupt resume | Partial | Pending custom-tool results and approve/reject decisions are grouped by saved interrupt ID and resumed on the same thread; real-graph tests cover cross-turn resume. | Resume across changed deployments requires the original agent revision and compatible graph shape; edit decisions and checkpoint migration remain incomplete. |
| Graph stream translation | Implemented | Parent text, fragmented tool calls, tool results, usage, and HITL interrupts are translated into durable VMA events; previews and final events share exact event IDs. | Full Claude event/span union and child-agent thread routing remain partial. |
| Conversation compaction | Partial | Deep Agents supplies model-aware summarization and compact checkpoint message state. | Trigger thresholds and summary wording are not Claude's managed compaction implementation. |
| Built-in filesystem tools | Partial | Claude names map to Deep Agents `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, and optional `execute`; disabled tools are filtered from model requests. | Tool schemas, result text, path behavior, and error payloads are not byte-for-byte Claude equivalents. |
| Web tools | Partial | `web_fetch` enforces public HTTP(S), redirect revalidation, and response limits; `web_search` can call an operator-provided JSON endpoint. | Search ranking, citations, availability, and policy do not match Anthropic's hosted tools. |
| Permission policies | Partial | `always_ask` tool configs map to LangGraph human-in-the-loop interrupts for supported tools. | Tool middleware is not a security boundary; shell/file policy must be enforced by the sandbox. Edit/respond decision parity and all MCP approval cases need continued contract work. |
| Custom tools | Partial | Custom schemas become client-owned Deep Agents tools, pause with a respond-only interrupt, persist their public event IDs, and resume from matching application results. | Idempotency and exact Claude behavior are not complete for every concurrent, cancellation, or retry path. |
| MCP runtime | Partial | Runtime matches vault credentials by normalized MCP URL and can construct remote MCP tools with static authorization headers. Missing credentials emit non-terminal session errors. | MCP currently runs in the control plane, so `allow_mcp_servers` does not map to E2B egress; OAuth refresh, discovery, revocation, reconnect policy, capability drift, and complete approval events are also missing. |
| Custom skills at runtime | Partial | Versioned archives remain in S3-compatible storage, are selected and traversal/symlink checked, then are materialized once below `/skills/custom` when an E2B Session is created. The sealed Skill identity is verified on every turn. | Skills cannot change within an E2B Session; a change requires a new Session. Deep Agents does not guarantee Claude's prompt, cache behavior, or security policy. |
| Anthropic system skills | Gap | References can be accepted as compatibility data. | VMA does not have Anthropic's private skill packages and skips them during local runtime materialization. |
| Memory at runtime | Partial | A bounded Memory Store seed is materialized once at each mount with an `AGENTS.md` source. E2B seals read-only memory; read-write memory remains mutable and persists in the same sandbox across turns. | Runtime edits are not written back to managed Memory Store versions; custom execute-capable backends must enforce access themselves, and no semantic index exists. |
| Synchronous subagents | Partial | Pinned VMA roster entries map to Deep Agents declarative `SubAgent` specifications and the `task` tool. | Calls are ephemeral and return one result to the coordinator; they do not create equivalent durable Claude session threads. |
| General-purpose subagent | Partial | Deep Agents may add its built-in general-purpose subagent whenever `task` is exposed. | Harness profiles are process-global, so exact per-agent roster exclusion needs an adapter change or carefully fixed startup profile. |
| Background subagents | Gap | Deep Agents offers `AsyncSubAgent` as an upstream capability. | It speaks LangGraph Agent Protocol, not the VMA/Claude session-thread API, and is not exposed as compatible background orchestration. |
| Safe default backend | Implemented | Without a selected remote provider or factory, `StateBackend` supplies checkpointed file state and does not expose shell execution. | It is not a cloud sandbox and does not enforce environment network/package policy. |
| E2B remote sandbox | Partial | The `sandbox-e2b` extra pins `langchain-e2b==0.0.5` and `e2b==2.31.0`; `VMA_SANDBOX_PROVIDER=e2b` gives Deep Agents the one Session-bound `AsyncE2BSandbox`. A named operator-owned hardened template is mandatory; VMA checks its default guest is non-root, cannot use passwordless sudo, and cannot modify the trusted system interpreter roots used for isolated root bootstrap. Secure access is enabled, public traffic and provider auto-resume are disabled, full-memory pause preserves `/workspace` and read-write memory, and limited egress uses E2B `allow_out`. | No lifecycle leases/outbox/orphan recovery or managed-file sync exists. E2B is hosted unless deployed with BYOC; limited mode adds no separate deny-all rule, resources/packages are template-level and trusted from the operator-declared profile rather than independently attested, generated artifacts and memory edits are not exported, and E2B supplies additional untrusted Session-local writable paths outside the durable workspace/memory contract. See [sandbox runtime](./sandbox-runtime.md). |
| Custom remote sandbox | Partial | `VMA_SANDBOX_FACTORY` remains the provider integration boundary. | The operator is responsible for isolation, policy enforcement, lifecycle, retention, and compatibility. |
| Unsafe local shell | Implemented | Explicit local-only mode uses `LocalShellBackend` with a workspace/session-derived root and minimal environment inheritance. | It executes in the control-plane host and must never be used for untrusted tenants. |
| Run timeout and graph steps | Partial | Settings cap graph recursion and wall-clock runtime. | Complete token, tool-call, cost, subprocess, network, and provider-request budgets are not enforced end to end. |
| Cancellation and retries | Partial | Session retry metadata, work retries, and Deep Agents dangling-tool-call repair cover some paths. | Provider calls, MCP requests, and remote commands need cooperative cancellation; process-local run locks are insufficient in a multi-worker deployment. |

## Operations and enterprise controls

| Area | Status | Current VMA behavior | Material difference or remaining work |
| --- | --- | --- | --- |
| Relational persistence | Implemented | SQLAlchemy and Alembic support SQLite locally and Postgres for deployment. | Production backup, RLS, restore testing, and data lifecycle are operator responsibilities. |
| Object persistence | Partial | S3-compatible storage supplies uploaded files and Skill archives for the one-time Session bootstrap; the sealed sandbox copy is then independent. | There is no managed-file sync or generated-artifact export; sandbox edits, including read-write memory changes, remain only in E2B. Retention, malware quarantine, regional replication, and customer-managed keys are not productized. |
| Durable work records | Partial | Postgres work items support queueing, leasing, heartbeat, retry gates, stop, and an optional worker. | Strong fencing, distributed run ownership, dead-letter handling, and managed queue integration remain incomplete. |
| Live-preview broker | Gap | Same-process fan-out is available. | Multi-process and multi-region delivery require a tenant-scoped broker with ordering and backpressure. |
| Scheduler | Partial | An importable, idempotent due-schedule tick exists. | No always-on scheduler service or production retry SLO is included. |
| Authentication | Partial | Environment and database API-key providers resolve a workspace. | Hosted user sessions, service accounts, OAuth/OIDC, and key lifecycle policy need a private layer. |
| RBAC and SSO | Gap | Not part of the workspace-scoped Votrix core. | Required for a managed enterprise service. |
| Quotas, billing, and usage metering | Gap | Some model usage can be stored on sessions. | No authoritative quota enforcement, cost ledger, invoicing, or plan system. |
| Audit | Gap | Application logs and resource timestamps exist. | No immutable tenant audit trail, export, retention policy, or administrator access log. |
| Compliance | Gap | No compliance certification is claimed. | Isolation, retention, deletion verification, incident response, and regional controls require deployment-specific work. |

## Compatibility rule

When a public response can match Claude but the execution semantics cannot, VMA should preserve the compatible wire shape, expose the limitation in session events or operator diagnostics, and keep the row **Partial**. It must not silently claim managed behavior that the configured model, broker, secret provider, or sandbox cannot provide.
