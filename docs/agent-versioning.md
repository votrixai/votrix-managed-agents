---
title: Agent Versioning
description: Agent snapshots, optimistic updates, and Session pinning in the active rewrite.
---

Snapshot: 2026-07-30

An Agent is a stable public handle whose executable definition lives in
immutable versions.

```text
agents
  id
  active_version ───────┐
                       v
agent_versions (agent_id, version)
```

## Stored fields

Each Agent version snapshots:

- name and description;
- model;
- system prompt;
- tools;
- MCP server definitions;
- Skill references;
- multi-agent configuration;
- runtime configuration;
- metadata.

The stable `agents` row duplicates the active name, description, and metadata
for efficient listing, and points to the current version number.

The active runtime currently consumes the model, system prompt, tools, and
Skill references. MCP server definitions, the stored multi-agent roster, and
the generic runtime object are persisted for contract evolution but are not
consumed yet. Deep Agents' built-in general-purpose `task` delegation is
separate from that stored roster and remains available.

## Create

`POST /v1/agents` creates the stable row and version `1` in one transaction.
A bare model string is normalized to:

```json
{"id": "claude-opus-5"}
```

The same shape is stored when the caller supplies the object explicitly.

## Update

`POST` or `PATCH /v1/agents/{agent_id}` requires the current `version` in the
request body.

The version is an optimistic concurrency guard. If the Agent has advanced since
the client read it, the update returns `409` instead of overwriting a newer
snapshot.

Update behavior:

- omitted fields keep the active version's value;
- provided list and object fields replace the complete previous value;
- metadata is replaced as a whole; it is not merged by key;
- the next version number is `active_version + 1`;
- an edit whose resulting snapshot is identical returns the existing active
  version and does not create a duplicate.

The comparison covers every versioned field, including runtime configuration
and metadata.

## Session pinning

Session creation accepts an optional `agent_version`.

- When omitted, the service reads the Agent's current active version.
- When supplied, that exact Organization-scoped version must exist.
- The Session stores both `agent_id` and the resolved version number.

Every turn reloads that pinned snapshot. Updating the Agent later does not
change the model, prompt, tools, or Skills of an existing Session.

## Archive behavior

Archiving records `archived_at` on the stable Agent row. Archived Agents are
hidden from ordinary list responses and cannot be updated.

The rewrite does not yet reject an archived Agent during Session creation. That
is a current gap, not a promise that archived Agents are intentionally reusable.
Existing Sessions remain pinned to their version either way.

## Version listing

`GET /v1/agents/{agent_id}/versions` orders versions by their numeric version,
using the shared `before_id` and `after_id` cursor envelope. Version responses
include the stored `runtime` object in addition to the public Agent fields.
