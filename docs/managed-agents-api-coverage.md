---
title: Managed Agents API Coverage
description: Route-by-route coverage of the Managed Agents-compatible HTTP API.
---

Source basis:

- Official docs navigation and guides under `https://platform.claude.com/docs/en/managed-agents`
- Official `anthropic-sdk-python` `api.md` on `main`, checked on 2026-06-19
- Semantic alignment map: [Claude Managed Agents Alignment](./claude-managed-agents-alignment.md)

This file is route coverage only. It is not a claim of production semantic parity. Use the alignment map and `TODO.md` for state machine, runtime, sandbox, tool, vault, webhook, and deployment semantics.

When `VMA_PUBLIC_GA_ONLY=true`, only API keys, Agents, Environments without
worker operations, Sessions without Threads, Files without presign/complete,
Skills, Vaults/native model Credentials, Model Providers, health, and
capabilities appear in OpenAPI. Other rows below describe repository
compatibility work, not public-beta product promises.

Every row answers three separate questions:

- **Public Beta**: `Yes` means the route is exposed when `VMA_PUBLIC_GA_ONLY=true`.
- **VMA Readiness**: `Complete` is fully usable for the documented VMA contract;
  `Limited` has a material constraint described in that section; `Prototype`
  provides compatibility data without a product-grade execution path; `Missing`
  is not implemented.
- **Claude Parity**: `Compatible` means the tested wire contract is compatible;
  `Different` means material behavior intentionally differs; `N/A` marks a
  VMA-native route or an unavailable capability.

Cross-resource metadata contract:

- Metadata bags enforce 16 keys, 64-character keys, and 512-character values.
- Create requests require string metadata values.
- Update requests merge metadata by key. `null` and empty string delete a key for routes whose SDK request shape permits them.

## API Keys (VMA native)

Hosted API keys are Organization-scoped, hashed at rest, independently revocable,
and authorized through `api`, `api_keys:manage`, or `worker`. Plaintext is
returned only by create/rotate. A trusted CLI creates the first management key.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/api_keys` | Yes | Complete | N/A |
| list | `GET /v1/api_keys` | Yes | Complete | N/A |
| retrieve | `GET /v1/api_keys/{api_key_id}` | Yes | Complete | N/A |
| revoke | `POST /v1/api_keys/{api_key_id}/revoke` | Yes | Complete | N/A |
| rotate | `POST /v1/api_keys/{api_key_id}/rotate` | Yes | Complete | N/A |

## Model Providers (VMA native)

The authenticated catalog is a secret-free projection of the server-owned
provider registry. It never returns an API key, private environment-variable
name, base URL, model kwargs, or Session-specific credential availability.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| list | `GET /v1/model_providers` | Yes | Complete | N/A |
| retrieve | `GET /v1/model_providers/{provider_id}` | Yes | Complete | N/A |

## Agents

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/agents` | Yes | Complete | Compatible |
| retrieve | `GET /v1/agents/{agent_id}` | Yes | Complete | Compatible |
| update | `POST /v1/agents/{agent_id}` | Yes | Complete | Compatible |
| update alias | `PATCH /v1/agents/{agent_id}` | Yes | Complete | Compatible |
| list | `GET /v1/agents` | Yes | Complete | Compatible |
| archive | `POST /v1/agents/{agent_id}/archive` | Yes | Complete | Compatible |
| list versions | `GET /v1/agents/{agent_id}/versions` | Yes | Complete | Compatible |

## Environments

