---
title: Service Limits
description: Limits enforced by the active peteryue rewrite.
---

Snapshot: 2026-08-01

These values come from the active `app/` code. Most are code constants rather
than deployment settings.

## Uploads and workspace

| Limit | Value |
| --- | ---: |
| Single File upload | 100 MiB |
| File resources attached when creating a Session | 100 |
| Memory Stores attached when creating a Session | 8 |
| Memory Store attachment instructions | 4,096 characters |
| Live Memories in one Memory Store | 2,000 |
| One Memory path | 1,024 UTF-8 bytes |
| One Memory content body | 102,400 UTF-8 bytes |
| Attached File path length | 512 characters |
| Skill archive upload | 25 MiB |
| Skill archive members | 1,000 |
| Skill archive unpacked size | 100 MiB |
| Maximum per-entry Skill compression ratio | 1,000:1 |
| Skills referenced by one Agent version at Session creation | 20 |
| Skill name | 64 characters |
| Skill description | 1,024 characters |
| Image read by `read_image` | 10 MiB |
| Output files considered after a turn | 50 by default |
| Single collected output file | 100 MiB by default |

The active rewrite does not enforce the previously documented 64 MiB aggregate
Session-input limit.

## Environment recipes

| Limit | Value |
| --- | ---: |
| CPU | 1–8 |
| Memory | 512–8,192 MiB |

Supported package managers are apt, Cargo, RubyGems, Go, npm, and pip.

## Execution

| Limit | Value |
| --- | ---: |
| One turn | 600 seconds |
| Session lease | 120 seconds |
| Lease heartbeat | every 45 seconds |
| Sandbox idle timeout | 900 seconds by default |
| One sandbox command | 300 seconds by default |
| Cloud Tasks dispatch deadline | 720 seconds |

The sandbox idle and command timeouts are configurable through
`SANDBOX_TIMEOUT_SECONDS` and `SANDBOX_COMMAND_TIMEOUT_SECONDS`. The turn,
lease, heartbeat, and dispatch deadline are code constants.

The active runtime does not enforce the previously documented 250 graph-step
limit.

## Pagination

| Limit | Value |
| --- | ---: |
| Default page size | 20 |
| Maximum page size | 1,000 |

Every list endpoint clamps `limit` into the range `1..1000`. It uses stable
`before_id` and `after_id` cursors rather than offsets.

## Event streaming

| Limit | Value |
| --- | ---: |
| Events read per poll | 100 |
| Poll interval | 300 ms |
| SSE keep-alive interval | 15 seconds |
| Maximum lifetime of one SSE response | 30 minutes |

The client reconnects with `Last-Event-ID` or `after_seq` after the 30-minute
response ends. Durable event history remains in `session_events`; the response
limit is not an event-retention limit.

## Signed URLs

| Use | Lifetime |
| --- | ---: |
| File download | 5 minutes |
| Sandbox input, Skill, and output transfer | 10 minutes by default |

Signed URLs grant access to one object and may include its object key in the URL
path. Response models do not expose a separate storage-key field, and long-lived
bucket credentials are not returned to clients or copied into the sandbox.
Sandbox transfer lifetime is configurable with `TRANSFER_URL_TTL_SECONDS`.
