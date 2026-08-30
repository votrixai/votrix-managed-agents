---
title: Core Concepts
description: The public resources and rules that make up VMA.
---

VMA is organized around a small set of public resources. Understanding how
they connect is enough to build an integration; the service behind them is not
part of the API contract.

## Resource model

```text
Agent + Environment + Account + optional Files or Memory Stores
                              |
                              v
                           Session
                              |
                              v
                    Events and output Files
```

### Organization

The top-level access boundary. An API key identifies one Organization, and a
request can access only resources in that Organization.

### Agent

A reusable definition of how work should be done: its model, instructions,
tools, and Skills. Agent changes create versions so existing Sessions can keep
using the version with which they started.

Read [Agents](/docs/agents) for a runnable definition and tool configuration.

### Environment

The sandbox setup available to an Agent. A Session can start after its chosen
Environment reports `build_state: "ready"`.

Read [Environments](/docs/environments) for package recipes, machine settings,
and the asynchronous build lifecycle.

### Account

The usage and spending boundary for Agent work. Every Session is assigned one
Account for its lifetime. If you omit `account_id`, VMA uses the Organization's
default Account.

### Session

One ongoing job. A Session connects an Agent version, an Environment, an
Account, attached resources, and an ordered event history. Follow-up messages
continue the same job instead of starting over.

Session provisioning creates a sandbox and can outlive a caller's network
request. Set `idempotency_key` when creating one if the create may be retried:
the value is scoped to the Organization, and a retry returns the first Session
instead of provisioning a second sandbox.

File upload/import accepts the same optional operation identity. This matters
when a large URL import completes but its HTTP response is lost: retrying the
key returns the first durable File rather than storing another copy.

### Skill

Reusable guidance for a type of work. An Agent can reference Skills, and those
references are captured by the Agent version used for a Session.

### File

A durable input or output. Upload a File before a Session to make it available
as an input, or list Files by Session after the Agent creates deliverables.

### Memory Store

A shared workspace that can be attached when a Session starts. The Agent can
read and update it from the Session sandbox, making selected context available
to later Sessions.

### Event

An ordered record of what you or the Agent did in a Session. Events carry a
per-Session `seq` value, which lets clients page and resume streams reliably.

## Rules to build around

- **The API key chooses the Organization.** Do not send a separate Organization
  header.
- **Resource IDs are explicit.** Requests connect resources through IDs such as
  `agent_id`, `environment_id`, and `account_id`.
- **A Session keeps its choices.** The selected Agent version, Environment,
  Account, Files, and Memory Stores do not silently change during the Session.
- **Events are ordered.** Store the latest `seq` you processed and use it to
  resume without skipping work.
- **Archive and delete are different.** Archiving retires a resource while
  preserving references and history. Deletion is available only when the
  resource is no longer needed by other resources.

For exact fields and allowed values, use the [API Reference](/docs/api).
