---
title: Service Limits
description: Size, time, and pagination limits enforced by the control plane and runtime.
---

Package defaults, enforced server-side. Hosted deployments may configure
different values; where a limit is exceeded the response is a `4xx` with a
[standard error envelope](./errors.md).

## Sizes

| Limit | Default |
|---|---|
| Single file upload | 50 MiB |
| Aggregate Session input (files materialized at Session create) | 64 MiB |
| Skill archive | 25 MiB |

## Execution

| Limit | Default |
|---|---|
| Single turn execution budget | 900 seconds |
| Graph steps per turn (model + tool iterations) | 250 |
| Single sandbox command | 900 seconds |

A turn that exceeds its budget fails as a transient runtime error and follows
the [`session.error` retry semantics](./errors.md#sessionerror-events).

## Pagination and streaming

| Limit | Default |
|---|---|
| `GET /v1/sessions/{id}/events` page size | up to 1,000 events |
| Streaming batch per poll cycle | 100 events |

Event history depth and retention are deployment policies; see the event
stream's `after_seq=0` replay to read a Session from the beginning.
