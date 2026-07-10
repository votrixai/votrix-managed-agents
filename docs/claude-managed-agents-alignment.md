# Claude Managed Agents Alignment

Last checked: 2026-07-10
Runtime kernel: Deep Agents 0.6.12

This document defines how VMA maps the public Claude Managed Agents product model onto a self-hosted control plane and a Deep Agents execution plane. Official Claude documentation is the source of truth for the compatibility target. The VMA implementation and [compatibility matrix](./compatibility-matrix.md) are the source of truth for what works here today.

VMA is an independent implementation. A matching JSON response does not imply matching model behavior, infrastructure, security, or service-level guarantees.

## Official source policy

Check the relevant official page before changing a public contract:

- Product concepts: https://platform.claude.com/docs/en/managed-agents/overview
- Agent definitions and versioning: https://platform.claude.com/docs/en/managed-agents/agent-setup
- Tools: https://platform.claude.com/docs/en/managed-agents/tools
- MCP connector: https://platform.claude.com/docs/en/managed-agents/mcp-connector
- Permission policies: https://platform.claude.com/docs/en/managed-agents/permission-policies
- Agent skills: https://platform.claude.com/docs/en/managed-agents/skills
- Cloud environments: https://platform.claude.com/docs/en/managed-agents/environments
- Cloud sandbox reference: https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference
- Self-hosted sandboxes: https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes
- Sessions: https://platform.claude.com/docs/en/managed-agents/sessions
- Session operations: https://platform.claude.com/docs/en/managed-agents/session-operations
- Events and streaming: https://platform.claude.com/docs/en/managed-agents/events-and-streaming
- Session files: https://platform.claude.com/docs/en/managed-agents/files
- Vaults: https://platform.claude.com/docs/en/managed-agents/vaults
- Memory stores: https://platform.claude.com/docs/en/managed-agents/memory
- Multiagent sessions: https://platform.claude.com/docs/en/managed-agents/multi-agent
- Outcomes: https://platform.claude.com/docs/en/managed-agents/define-outcomes
- Scheduled deployments: https://platform.claude.com/docs/en/managed-agents/scheduled-deployments
- Webhooks: https://platform.claude.com/docs/en/managed-agents/webhooks
- Reference catalog: https://platform.claude.com/docs/en/managed-agents/reference

The official SDK is also used as an executable wire contract in `tests/contract/test_anthropic_sdk_contract.py`. Those tests are deliberately narrower than the service semantics described in the official guides.

## Product model

Claude Managed Agents combines a versioned control plane with managed execution infrastructure. VMA preserves that separation:

| Public concept | VMA control-plane representation | Deep Agents/runtime representation | Status |
| --- | --- | --- | --- |
| Agent | Mutable `agents` pointer plus immutable `agent_versions` snapshot | A graph compiled from the pinned snapshot | Implemented control plane; partial execution |
| Environment | Workspace-scoped environment config | A backend selected from `StateBackend`, explicit unsafe local shell, or `VMA_SANDBOX_FACTORY` | Implemented config; remote enforcement gap |
| Session | Pinned agent version, environment, resources, state, event sequence, checkpoint thread ID | One LangGraph thread plus run context | Partial |
| Events | Append-only database records and SSE | LangGraph message/update chunks translated to public events | Partial |
| Tool policy | Versioned toolset configuration and session continuation events | Deep Agents tool filtering plus LangGraph human-in-the-loop interrupts | Partial |
| Files | File and session-resource metadata plus S3-compatible bytes | Files written into the selected backend for a run | Partial |
| Skills | Versioned archives referenced by agent versions | Extracted Agent Skills directories consumed by `SkillsMiddleware` | Partial |
| Memory | Versioned path records mounted as session resources | A bounded filesystem snapshot plus an `AGENTS.md` memory source | Partial |
| Multiagent roster | Pinned agent/version references and thread resources | Declarative synchronous `SubAgent` entries reached through `task` | Partial |
| Deployment | Schedule, initial events, resources, vaults, and run records | Session creation through an externally invoked scheduler tick | Partial |

