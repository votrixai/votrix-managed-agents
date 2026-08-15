---
title: Quickstart
description: Create an Agent, start a Session, and send its first job.
---

This walkthrough makes five API calls. You need a VMA API key and a model ID
available to your Organization. Replace every `YOUR_...` value before sending
the requests.

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
    "model": "YOUR_MODEL_ID",
    "system": "Research the topic carefully and save the final brief in outputs/brief.md."
  }'
```

Save the returned Agent `id` as `YOUR_AGENT_ID`.

## 2. Create an Environment

An Environment describes the sandbox the Agent will use.

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/environments \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{"name":"General workspace"}'
```

Save the returned Environment `id` as `YOUR_ENVIRONMENT_ID`. If its
`build_state` is `building`, retrieve it again and wait for `ready` before
starting a Session.

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

Next, read [Core Concepts](/docs/core-concepts), learn the event flow in
[Session Events](/docs/session-events), or explore every request and response
in the [API Reference](/docs/api).
