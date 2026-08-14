---
title: Service Limits
description: Public request, resource, and streaming limits.
---

Build clients around these current public limits. A request beyond a validated
field range returns `400` or `422`; an oversized upload returns `413`.

## Files, Skills, and Session resources

| Limit | Value |
| --- | ---: |
| One File upload | 100 MiB |
| Resources attached when creating a Session | 100 |
| Memory Stores attached to one Session | 8 |
| Memory Store attachment instructions | 4,096 characters |
| Attached File path | 512 characters |
| Skills referenced by one Agent version | 20 |
| One Skill archive | 25 MiB |
| Files in one Skill archive | 1,000 |
| Unpacked Skill archive | 100 MiB |
| Skill name | 64 characters |
| Skill description | 1,024 characters |
| Image read by an Agent | 10 MiB |
| Output Files collected after a turn | 50 |
| One collected output File | 100 MiB |

## Memory Stores

| Limit | Value |
| --- | ---: |
| Name | 255 characters |
| Description | 1,024 characters |
| Metadata keys | 16 |
| Metadata key | 64 characters |
| Metadata value | 512 characters |
| One path-addressed Memory Store file write | 100 MiB |
| Memory Store file path | 1,024 UTF-8 bytes |

## Environments and turns

| Limit | Value |
| --- | ---: |
| Environment CPU setting | 1–8 |
| Environment memory setting | 512–8,192 MiB |
| One Agent turn | 20 minutes |

## Pagination

Most list endpoints return 20 items by default and accept up to 1,000. Memory
Store lists accept up to 100. Use the cursor fields returned by each endpoint;
do not construct opaque page tokens yourself.

## Event streaming

| Limit | Value |
| --- | ---: |
| Events returned in one stream batch | 100 |
| SSE keep-alive interval | 15 seconds |
| One SSE response | 30 minutes |

The 30-minute response lifetime is not an event-retention limit. Reconnect with
`Last-Event-ID` or `after_seq` to continue.

## File downloads

A File download URL is valid for five minutes. Request a new URL from
`GET /v1/files/{file_id}/content` after it expires.