See [known incompatibilities](./known-incompatibilities.md) for the exact places where these mappings are not equivalent.

## Layer ownership

### Public compatibility layer

The FastAPI layer owns:

- `/v1` paths, beta/version headers, request validation, response models, errors, and pagination.
- Workspace authentication and lookup.
- Public resource IDs and relationships.
- Optimistic agent versioning and immutable snapshots.
- Session state and append-only public event history.
- Translation between public tool/continuation events and runtime interrupts.

It must not expose LangGraph checkpoint IDs, provider credentials, MCP authorization headers, or sandbox-native identifiers directly.

### Durable control plane

The database and object-storage layer owns:

- Agents and revisions, environments, sessions, session resources, and session events.
- Durable work records, leases, retry metadata, and deployment run records.
- Files, custom skill archives, vault/credential metadata, and memory versions.
- The mapping from a public session to an opaque internal checkpoint thread ID.

The VMA event log remains the public source of truth. A LangGraph checkpoint is runtime state and cannot replace public event history, processed timestamps, resource state, or audit records.

### Deep Agents execution layer

Deep Agents owns the in-process graph loop:

- Model/tool invocation through LangChain chat models.
- Todo planning, filesystem tools, compaction/summarization, and dangling-tool-call repair.
- LangGraph checkpoints and human-in-the-loop interrupts.
- Agent Skills loading.
- Synchronous subagent delegation.

Deep Agents does not provide tenant authentication, API resources, durable work ownership, a remote sandbox fleet, billing, quotas, audit, or Claude-compatible events. Those remain VMA responsibilities.

## Agent compilation contract

Every session resolves an immutable effective agent snapshot before compilation. That snapshot includes the pinned base version and any supported session-local replacements.

VMA compilation follows these rules:

1. Resolve the provider and model using server-owned configuration and credentials from approved settings or mounted vaults.
2. Reject features that the provider capability record cannot support. In particular, `deepseek-reasoner` is rejected because the Deep Agents harness requires tool calling.
3. Map public built-in tool names to Deep Agents tool names and hide disabled tools at model-call time.
4. Map `always_ask` policies and client-owned custom tools to LangGraph interrupts.
5. Connect declared remote MCP servers using matched session-vault credentials when available.
6. Materialize custom skills and session files into run-scoped paths.
7. Map pinned coordinator roster entries to declarative Deep Agents subagents.
8. Compile with an opaque session thread ID and the configured durable checkpointer.
9. Apply VMA graph-step and wall-clock limits independently of Deep Agents' permissive defaults.

Provider profiles and harness profiles in Deep Agents are process-global. VMA must not mutate them with tenant-specific values. Tenant credentials belong in preconstructed model clients or a tenant-aware gateway; sandbox selection belongs in a run-scoped backend or tenant-aware router.

## Session execution contract

A normal turn is expected to move through this sequence:

1. Authenticate the caller and resolve the workspace.
2. Lock or claim the session work item.
3. Load the pinned agent revision, environment, event history, resources, vault credentials, memory context, and subagent roster.
4. Open the run-scoped backend and durable checkpointer.
5. Compile the Deep Agents graph.
6. Invoke with the new user events, or resume the exact saved interrupt on the same internal thread.
7. Translate graph messages, tool calls, tool results, updates, and interrupts into canonical VMA runtime events.
8. Persist durable events before advancing the public session state.
9. Finish in `idle`, `rescheduling`, or `terminated`, or pause in `idle` with `requires_action`.

The same agent revision and compatible graph topology must be used when resuming a checkpoint. Updating an agent must not mutate already pinned sessions.

The current implementation still uses process-local session-run serialization in places. A production multi-worker deployment needs a database advisory lock, Redis lock with fencing, or equivalent distributed ownership in addition to queue leases.

## Streaming alignment

Claude exposes persisted session events and live SSE. VMA has two related channels:

- Durable events stored with a monotonic sequence number and replayed from the database.
- Best-effort preview frames for token/tool deltas while a graph is running.