The work routes are the operator/self-hosted worker protocol. They are complete
for VMA's durable work ledger but excluded from the public-beta schema; remote
provider side effects are not guaranteed exactly once.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/environments` | Yes | Complete | Compatible |
| retrieve | `GET /v1/environments/{environment_id}` | Yes | Complete | Compatible |
| update | `POST /v1/environments/{environment_id}` | Yes | Complete | Compatible |
| update alias | `PATCH /v1/environments/{environment_id}` | Yes | Complete | Compatible |
| list | `GET /v1/environments` | Yes | Complete | Compatible |
| delete | `DELETE /v1/environments/{environment_id}` | Yes | Complete | Compatible |
| archive | `POST /v1/environments/{environment_id}/archive` | Yes | Complete | Compatible |
| work retrieve | `GET /v1/environments/{environment_id}/work/{work_id}` | No | Complete | Different |
| work update | `POST /v1/environments/{environment_id}/work/{work_id}` | No | Complete | Different |
| work list | `GET /v1/environments/{environment_id}/work` | No | Complete | Different |
| work ack | `POST /v1/environments/{environment_id}/work/{work_id}/ack` | No | Complete | Different |
| work heartbeat | `POST /v1/environments/{environment_id}/work/{work_id}/heartbeat` | No | Complete | Different |
| work poll | `GET /v1/environments/{environment_id}/work/poll` | No | Complete | Different |
| work stats | `GET /v1/environments/{environment_id}/work/stats` | No | Complete | Different |
| work stop | `POST /v1/environments/{environment_id}/work/{work_id}/stop` | No | Complete | Different |

## Sessions

Session creation accepts the official three-form Agent union: an Agent ID, a
`type: agent` pinned reference, or `type: agent_with_overrides`. The override
form fully replaces any provided `model`, `system`, `tools`, `mcp_servers`, or
`skills` field and preserves omitted fields. `system: null` clears the prompt;
empty arrays clear tools, MCP servers, or Skills; null is rejected for those
arrays; and `model: null` is rejected. The response returns the resolved Agent
snapshot while the base Agent and version remain unchanged. Custom Skill
`latest` references are pinned before the Session sandbox is provisioned.
Every key-based model receives one immutable create-time funding binding.
Existing CMA callers omit `funding` and use the Organization default; native
callers may request `byok`, `platform_credits`, or `organization_default`. With
no Organization billing account, the default remains BYOK and a missing model
Credential returns `422 model_credential_required`. Because the MVP persists
one funding binding per Session, the coordinator and all pinned subagents must
use the same provider.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/sessions` | Yes | Complete | Compatible |
| retrieve | `GET /v1/sessions/{session_id}` | Yes | Complete | Compatible |
| update | `POST /v1/sessions/{session_id}` | Yes | Complete | Compatible |
| update alias | `PATCH /v1/sessions/{session_id}` | Yes | Complete | Compatible |
| list | `GET /v1/sessions` | Yes | Complete | Compatible |
| delete | `DELETE /v1/sessions/{session_id}` | Yes | Complete | Compatible |
| archive | `POST /v1/sessions/{session_id}/archive` | Yes | Complete | Compatible |
| cancel compatibility helper | `POST /v1/sessions/{session_id}/cancel` | Yes | Complete | Compatible |
| resume compatibility helper | `POST /v1/sessions/{session_id}/resume` | Yes | Complete | Compatible |

## Session Events

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| list | `GET /v1/sessions/{session_id}/events` | Yes | Complete | Compatible |
| send | `POST /v1/sessions/{session_id}/events` | Yes | Complete | Compatible |
| stream | `GET /v1/sessions/{session_id}/events/stream` | Yes | Limited | Different |
| stream alias | `GET /v1/sessions/{session_id}/stream` | Yes | Limited | Different |

## Session Resources

