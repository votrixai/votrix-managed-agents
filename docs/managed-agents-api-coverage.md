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

Status legend:

- `implemented`: route and basic lifecycle behavior are implemented.
- `partial`: route exists and persists data, but exact schema or production semantics are incomplete.
- `stub`: route exists as a compatibility placeholder.
- `todo`: not implemented.

Cross-resource metadata contract:

- Metadata bags enforce 16 keys, 64-character keys, and 512-character values.
- Create requests require string metadata values.
- Update requests merge metadata by key. `null` and empty string delete a key for routes whose SDK request shape permits them.

## API Keys (VMA native)

Hosted API keys are workspace-scoped, hashed at rest, independently revocable,
and authorized through `api`, `api_keys:manage`, or `worker`. Plaintext is
returned only by create/rotate. A trusted CLI creates the first management key.

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/api_keys` | implemented |
| list | `GET /v1/api_keys` | implemented |
| retrieve | `GET /v1/api_keys/{api_key_id}` | implemented |
| revoke | `POST /v1/api_keys/{api_key_id}/revoke` | implemented |
| rotate | `POST /v1/api_keys/{api_key_id}/rotate` | implemented |

## Model Providers (VMA native)

The authenticated catalog is a secret-free projection of the server-owned
provider registry. It never returns an API key, private environment-variable
name, base URL, model kwargs, or whether a server key is configured.

| Operation | Route | Status |
| --- | --- | --- |
| list | `GET /v1/model_providers` | implemented |
| retrieve | `GET /v1/model_providers/{provider_id}` | implemented |

## Agents

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/agents` | implemented |
| retrieve | `GET /v1/agents/{agent_id}` | implemented |
| update | `POST /v1/agents/{agent_id}` | implemented |
| update alias | `PATCH /v1/agents/{agent_id}` | implemented |
| list | `GET /v1/agents` | implemented |
| archive | `POST /v1/agents/{agent_id}/archive` | implemented |
| list versions | `GET /v1/agents/{agent_id}/versions` | implemented |

## Environments

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/environments` | implemented |
| retrieve | `GET /v1/environments/{environment_id}` | implemented |
| update | `POST /v1/environments/{environment_id}` | implemented |
| update alias | `PATCH /v1/environments/{environment_id}` | implemented |
| list | `GET /v1/environments` | implemented |
| delete | `DELETE /v1/environments/{environment_id}` | implemented |
| archive | `POST /v1/environments/{environment_id}/archive` | implemented |
| work retrieve | `GET /v1/environments/{environment_id}/work/{work_id}` | partial |
| work update | `POST /v1/environments/{environment_id}/work/{work_id}` | partial |
| work list | `GET /v1/environments/{environment_id}/work` | partial |
| work ack | `POST /v1/environments/{environment_id}/work/{work_id}/ack` | partial |
| work heartbeat | `POST /v1/environments/{environment_id}/work/{work_id}/heartbeat` | partial |
| work poll | `GET /v1/environments/{environment_id}/work/poll` | partial |
| work stats | `GET /v1/environments/{environment_id}/work/stats` | partial |
| work stop | `POST /v1/environments/{environment_id}/work/{work_id}/stop` | partial |

## Sessions

Session creation accepts the official three-form Agent union: an Agent ID, a
`type: agent` pinned reference, or `type: agent_with_overrides`. The override
form fully replaces any provided `model`, `system`, `tools`, `mcp_servers`, or
`skills` field and preserves omitted fields. `system: null` clears the prompt;
empty arrays clear tools, MCP servers, or Skills; null is rejected for those
arrays; and `model: null` is rejected. The response returns the resolved Agent
snapshot while the base Agent and version remain unchanged. Custom Skill
`latest` references are pinned before the Session sandbox is provisioned.

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/sessions` | implemented |
| retrieve | `GET /v1/sessions/{session_id}` | implemented |
| update | `POST /v1/sessions/{session_id}` | implemented |
| update alias | `PATCH /v1/sessions/{session_id}` | implemented |
| list | `GET /v1/sessions` | implemented |
| delete | `DELETE /v1/sessions/{session_id}` | implemented |
| archive | `POST /v1/sessions/{session_id}/archive` | implemented |
| cancel compatibility helper | `POST /v1/sessions/{session_id}/cancel` | implemented |
| resume compatibility helper | `POST /v1/sessions/{session_id}/resume` | implemented |

## Session Events

| Operation | Route | Status |
| --- | --- | --- |
| list | `GET /v1/sessions/{session_id}/events` | implemented |
| send | `POST /v1/sessions/{session_id}/events` | implemented; optional durable `Idempotency-Key` replay |
| stream | `GET /v1/sessions/{session_id}/events/stream` | partial |
| stream alias | `GET /v1/sessions/{session_id}/stream` | partial |

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

