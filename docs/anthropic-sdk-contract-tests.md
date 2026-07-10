# Anthropic SDK Contract Tests

These tests use the official `anthropic` Python SDK against the local ASGI app with strict response validation enabled. They are meant to prevent API-shape guessing.

Install the contract test dependency:

```bash
uv sync --extra dev --extra contract
```

Run the contract suite:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/contract -p no:cacheprovider
```

Current passing coverage:

- Managed Agents beta SDK surface discovery.
- `client.beta.agents.create/retrieve/update/list/archive`.
- `client.beta.agents.retrieve(version=...)` and `client.beta.agents.versions.list`.
- Agent multiagent roster pinning, including SDK string roster entries.
- Metadata merge/delete behavior and bag limits for agents and representative generic resources.
- `client.beta.environments.create/retrieve/update/list/archive/delete`, including self-hosted `scope`.
- `client.beta.environments.work.poll/list/retrieve/update/ack/heartbeat/stats/stop`.
- `client.beta.sessions.create/retrieve/update/list/archive/delete`, including `deployment_id` filtering.
- `client.beta.sessions.create` resource union for `file`, `github_repository`, and `memory_store`, including GitHub token redaction.
- `client.beta.sessions.update` metadata/title patches and session-local agent `tools`/`mcp_servers` replacement.
- `client.beta.sessions.events.send/list`, including `user.tool_result`, `system.message` batch ordering, and SDK `types[]` filtering.
- SDK SSE decoder and Managed Agents stream-event union parsing for session event payloads.
- `client.beta.sessions.resources.add/retrieve/update/list/delete`.
- `client.beta.sessions.threads.list/retrieve/archive`.
- `client.beta.sessions.threads.events.list`.
- `client.beta.files.upload/retrieve_metadata/list/download/delete`, including session `scope_id` filtering.
- `client.beta.skills.create/retrieve/list/delete`.
- `client.beta.skills.versions.create/retrieve/list/download/delete`.
- Agent skill refs using official `custom` and `anthropic` skill union shapes.
- Skill multipart uploads for both create paths, using the official SDK's `display_title` and `files` request shape.
- Official-compatible epoch-microsecond skill version identifiers.
- `client.beta.vaults.create/retrieve/update/list/archive/delete`.
- `client.beta.vaults.credentials.create/retrieve/update/list/archive/delete/mcp_oauth_validate`, including `mcp_oauth`, `static_bearer`, and `environment_variable` auth unions, refresh secret redaction, and environment-variable partial auth updates.
- `client.beta.memory_stores.create/retrieve/update/list/archive/delete`.
- `client.beta.memory_stores.memories.create/retrieve/update/list/delete`, including update no-op/precondition idempotency, delete `expected_content_sha256`, and `depth` rollups as `memory_prefix` items.
- `client.beta.memory_stores.memory_versions.retrieve/list/redact`, including `api_key_id`, `session_id`, `operation`, `view` list filters, and deleted-version null content/hash/size semantics.
- `client.beta.deployments.create/retrieve/update/list/archive/pause/unpause/run`, including deployment `system.message` initial-event ordering and local `resources`/`vault_ids` limit coverage.
- `client.beta.deployment_runs.retrieve/list`.
- `client.beta.user_profiles.create/retrieve/update/list/create_enrollment_url`, including `external` and `resold` relationships.
- SDK `next_page` cursor pagination for agents, sessions, skills, credentials, memories, and user profiles.
- SDK `after_id` and `before_id` pagination for files.
- Representative list filters and sort options: `include_archived`, `source`, `scope_id`, session `agent_id`/`agent_version`/`statuses`, deployment `agent_id`/`status` and `status` vs `include_archived` validation, memory `path_prefix`/`depth`, memory path validation, `order/order_by`, `deployment_id`, memory-version `api_key_id`/`session_id`/`operation`/`view`, `trigger_type`, and `has_error` success/error semantics.
- Invalid page cursor handling, expired page cursor handling, invalid file ID cursor handling, timestamp alias filtering, and SDK route-specific high-limit clamping for agents and agent versions.

Important remaining coverage gaps:

- Exhaustive pagination edge cases across every route group.
- Full filter semantics for every list endpoint, especially less common filters.
- Production runtime semantics behind the validated response shapes.
- End-to-end infinite HTTP SSE consumption through ASGITransport. The contract suite validates the SDK decoder/union parser, while route-level stream behavior is covered by local API tests.

Reference sources:

- https://github.com/anthropics/anthropic-sdk-python
- https://github.com/anthropics/anthropic-sdk-python/blob/main/api.md
- https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python
