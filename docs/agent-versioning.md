---
title: Agent Versioning
description: How Agent changes affect new and existing Sessions.
---

An Agent keeps one stable `id` while its definition changes through numbered
versions. This lets you improve an Agent without unexpectedly changing work
that has already started.

## Creating an Agent

`POST /v1/agents` creates version `1`. The response includes the stable Agent
`id` and its current `version`.

## Updating an Agent

Use `PATCH /v1/agents/{agent_id}` or `POST /v1/agents/{agent_id}` and send only
the fields you want to change.

- Omitted fields keep their current values.
- A provided list or object replaces the previous value for that field.
- An update that produces no change returns the current version instead of
  adding a duplicate.
- A successful change returns the new version number.

The request does not require a version number. If two requests change the same
field, the last accepted update is the value used by later Sessions.

## Choosing a version for a Session

Session creation accepts an optional `agent_version`:

- Omit it to use the Agent's active version at the time the Session is created.
- Supply it to use that exact version.

The resolved version is returned as `session.agent_version` and stays fixed for
the Session. Updating the Agent later affects new Sessions, not existing ones.

```json
{
  "agent_id": "agent_...",
  "agent_version": 3,
  "environment_id": "env_..."
}
```

Use `GET /v1/agents/{agent_id}/versions` to list available versions and
`GET /v1/agents/{agent_id}?version=3` to retrieve one.

## Archiving

Archiving retires an Agent from new work without changing the version held by
an existing Session. Archived Agents are hidden from ordinary lists unless
`include_archived=true` is supplied.