Session creation accepts the SDK resource union for `file`, `github_repository`, and `memory_store`.
Resource responses are strict-SDK-compatible, including file mounts, memory-store snapshots, GitHub checkout shape, and GitHub token redaction.
Runtime `resources.add` follows the SDK shape and only adds files. For a bound
E2B Session it is active-and-idle-only (including idle `requires_action`),
append-only, and restricted to a direct
`/mnt/session/uploads/<filename>` mount. It reconnects the same paused Sandbox,
advances a monotonic immutable manifest revision, and rejects overlap or
replacement. An exact retry returns the existing resource.
Uploaded file mounts create new session-scoped file resources and
object-storage copies, verify the copied size and SHA-256, and enforce the
official 100 file resources per session limit. Existing resources on a bound
E2B Session cannot be updated or deleted; the update/delete routes retain their
broader stored-resource behavior for non-managed backends.
Memory-store session resources enforce the official 8 stores per session limit, can only be attached at session creation, and cannot be removed afterward.
The E2B append crosses provider state and PostgreSQL without a distributed
transaction. Only an exact retry repairs a provider seal that advanced before
the database commit; unrelated operations fail closed. There is no durable
outbox or automatic orphan recovery in this MVP.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| add | `POST /v1/sessions/{session_id}/resources` | Yes | Limited | Different |
| retrieve | `GET /v1/sessions/{session_id}/resources/{resource_id}` | Yes | Complete | Compatible |
| update | `POST /v1/sessions/{session_id}/resources/{resource_id}` | Yes | Limited | Different |
| list | `GET /v1/sessions/{session_id}/resources` | Yes | Complete | Compatible |
| delete | `DELETE /v1/sessions/{session_id}/resources/{resource_id}` | Yes | Limited | Different |

## Session Threads

Thread records and response shapes exist, but Deep Agents delegation does not
provide Claude-equivalent durable, independently steerable child-agent threads.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| retrieve | `GET /v1/sessions/{session_id}/threads/{thread_id}` | No | Prototype | Different |
| list | `GET /v1/sessions/{session_id}/threads` | No | Prototype | Different |
| archive | `POST /v1/sessions/{session_id}/threads/{thread_id}/archive` | No | Prototype | Different |
| list events | `GET /v1/sessions/{session_id}/threads/{thread_id}/events` | No | Prototype | Different |
| stream events | `GET /v1/sessions/{session_id}/threads/{thread_id}/stream` | No | Prototype | Different |

## Deployments

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/deployments` | No | Limited | Different |
| retrieve | `GET /v1/deployments/{deployment_id}` | No | Complete | Compatible |
| update | `POST /v1/deployments/{deployment_id}` | No | Limited | Different |
| list | `GET /v1/deployments` | No | Complete | Compatible |
| archive | `POST /v1/deployments/{deployment_id}/archive` | No | Complete | Compatible |
| pause | `POST /v1/deployments/{deployment_id}/pause` | No | Complete | Compatible |
| run | `POST /v1/deployments/{deployment_id}/run` | No | Complete | Compatible |
| unpause | `POST /v1/deployments/{deployment_id}/unpause` | No | Complete | Compatible |

Deployment create/update validates the referenced agent, environment, and `initial_events` containing at least one `user.message`; short-form `agent="<agent_id>"` pins the latest active agent version. Deployment list supports SDK `agent_id`/`status` filters and rejects `status` combined with `include_archived`. Deployment resources use the SDK session-resource union for files, GitHub repositories, and memory stores. Deployment responses omit write-only GitHub authorization tokens, and manual deployment runs mount deployment resources onto the created session. Paused deployments still allow manual runs while suppressing scheduled triggers, archived deployments are terminal for modification/run routes, primary-agent archive auto-archives the deployment without creating a run, failed session creation is recorded on the deployment run, and the core exposes an importable due-schedule tick for self-hosted/hosted schedulers.

## Deployment Runs

Deployment-run list supports SDK `deployment_id`, `trigger_type`, created-at filters, and exact `has_error` semantics: `true` returns runs with non-null `error`, while `false` returns runs with non-null `session_id`.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| retrieve | `GET /v1/deployment_runs/{deployment_run_id}` | No | Complete | Compatible |
| list | `GET /v1/deployment_runs` | No | Complete | Compatible |

## Vaults

Vault credentials are Organization-scoped. Active Credentials have a unique
private credential slot or `mcp_server_url` within one Vault, are limited to 20
per Vault, and keep structural keys immutable. Archiving or deleting a Vault
cascades secret purge and revocation. Native callers create a model Credential
with a public provider ID and write-only key; VMA performs the internal mapping.
For BYOK, VMA uses the first matching Vault in `vault_ids` at Session creation
and persists only the selected Credential ID. For platform funding, it persists
the exact Organization billing-account and provider-key row IDs. Later turns
reload only that selected row and fail closed after revocation or expiry rather
than changing funding sources. Secrets stay in the control plane and are not
copied into E2B. VMA never reads a model API key from process environment or
provider configuration. Keyless `fake` and `ollama` providers use credential
source `none`.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/vaults` | Yes | Complete | Compatible |
| retrieve | `GET /v1/vaults/{vault_id}` | Yes | Complete | Compatible |
| update | `POST /v1/vaults/{vault_id}` | Yes | Complete | Compatible |
| list | `GET /v1/vaults` | Yes | Complete | Compatible |
| delete | `DELETE /v1/vaults/{vault_id}` | Yes | Complete | Compatible |
| archive | `POST /v1/vaults/{vault_id}/archive` | Yes | Complete | Compatible |
| credential create | `POST /v1/vaults/{vault_id}/credentials` | No | Limited | Compatible |
| native model credential create | `POST /v1/vaults/{vault_id}/model_credentials` | Yes | Complete | N/A |
| native model credential list | `GET /v1/vaults/{vault_id}/model_credentials` | Yes | Complete | N/A |
| native model credential retrieve | `GET /v1/vaults/{vault_id}/model_credentials/{credential_id}` | Yes | Complete | N/A |
| native model credential rotate | `POST /v1/vaults/{vault_id}/model_credentials/{credential_id}` | Yes | Complete | N/A |
| native model credential archive | `POST /v1/vaults/{vault_id}/model_credentials/{credential_id}/archive` | Yes | Complete | N/A |
| native model credential delete | `DELETE /v1/vaults/{vault_id}/model_credentials/{credential_id}` | Yes | Complete | N/A |
| credential retrieve | `GET /v1/vaults/{vault_id}/credentials/{credential_id}` | No | Limited | Compatible |
| credential update | `POST /v1/vaults/{vault_id}/credentials/{credential_id}` | No | Limited | Compatible |
| credential list | `GET /v1/vaults/{vault_id}/credentials` | No | Limited | Compatible |
| credential delete | `DELETE /v1/vaults/{vault_id}/credentials/{credential_id}` | No | Limited | Compatible |
| credential archive | `POST /v1/vaults/{vault_id}/credentials/{credential_id}/archive` | No | Limited | Compatible |
| credential OAuth validate | `POST /v1/vaults/{vault_id}/credentials/{credential_id}/mcp_oauth_validate` | No | Limited | Different |