| Operation | Route | Status |
| --- | --- | --- |
| add | `POST /v1/sessions/{session_id}/resources` | implemented for files; bound E2B Sessions require an idle append |
| retrieve | `GET /v1/sessions/{session_id}/resources/{resource_id}` | implemented |
| update | `POST /v1/sessions/{session_id}/resources/{resource_id}` | partial; rejected after E2B binding |
| list | `GET /v1/sessions/{session_id}/resources` | implemented |
| delete | `DELETE /v1/sessions/{session_id}/resources/{resource_id}` | partial; rejected after E2B binding |

## Session Threads

| Operation | Route | Status |
| --- | --- | --- |
| retrieve | `GET /v1/sessions/{session_id}/threads/{thread_id}` | partial |
| list | `GET /v1/sessions/{session_id}/threads` | partial |
| archive | `POST /v1/sessions/{session_id}/threads/{thread_id}/archive` | partial |
| list events | `GET /v1/sessions/{session_id}/threads/{thread_id}/events` | partial |
| stream events | `GET /v1/sessions/{session_id}/threads/{thread_id}/stream` | partial |

## Deployments

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/deployments` | partial |
| retrieve | `GET /v1/deployments/{deployment_id}` | partial |
| update | `POST /v1/deployments/{deployment_id}` | partial |
| list | `GET /v1/deployments` | partial |
| archive | `POST /v1/deployments/{deployment_id}/archive` | partial |
| pause | `POST /v1/deployments/{deployment_id}/pause` | partial |
| run | `POST /v1/deployments/{deployment_id}/run` | partial |
| unpause | `POST /v1/deployments/{deployment_id}/unpause` | partial |

Deployment create/update validates the referenced agent, environment, and `initial_events` containing at least one `user.message`; short-form `agent="<agent_id>"` pins the latest active agent version. Deployment list supports SDK `agent_id`/`status` filters and rejects `status` combined with `include_archived`. Deployment resources use the SDK session-resource union for files, GitHub repositories, and memory stores. Deployment responses omit write-only GitHub authorization tokens, and manual deployment runs mount deployment resources onto the created session. Paused deployments still allow manual runs while suppressing scheduled triggers, archived deployments are terminal for modification/run routes, primary-agent archive auto-archives the deployment without creating a run, failed session creation is recorded on the deployment run, and the core exposes an importable due-schedule tick for self-hosted/hosted schedulers.

## Deployment Runs

Deployment-run list supports SDK `deployment_id`, `trigger_type`, created-at filters, and exact `has_error` semantics: `true` returns runs with non-null `error`, while `false` returns runs with non-null `session_id`.

| Operation | Route | Status |
| --- | --- | --- |
| retrieve | `GET /v1/deployment_runs/{deployment_run_id}` | partial |
| list | `GET /v1/deployment_runs` | partial |

## Vaults

Vault credentials are Workspace-scoped. Active Credentials have a unique
private credential slot or `mcp_server_url` within one Vault, are limited to 20
per Vault, and keep structural keys immutable. Archiving or deleting a Vault
cascades secret purge and revocation. Native callers create a model Credential
with a public provider ID and write-only key; VMA performs the internal mapping.
VMA uses the first matching Vault in `vault_ids` at Session creation and
persists only the selected Credential ID. Later turns reload that exact
Credential and fail closed after revocation instead of changing payer. The
secret stays in the control plane and is not copied into E2B.

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/vaults` | partial |
| retrieve | `GET /v1/vaults/{vault_id}` | partial |
| update | `POST /v1/vaults/{vault_id}` | partial |
| list | `GET /v1/vaults` | partial |
| delete | `DELETE /v1/vaults/{vault_id}` | partial |
| archive | `POST /v1/vaults/{vault_id}/archive` | partial |
| credential create | `POST /v1/vaults/{vault_id}/credentials` | partial |
| native model credential create | `POST /v1/vaults/{vault_id}/model_credentials` | implemented |
| native model credential list | `GET /v1/vaults/{vault_id}/model_credentials` | implemented |
| native model credential retrieve | `GET /v1/vaults/{vault_id}/model_credentials/{credential_id}` | implemented |
| native model credential rotate | `POST /v1/vaults/{vault_id}/model_credentials/{credential_id}` | implemented |
| native model credential archive | `POST /v1/vaults/{vault_id}/model_credentials/{credential_id}/archive` | implemented |
| native model credential delete | `DELETE /v1/vaults/{vault_id}/model_credentials/{credential_id}` | implemented |
| credential retrieve | `GET /v1/vaults/{vault_id}/credentials/{credential_id}` | partial |
| credential update | `POST /v1/vaults/{vault_id}/credentials/{credential_id}` | partial |
| credential list | `GET /v1/vaults/{vault_id}/credentials` | partial |
| credential delete | `DELETE /v1/vaults/{vault_id}/credentials/{credential_id}` | partial |
| credential archive | `POST /v1/vaults/{vault_id}/credentials/{credential_id}/archive` | partial |
| credential OAuth validate | `POST /v1/vaults/{vault_id}/credentials/{credential_id}/mcp_oauth_validate` | partial |