Deep Agents streams LangGraph tuples containing a subgraph namespace, stream mode, and data. VMA translates parent text, tool-call fragments, tool results, state updates, and interrupts instead of exposing those LangChain objects publicly.

Today the preview bus is process-local. If execution runs in a worker process and SSE runs in a web process, clients receive durable database events but not true live deltas. Production needs a tenant-scoped broker with ordering, bounded backpressure, and reconnection behavior. See [known incompatibilities](./known-incompatibilities.md#live-streaming-and-process-topology).

## Tool and approval alignment

Public tool configuration is versioned with the agent. Runtime mapping is intentionally explicit:

| Public tool | Deep Agents/runtime tool |
| --- | --- |
| `bash` | `execute`, only with an execution-capable sandbox |
| `read` | `ls`, `read_file` |
| `write` | `write_file` |
| `edit` | `edit_file` |
| `glob` | `glob` |
| `grep` | `grep` |
| `web_fetch` | VMA bounded HTTP fetch tool |
| `web_search` | VMA operator-configured search tool |

`always_ask` can pause a graph, but approval middleware is not access control. The sandbox and remote tool gateway must independently enforce tenant policy. Custom tools remain application-owned: VMA must persist the request, stop the session, and resume only with a validated matching result.

## Files, skills, memory, and vaults

- Files and custom skill bytes live behind S3-compatible storage; relational rows hold metadata and immutable references.
- Session file copies are materialized for the runtime, but the sandbox provider must enforce mount paths and read-only behavior.
- Custom skill archives use Agent Skills `SKILL.md` conventions understood by Deep Agents. Anthropic system-skill references are compatibility metadata only because their private contents are unavailable.
- Memory stores remain versioned VMA resources. The current runtime seeds bounded records under each mount path and supplies an `AGENTS.md` memory source; edits in the graph backend are not synchronized back into VMA memory versions.
- Vault responses redact secrets. Runtime credentials are resolved server-side and never placed in public run state. Complete OAuth refresh and enterprise secret-provider policy are still gaps.

## Multiagent alignment

VMA correctly pins roster versions, but the execution models differ:

- Claude multiagent sessions expose durable agent threads with isolated event streams and shared session infrastructure.
- Deep Agents synchronous `SubAgent` calls are ephemeral, receive a task message, and return one result to the coordinator.
- Deep Agents background `AsyncSubAgent` uses LangGraph Agent Protocol rather than the VMA session-thread API.

VMA therefore treats multiagent execution and thread streaming as partial even when thread response objects pass SDK validation.

Deep Agents also adds a general-purpose subagent by default when `task` is enabled. Exact declared-roster behavior requires an adapter-level exclusion or a process-stable harness profile; per-tenant profile mutation is unsafe.

## Open-core and hosted boundary

The open core stops at workspace-scoped resources and injectable providers. A hosted layer may import:

```python
from votrix_managed_agents import create_app

app = create_app(auth_provider=HostedAuthProvider())
```

The hosted layer owns organizations, membership, RBAC, SSO, billing, quotas, usage accounting, audit policy, managed secrets, sandbox fleets, and support/admin functions. Core tables must remain usable without hosted tables. See [open-core architecture](./open-core-architecture.md).

## Compatibility priorities

Work should close gaps in this order:

1. Preserve strict official-SDK contract tests for every changed public route.
2. Add distributed session ownership and a cross-process preview broker.
3. Ship or integrate a production remote sandbox with enforceable environment policy.
4. Make interrupt persistence and custom-tool continuation restart-safe.
5. Complete MCP connection lifecycle, OAuth refresh, and approval mapping.
6. Complete file mounts, custom-skill lifecycle, and bidirectional memory tools.
7. Decide and implement exact roster-only subagent behavior and durable multiagent threads.
8. Operate scheduled deployments with a durable scheduler and workers.
9. Add webhook registration/delivery and hosted RBAC, quotas, billing, and audit.

Do not mark a row implemented merely because the public response validates. The [compatibility matrix](./compatibility-matrix.md) and [known incompatibilities](./known-incompatibilities.md) must change with the implementation.
