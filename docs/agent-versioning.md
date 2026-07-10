# Agent Versioning Contract

This project follows the Claude Managed Agents versioning shape for Agent resources.

Official basis:

- Claude Managed Agents "Define your agent": https://platform.claude.com/docs/en/managed-agents/agent-setup
- Claude Managed Agents reference: https://platform.claude.com/docs/en/managed-agents/reference

## Resource Shape

An Agent is a reusable, versioned configuration. The public Agent response exposes:

- `id`
- `type: "agent"`
- `name`
- `model`
- `system`
- `description`
- `tools`
- `mcp_servers`
- `skills`
- `multiagent`
- `metadata`
- `version`
- `created_at`
- `updated_at`
- `archived_at`

The database stores:

- `agents`: mutable resource pointer and active version.
- `agent_versions`: immutable version snapshots.

Sessions must pin `agent_id` and `agent_version` at creation time.

## Create

Creating an Agent creates version `1`.

## Update

The update request must include the current `version`. This is an optimistic concurrency guard.

If the supplied version does not match the Agent's current active version, return `409`.

Update semantics:

- Omitted fields are preserved.
- `name` and `model` are replaced when provided and cannot be `null`.
- `system` and `description` are replaced when provided and can be cleared with `null`.
- `tools`, `mcp_servers`, and `skills` are full replacements. `null` and `[]` both clear the field.
- `multiagent` is replaced as a whole and can be cleared with `null`.
- `metadata` is merged by key. Provided keys are added or updated. Setting a key to an empty string deletes that key.
- If the resulting config is identical to the current version, no new version is created and the existing version is returned.
- `multiagent.agents` entries that reference another Agent without an explicit `version` are resolved to that Agent's current active version when the coordinator is created or updated. The stored roster is pinned and does not drift when referenced Agents are updated later.

## Archive

Archiving makes the Agent read-only.

New sessions cannot reference an archived Agent. Existing sessions pinned to older versions continue to run.

## YAML / JSON Files

Claude Managed Agents does not require uploading a YAML manifest to publish a new Agent version. The API accepts JSON resource fields for create/update. CLI examples pass JSON-like values for nested fields such as `model` and `tools`.

Skills are separate resources referenced by Agents. Skills may contain markdown, YAML, JSON schemas, scripts, templates, and assets, but that is not the Agent version publish mechanism itself.