## Memory Stores

Memory records enforce SDK-compatible slash-prefixed path validation, required create content, the official 100KB content limit, and the 2000 memories per store limit. Memory list supports `path_prefix`, `depth` rollups as `memory_prefix` items, opaque page cursors, and `view`. Every non-no-op create/update/delete produces an immutable memory version; stale content preconditions return the current memory when the requested content/path already matches. Delete supports `expected_content_sha256`, and deleted memory-version responses return `null` content/hash/size while preserving the deleted path. Store-level version listing and version retrieve keep working after the memory is deleted. Memory-version list supports SDK `memory_id`, `operation`, `api_key_id`, `session_id`, `view`, and created-at filters. Redaction rejects the current live head version, and archived stores remain readable but reject API writes and new session attachments.

The E2B runtime mounts a Store's provider Volume directly. After successful
`write_file`, `edit_file`, and `execute` tool results, and again at turn exit,
VMA hashes the mounted tree and records new, changed, and removed UTF-8 files
as session-attributed Memory Versions. This is tool-boundary reconciliation,
not a per-syscall journal. Read-only Volume mounts, cross-Sandbox conflict
resolution, atomic provider/database commits, and direct Volume API writes
while a Sandbox still owns the mount remain provider/runtime limitations.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/memory_stores` | No | Complete | Compatible |
| retrieve | `GET /v1/memory_stores/{memory_store_id}` | No | Complete | Compatible |
| update | `POST /v1/memory_stores/{memory_store_id}` | No | Complete | Compatible |
| list | `GET /v1/memory_stores` | No | Complete | Compatible |
| delete | `DELETE /v1/memory_stores/{memory_store_id}` | No | Complete | Compatible |
| archive | `POST /v1/memory_stores/{memory_store_id}/archive` | No | Complete | Compatible |
| memory create | `POST /v1/memory_stores/{memory_store_id}/memories` | No | Complete | Compatible |
| memory retrieve | `GET /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | No | Complete | Compatible |
| memory update | `POST /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | No | Complete | Compatible |
| memory list | `GET /v1/memory_stores/{memory_store_id}/memories` | No | Complete | Compatible |
| memory delete | `DELETE /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | No | Complete | Compatible |
| memory version retrieve | `GET /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}` | No | Complete | Compatible |
| memory version list | `GET /v1/memory_stores/{memory_store_id}/memory_versions` | No | Complete | Compatible |
| memory version redact | `POST /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}/redact` | No | Complete | Compatible |