## Memory Stores

Memory records enforce SDK-compatible slash-prefixed path validation, required create content, the official 100KB content limit, and the 2000 memories per store limit. Memory list supports `path_prefix`, `depth` rollups as `memory_prefix` items, `order`, `order_by`, and `view`. Every non-no-op create/update/delete produces an immutable memory version; stale content preconditions return the current memory when the requested content/path already matches. Delete supports `expected_content_sha256`, and deleted memory-version responses return `null` content/hash/size while preserving the deleted path. Store-level version listing and version retrieve keep working after the memory is deleted. Memory-version list supports SDK `memory_id`, `operation`, `api_key_id`, `session_id`, `view`, and created-at filters. Redaction rejects the current live head version, and archived stores remain readable but reject writes and new session attachments.

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/memory_stores` | partial |
| retrieve | `GET /v1/memory_stores/{memory_store_id}` | partial |
| update | `POST /v1/memory_stores/{memory_store_id}` | partial |
| list | `GET /v1/memory_stores` | partial |
| delete | `DELETE /v1/memory_stores/{memory_store_id}` | partial |
| archive | `POST /v1/memory_stores/{memory_store_id}/archive` | partial |
| memory create | `POST /v1/memory_stores/{memory_store_id}/memories` | partial |
| memory retrieve | `GET /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | partial |
| memory update | `POST /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | partial |
| memory list | `GET /v1/memory_stores/{memory_store_id}/memories` | partial |
| memory delete | `DELETE /v1/memory_stores/{memory_store_id}/memories/{memory_id}` | partial |
| memory version retrieve | `GET /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}` | partial |
| memory version list | `GET /v1/memory_stores/{memory_store_id}/memory_versions` | partial |
| memory version redact | `POST /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}/redact` | partial |

## Files

Files and Skill archives use a private S3-compatible bucket. VMA authenticates
downloads, and no public bucket URL is required. Presign/complete upload routes
exist for non-GA integrations but are hidden from the public-beta schema; GA
callers use the authenticated bounded upload route. Workspace stored-byte quota
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

| Operation | Route | Status |
| --- | --- | --- |
| list | `GET /v1/files` | partial |
| delete | `DELETE /v1/files/{file_id}` | partial |
| download | `GET /v1/files/{file_id}/content` | partial |
| retrieve metadata | `GET /v1/files/{file_id}` | partial |
| upload | `POST /v1/files` | partial |

## Skills

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/skills` | partial |
| retrieve | `GET /v1/skills/{skill_id}` | partial |
| list | `GET /v1/skills` | partial |
| delete | `DELETE /v1/skills/{skill_id}` | partial |
| version create | `POST /v1/skills/{skill_id}/versions` | partial |
| version retrieve | `GET /v1/skills/{skill_id}/versions/{version}` | partial |
| version list | `GET /v1/skills/{skill_id}/versions` | partial |
| version delete | `DELETE /v1/skills/{skill_id}/versions/{version}` | partial |
| version download | `GET /v1/skills/{skill_id}/versions/{version}/content` | partial |

## Webhooks

The current SDK exposes webhook event types and unwrap helpers in beta, but this API pass did not find beta webhook CRUD routes in `api.md`.

| Operation | Route | Status |
| --- | --- | --- |
| unwrap/verify helpers | SDK local helper | partial; Standard Webhooks-compatible helpers in `app.webhooks` |

## User Profiles

| Operation | Route | Status |
| --- | --- | --- |
| create | `POST /v1/user_profiles` | partial |
| retrieve | `GET /v1/user_profiles/{user_profile_id}` | partial |
| update | `POST /v1/user_profiles/{user_profile_id}` | partial |
| list | `GET /v1/user_profiles` | partial |
| create enrollment URL | `POST /v1/user_profiles/{user_profile_id}/enrollment_url` | not implemented; VMA has no hosted enrollment or trust-grant flow |
