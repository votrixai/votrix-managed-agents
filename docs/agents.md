---
title: Agents
description: Define a runnable Agent's model, instructions, tools, and Skills.
---

An Agent is a reusable, versioned definition of how work should be done. It
chooses the model, instructions, tools, and Skills that a Session uses.

Creating an Agent and creating a runnable Agent are slightly different: the
create endpoint stores the supplied definition, while the runtime exercises
that definition only when a Session receives work. Use the runnable example
below so configuration errors do not first appear during a turn.

## Create a runnable Agent

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/agents \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "name": "Research assistant",
    "model": "claude-sonnet-5",
    "system": "Research carefully and save the final brief in outputs/brief.md.",
    "tools": [{"type":"agent_toolset_20260401"}]
  }'
```

The response contains the stable Agent `id` and `version: 1`. Save the ID for
Session creation.

<Callout title="Required for Agent turns" type="warn">

Every Agent that runs a turn must declare
`{"type":"agent_toolset_20260401"}` in `tools`. The API accepts an Agent
without it, but a turn cannot start because the runtime would have no declared
sandbox filesystem and shell toolset.

</Callout>

## Creation fields

| Field | Required | Purpose |
| --- | --- | --- |
| `name` | Yes | Human-readable Agent name, 1–255 characters. |
| `model` | Yes | Model ID string, or an object with `id` and optional `thinking`. |
| `system` | No | Instructions applied to every turn. |
| `description` | No | Human-readable explanation of the Agent. |
| `tools` | For turns | Toolset and custom-tool declarations. Include `agent_toolset_20260401`. |
| `skills` | No | Up to 20 uploaded Skill references. |
| `metadata` | No | Application-owned metadata stored with the Agent version. |
| `mcp_servers` | No | Stored and versioned, but not loaded by the current runtime. |
| `multiagent` | No | Stored and versioned, but not used by the current runtime. |
| `runtime` | No | Stored with the Agent version, but not interpreted by the current runtime. |

See [Create Agent](/docs/api/agents/create_agent_v1_agents_post) for the exact
request and response schemas.

## Choose a model

A bare model ID is shorthand for an object containing `id`:

```json
"model": "claude-sonnet-5"
```

```json
"model": {
  "id": "claude-sonnet-5",
  "thinking": "low"
}
```

When supplied, `thinking` is `none`, `low`, or `high`. Some models do not expose
a thinking control; omit the field for those models. The Quickstart and API
examples use `claude-sonnet-5` as a concrete model ID.

Agent creation stores the model selection without running it. An unknown model
ID is therefore reported when the first turn tries to use it, not while the
Agent is being created.

## Sandbox tools

The required Agent toolset makes these sandbox tools available:

- `execute`, `ls`, `read_file`, `read_image`, `write_file`, and `edit_file`;
- `glob` and `grep`;
- `write_todos`.

Tools run without a confirmation by default. To require approval for selected
tools, add a per-tool permission:

```json
{
  "type": "agent_toolset_20260401",
  "default_config": {
    "permission_policy": {"type": "always_allow"}
  },
  "configs": [{
    "name": "execute",
    "permission_policy": {"type": "always_ask"}
  }]
}
```

An `always_ask` call produces an action request in the Session event stream.
Your application must approve or reject it before the Agent continues. See
[Session Events](/docs/session-events).

Add web search and page fetching with the web toolset:

```json
{"type":"web_toolset_20260401"}
```

Web tools are available only when they are enabled on the VMA deployment.

## Custom tools

A custom tool describes work performed by your application rather than by VMA:

```json
{
  "type": "custom",
  "name": "lookup_order",
  "description": "Look up an order in the commerce system.",
  "input_schema": {
    "type": "object",
    "properties": {
      "order_id": {"type": "string"}
    },
    "required": ["order_id"]
  }
}
```

Every custom-tool call pauses the turn and emits `agent.custom_tool_use`. Return
its result with `user.custom_tool_result` as described in
[Session Events](/docs/session-events#answer-requested-actions).

## Attach Skills

Upload the Skill first, then reference its ID from the Agent:

```json
"skills": [
  {"skill_id": "skill_..."}
]
```

The selected Skill references are captured in the Agent version. When a Session
starts, VMA validates the referenced Skills and installs them in the sandbox.
An Agent version may reference at most 20 Skills.

## Outputs

Tell the Agent to save deliverables below `outputs/`, for example
`outputs/brief.md`. VMA collects eligible files from that directory after a turn
and exposes them through the Files API. Uploaded Session inputs are available
below `uploads/`.

## Updates and versions

Every effective Agent update creates an immutable version. Send only the fields
that change; omitted fields keep their active values. Lists and objects that you
do send replace their previous values, so include the complete new `tools` or
`skills` list.

Read [Agent Versioning](/docs/agent-versioning) for update, version selection,
and archiving behavior.
