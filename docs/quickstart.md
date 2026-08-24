---
title: Quickstart
description: Create an Agent, start a Session, and send its first job.
---

This walkthrough makes five API calls. You need a VMA API key. It uses
`claude-sonnet-5` as a concrete model ID; replace it if your Organization uses
a different supported model. Replace every `YOUR_...` value before sending the
requests.

Keep API keys on a trusted server. Send the key in the `x-api-key` header on
every request.

## 1. Create an Agent

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/agents \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "name": "Research assistant",
    "model": "claude-sonnet-5",
    "system": "Research the topic carefully and save the final brief in outputs/brief.md.",
    "tools": [{"type":"agent_toolset_20260401"}]
  }'
```

Save the returned Agent `id` as `YOUR_AGENT_ID`. The declared Agent toolset is
required for turns and gives the Agent its sandbox filesystem and shell tools.
See [Agents](/docs/agents) for models, tool permissions, custom tools, and
Skills.

## 2. Create an Environment

An Environment describes the sandbox the Agent will use. This minimal request
uses the base image and becomes ready immediately.

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/environments \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{"name":"General workspace"}'
```

Save the returned Environment `id` as `YOUR_ENVIRONMENT_ID`. A custom
Environment can declare packages, CPU, and memory; it returns
`build_state: "building"` until its image is ready. See
[Environments](/docs/environments) for a complete build recipe and polling
flow.

## 3. Start a Session

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/sessions \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "agent_id": "YOUR_AGENT_ID",
    "environment_id": "YOUR_ENVIRONMENT_ID",
    "title": "First research brief"
  }'
```

Save the returned Session `id` as `YOUR_SESSION_ID`. Because `account_id` is
omitted, this Session uses the Organization's default Account.

## 4. Send the work

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/sessions/YOUR_SESSION_ID/events \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "events": [{
      "type": "user.message",
      "content": [{"type":"text","text":"Write a short brief on the future of vertical AI agents."}]
    }]
  }'
```

The accepted message starts an Agent turn.

## 5. Follow the result

Open the Session's event stream:

```bash
curl --no-buffer \
  --url https://api.vma.votrixai.com/v1/sessions/YOUR_SESSION_ID/events/stream \
  --header 'accept: text/event-stream' \
  --header 'x-api-key: YOUR_API_KEY'
```

The stream includes Agent messages, tool activity, and status changes. A
`session.status_idle` event means the current turn has stopped. Its
`stop_reason` tells you whether the Agent finished, needs an answer, was
interrupted, or encountered an error.

Files created as deliverables can be listed with:

```text
GET /v1/files?scope_id=YOUR_SESSION_ID
```

Next, configure more capabilities in [Agents](/docs/agents), build a custom
[Environment](/docs/environments), learn the event flow in
[Session Events](/docs/session-events), or explore every request and response
in the [API Reference](/docs/api).