## Files

Files and Skill archives use a private S3-compatible bucket. VMA authenticates
downloads, and no public bucket URL is required. Presign/complete upload routes
exist for non-GA integrations but are hidden from the public-beta schema; GA
callers use the authenticated bounded upload route. Organization stored-byte quota
is enforced for File and Skill writes.

At the end of an E2B turn, VMA discovers bounded direct regular files below
`/mnt/session/outputs`, snapshots new `(path, SHA-256)` versions into
R2-compatible storage, and creates downloadable Files scoped to the Session.
`GET /v1/files?scope_id=<session_id>` includes these generated artifacts, and
the metadata and content routes expose the stored version. Nested files,
directories, symlinks, hardlinks, and files outside the output root cause
discovery to fail closed. Supported image/document blocks resolve only against
files mounted in that Session and become LangChain standard image, PDF, or text
blocks. Profiles without multimodal input receive a sandbox-path marker for
binary files instead of an invalid provider file ID.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| list | `GET /v1/files` | Yes | Limited | Compatible |
| delete | `DELETE /v1/files/{file_id}` | Yes | Limited | Compatible |
| download | `GET /v1/files/{file_id}/content` | Yes | Limited | Compatible |
| retrieve metadata | `GET /v1/files/{file_id}` | Yes | Limited | Compatible |
| upload | `POST /v1/files` | Yes | Limited | Compatible |

## Skills

Custom Skill archives are validated and versioned, but runtime behavior follows
Deep Agents and Anthropic system Skill packages are not available to VMA.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/skills` | Yes | Limited | Different |
| retrieve | `GET /v1/skills/{skill_id}` | Yes | Limited | Different |
| list | `GET /v1/skills` | Yes | Limited | Different |
| delete | `DELETE /v1/skills/{skill_id}` | Yes | Limited | Different |
| version create | `POST /v1/skills/{skill_id}/versions` | Yes | Limited | Different |
| version retrieve | `GET /v1/skills/{skill_id}/versions/{version}` | Yes | Limited | Different |
| version list | `GET /v1/skills/{skill_id}/versions` | Yes | Limited | Different |
| version delete | `DELETE /v1/skills/{skill_id}/versions/{version}` | Yes | Limited | Different |
| version download | `GET /v1/skills/{skill_id}/versions/{version}/content` | Yes | Limited | Different |

## Webhooks

The current SDK exposes webhook event types and unwrap helpers in beta, but this API pass did not find beta webhook CRUD routes in `api.md`.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| unwrap/verify helpers | SDK local helper | No | Prototype | Different |

## User Profiles

Profile records and relationship metadata are stored, but VMA has no hosted
enrollment, verification, trust-grant, or provider-attribution flow.

| Operation | Route | Public Beta | VMA Readiness | Claude Parity |
| --- | --- | --- | --- | --- |
| create | `POST /v1/user_profiles` | No | Prototype | Different |
| retrieve | `GET /v1/user_profiles/{user_profile_id}` | No | Prototype | Different |
| update | `POST /v1/user_profiles/{user_profile_id}` | No | Prototype | Different |
| list | `GET /v1/user_profiles` | No | Prototype | Different |
| create enrollment URL | `POST /v1/user_profiles/{user_profile_id}/enrollment_url` | No | Missing | N/A |
