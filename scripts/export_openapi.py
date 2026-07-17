from __future__ import annotations

import argparse
import json
import os
import re
from pprint import pformat
from pathlib import Path
from typing import Any

from app.auth import VOTRIX_MANAGED_AGENTS_BETA
from app.public_surface import public_ga_openapi
from votrix_managed_agents import create_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "website" / "public" / "openapi" / "vma.json"
DEFAULT_SERVER_URL = "https://managed-agents.votrixai.com"
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}

TAG_DESCRIPTIONS = {
    "agents": ("Agents", "Agent definitions and immutable agent versions."),
    "api keys": (
        "API keys",
        "Tenant-scoped credentials, permissions, expiration, rotation, and revocation.",
    ),
    "environments": (
        "Environments",
        "Execution environment configuration and lifecycle.",
    ),
    "environment work": (
        "Environment work",
        "Polling, claiming, and coordinating work assigned to environments.",
    ),
    "sessions": (
        "Sessions",
        "Long-running session lifecycle and outcomes.",
    ),
    "session events": (
        "Session events",
        "Sending, listing, and streaming events for active sessions.",
    ),
    "session resources": (
        "Session resources",
        "Resources attached to a long-running session.",
    ),
    "session threads": (
        "Session threads",
        "Threads, thread events, and thread streams within a session.",
    ),
    "files": ("Files", "Session and Organization file resources."),
    "skills": ("Skills", "Reusable skill archives and versioned skill definitions."),
    "usage": (
        "Usage",
        "Organization-scoped raw usage facts with Session and time filters.",
    ),
    "vaults": (
        "Vaults & credentials",
        "Credential vaults, secrets, and OAuth validation.",
    ),
    "memory": (
        "Memory",
        "Memory stores, records, immutable versions, and redaction.",
    ),
    "deployments": (
        "Deployments",
        "Deployment definitions, runs, and operational controls.",
    ),
    "user profiles": (
        "User profiles",
        "Managed user profiles and relationship metadata.",
    ),
    "health": ("Health", "Service and database readiness endpoints."),
}


SPECIAL_OPERATION_DESCRIPTIONS = {
    ("post", "/v1/api_keys"): (
        "Creates a tenant-scoped API key and returns its plaintext secret exactly once. "
        "Persist the secret immediately because later responses expose only its safe prefix."
    ),
    ("post", "/v1/api_keys/{key_id}/rotate"): (
        "Atomically creates a replacement with the same permissions and revokes the old key. "
        "The replacement plaintext is returned exactly once."
    ),
    ("post", "/v1/api_keys/{key_id}/revoke"): (
        "Revokes an API key immediately and records the requesting actor and optional audit reason."
    ),
    ("post", "/v1/agents"): (
        "Creates a versioned agent definition in the current Organization. "
        "The returned agent is ready to be referenced by a session or deployment."
    ),
    ("post", "/v1/environments"): (
        "Creates an execution environment that sessions can use for sandboxed work. "
        "The configuration determines whether execution is cloud-hosted, local, or self-hosted."
    ),
    ("post", "/v1/sessions"): (
        "Creates a durable session for an agent and execution environment. "
        "Send events to the returned session to start or continue agent work."
    ),
    ("post", "/v1/sessions/{session_id}/events"): (
        "Appends one or more input events to a session and returns the durable event records. "
        "Use the event stream to observe subsequent agent output."
    ),
    ("get", "/v1/usage"): (
        "Returns append-only raw usage facts for the current Organization. "
        "Filter by Session, metric, or occurrence time without inferred billing identity."
    ),
    ("post", "/v1/deployments"): (
        "Creates a reusable deployment definition for running an agent manually or on a schedule. "
        "The response includes the normalized schedule and current deployment state."
    ),
    ("post", "/v1/deployments/{deployment_id}/run"): (
        "Queues a run for the requested deployment and returns the resulting deployment run. "
        "Manual runs may create a linked session immediately."
    ),
    ("get", "/v1/sessions/{session_id}/events/stream"): (
        "Streams durable and requested preview events for a session as server-sent events. "
        "Resume after a known event by using `after_seq` or the `Last-Event-ID` header."
    ),
    ("get", "/v1/sessions/{session_id}/stream"): (
        "Compatibility alias for the session server-sent event stream. "
        "Resume after a known event by using `after_seq` or the `Last-Event-ID` header."
    ),
    ("get", "/v1/sessions/{session_id}/threads/{thread_id}/stream"): (
        "Streams events for one session thread as server-sent events. "
        "Resume after a known event by using `after_seq` or the `Last-Event-ID` header."
    ),
}


ACTION_DESCRIPTION_TEMPLATES = {
    "ack": "Acknowledges the requested {subject} and returns its latest state.",
    "add": "Adds {subject} to the requested parent resource and returns the created resource.",
    "archive": "Archives the requested {subject} while retaining its history and returns the archived state.",
    "cancel": "Requests cancellation of the specified {subject} and returns its latest state.",
    "complete": "Completes {subject} and returns the resulting resource metadata.",
    "create": "Creates a new {subject} in the current Organization and returns the created resource.",
    "delete": "Deletes the requested {subject} and returns a deletion confirmation.",
    "download": "Downloads the content for the requested {subject}.",
    "get": "Returns the requested {subject} from the current Organization.",
    "heartbeat": "Renews the lease for the requested {subject} and returns its latest state.",
    "list": "Returns a paginated list of {subject} visible to the current Organization.",
    "pause": "Pauses the requested {subject} and returns its latest state.",
    "poll": "Waits for available {subject} and returns the work item that was leased to the worker.",
    "presign": "Creates a temporary upload target for {subject} and returns the required upload details.",
    "redact": "Redacts sensitive content from the requested {subject} and returns its latest state.",
    "resume": "Resumes the requested {subject} and returns its latest state.",
    "retrieve": "Returns the requested {subject} from the current Organization.",
    "run": "Starts the requested {subject} and returns the resulting run state.",
    "send": "Appends {subject} and returns the durable records accepted by the service.",
    "stop": "Stops the requested {subject} and returns its latest state.",
    "stream": "Streams {subject} as server-sent events and supports resuming from an event cursor.",
    "unpause": "Unpauses the requested {subject} and returns its latest state.",
    "update": "Updates the requested {subject} and returns its latest representation.",
    "upload": "Uploads {subject} and returns the stored resource metadata.",
    "validate": "Validates the requested {subject} and returns the validation result.",
}


PARAMETER_DESCRIPTIONS = {
    "limit": "Maximum number of records to return in this page.",
    "page": "Opaque pagination cursor returned as `next_page` by the previous request.",
    "include_archived": "Whether archived resources should be included in the result.",
    "created_at[gt]": "Only return resources created after this RFC 3339 timestamp.",
    "created_at[gte]": "Only return resources created at or after this RFC 3339 timestamp.",
    "created_at[lt]": "Only return resources created before this RFC 3339 timestamp.",
    "created_at[lte]": "Only return resources created at or before this RFC 3339 timestamp.",
    "occurred_at[gt]": "Only return usage recorded after this RFC 3339 timestamp.",
    "occurred_at[gte]": "Only return usage recorded at or after this RFC 3339 timestamp.",
    "occurred_at[lt]": "Only return usage recorded before this RFC 3339 timestamp.",
    "occurred_at[lte]": "Only return usage recorded at or before this RFC 3339 timestamp.",
    "order": "Sort direction for the returned records: `asc` or `desc`.",
    "order_by": "Resource field used to order the returned records.",
    "view": "Response detail level. Memory endpoints accept `basic` or `full`.",
    "worker_id": "Stable identifier for the worker polling or updating environment work.",
    "lease_seconds": "Number of seconds for which a polled work item is leased to the worker.",
    "block_ms": "Maximum number of milliseconds to wait for work before returning an empty result.",
    "reclaim_older_than_ms": "Reclaim work whose lease heartbeat is older than this many milliseconds.",
    "desired_ttl_seconds": "Requested number of seconds to keep the work lease active.",
    "expected_last_heartbeat": "Last heartbeat timestamp expected by this conditional work update.",
    "agent_version": "Only return or run the specified immutable agent version.",
    "statuses": "Only return resources whose status is in this list.",
    "statuses[]": "Bracket-array form of the status filter.",
    "types": "Only return events whose type is in this list.",
    "types[]": "Bracket-array form of the event type filter.",
    "after_id": "Only return records after this resource identifier.",
    "before_id": "Only return records before this resource identifier.",
    "after_seq": "Only return or stream events with a sequence number greater than this value.",
    "event_deltas": "Preview event types to include before their durable event records are available.",
    "event_deltas[]": "Bracket-array form of the preview event type selection.",
    "source": "Only return resources created by this source.",
    "path": "Exact normalized memory path to retrieve or filter by.",
    "path_prefix": "Normalized memory path prefix used to filter descendants.",
    "depth": "Maximum descendant depth to include below the requested memory path.",
    "expected_content_sha256": "Expected current SHA-256 digest used as a conditional write or delete precondition.",
    "operation": "Only return memory versions created by this operation: `created`, `modified`, or `deleted`.",
    "status": "Only return resources with this status.",
    "has_error": "Whether to return only deployment runs that have, or do not have, an error.",
    "trigger_type": "Only return deployment runs created by this trigger type: `manual` or `schedule`.",
    "version": "Version number of the resource addressed by this request.",
    "Last-Event-ID": "Last received event sequence. The stream resumes after this event.",
    "Anthropic-Worker-ID": "Compatibility header containing the stable identifier of a self-hosted worker.",
    "votrix-managed-agents-beta": (
        "Required Votrix Managed Agents preview selector. "
        f"Use `{VOTRIX_MANAGED_AGENTS_BETA}`."
    ),
}


PARAMETER_EXAMPLES = {
    "limit": 20,
    "created_at[gt]": "2026-07-01T00:00:00Z",
    "created_at[gte]": "2026-07-01T00:00:00Z",
    "created_at[lt]": "2026-08-01T00:00:00Z",
    "created_at[lte]": "2026-08-01T00:00:00Z",
    "occurred_at[gt]": "2026-07-01T00:00:00Z",
    "occurred_at[gte]": "2026-07-01T00:00:00Z",
    "occurred_at[lt]": "2026-08-01T00:00:00Z",
    "occurred_at[lte]": "2026-08-01T00:00:00Z",
    "order": "desc",
    "view": "full",
    "worker_id": "worker-primary",
    "lease_seconds": 60,
    "block_ms": 500,
    "after_seq": 42,
    "event_deltas": ["agent.message"],
    "event_deltas[]": ["agent.message"],
    "types": ["user.message", "agent.message"],
    "types[]": ["user.message", "agent.message"],
    "status": "active",
    "trigger_type": "manual",
    "version": 1,
    "Last-Event-ID": "42",
    "Anthropic-Worker-ID": "worker-primary",
    "votrix-managed-agents-beta": VOTRIX_MANAGED_AGENTS_BETA,
}


IDENTIFIER_EXAMPLES = {
    "agent_id": "agt_1234567890abcdef1234567890abcdef",
    "api_key_id": "key_1234567890abcdef1234567890abcdef",
    "credential_id": "cred_1234567890abcdef1234567890abcdef",
    "deployment_id": "deploy_1234567890abcdef1234567890abcdef",
    "deployment_run_id": "deprun_1234567890abcdef1234567890abcdef",
    "environment_id": "env_1234567890abcdef1234567890abcdef",
    "file_id": "file_1234567890abcdef1234567890abcdef",
    "memory_id": "mem_1234567890abcdef1234567890abcdef",
    "memory_store_id": "memstore_1234567890abcdef1234567890abcdef",
    "memory_version_id": "memver_1234567890abcdef1234567890abcdef",
    "resource_id": "sesrsc_1234567890abcdef1234567890abcdef",
    "session_id": "sess_1234567890abcdef1234567890abcdef",
    "skill_id": "skill_1234567890abcdef1234567890abcdef",
    "thread_id": "thread_1234567890abcdef1234567890abcdef",
    "user_profile_id": "uprof_1234567890abcdef1234567890abcdef",
    "vault_id": "vault_1234567890abcdef1234567890abcdef",
    "work_id": "work_1234567890abcdef1234567890abcdef",
}


COMMON_PROPERTY_DESCRIPTIONS = {
    "id": "Unique identifier for this resource.",
    "type": "Stable type discriminator for this object.",
    "name": "Human-readable name for the resource.",
    "description": "Optional human-readable description.",
    "metadata": "Caller-defined metadata stored with the resource.",
    "version": "Version number of this resource.",
    "created_at": "RFC 3339 timestamp when the resource was created.",
    "updated_at": "RFC 3339 timestamp when the resource was last updated.",
    "archived_at": "RFC 3339 timestamp when the resource was archived, or null when active.",
    "deleted_at": "RFC 3339 timestamp when the resource was deleted, or null when present.",
    "data": "Resources or event records returned by this operation.",
    "has_more": "Whether another page of results is available.",
    "first_id": "Identifier of the first item in this page.",
    "last_id": "Identifier of the last item in this page.",
    "next_page": "Opaque cursor used to request the next page, or null at the end.",
    "deleted": "Whether the requested resource was deleted successfully.",
}


DOMAIN_PROPERTY_DESCRIPTIONS = {
    "model": "Model identifier and provider-specific model configuration used by the agent.",
    "system": "System instructions applied when the agent runs.",
    "tools": "Tool definitions exposed to the agent.",
    "mcp_servers": "MCP server connections available to the agent.",
    "skills": "Versioned skills made available to the agent.",
    "multiagent": "Optional coordinator and subagent configuration.",
    "runtime": "Provider-specific runtime configuration for the agent.",
    "config": "Execution environment configuration returned by the service.",
    "scope": "Ownership scope for this environment.",
    "agent": "Agent reference and resolved agent configuration used by the session.",
    "agent_id": "Identifier of the agent definition used by this resource.",
    "agent_version": "Immutable agent version used by this resource.",
    "environment_id": "Identifier of the execution environment used by this resource.",
    "title": "Optional display title for the resource.",
    "status": "Current lifecycle status of the resource.",
    "status_details": "Structured details associated with the current lifecycle status.",
    "stop_reason": "Structured reason the session stopped, or null while it can continue.",
    "run_state": "Latest durable runtime state reported for the session.",
    "sandbox_state": "Latest known sandbox lifecycle state for the session.",
    "resources": "Files or memory stores mounted into the session.",
    "outcome_evaluations": "Outcome evaluations recorded for the session.",
    "stats": "Aggregate execution statistics for the session.",
    "usage": "Aggregate model and runtime usage for the session.",
    "vault_ids": "Credential vault identifiers available to the session.",
    "last_event_seq": "Highest durable event sequence recorded for the session.",
    "deployment_id": "Identifier of the deployment that created this resource, when applicable.",
    "session_id": "Identifier of the session associated with this object.",
    "processed_at": "RFC 3339 timestamp when the event was processed, or null while pending.",
    "events": "Ordered input events to append to the session.",
    "path": "Normalized logical path for a memory record.",
    "content": "Content stored in or carried by this resource.",
    "actor": "Identifier of the actor responsible for this change.",
    "updated_by": "Identifier of the actor that last updated this resource.",
    "file": "Binary file content uploaded as multipart form data.",
    "key": "Storage key returned by the presign operation.",
    "filename": "Original filename presented to API consumers.",
    "mime_type": "Media type of the stored file content.",
    "size_bytes": "File size in bytes.",
    "sha256": "Lowercase hexadecimal SHA-256 digest of the file content.",
    "namespace": "Logical storage namespace used to organize uploaded files.",
    "expires_in": "Number of seconds before the temporary upload target expires.",
    "content_sha256": "Expected SHA-256 digest used by the memory precondition.",
    "precondition": "Conditional write requirement evaluated against the current memory.",
    "if_version": "Expected current memory version for a conditional update.",
    "expected_version": "Compatibility alias for the expected current memory version.",
    "seq": "Monotonically increasing event sequence within the session.",
    "detail": "Validation details reported for the failed request.",
    "loc": "Path to the field that failed validation.",
    "msg": "Human-readable validation failure message.",
    "input": "Input value that failed validation, when safe to include.",
    "ctx": "Additional structured validation context.",
    "error": "Machine-readable details for this API error.",
}


PROPERTY_DESCRIPTIONS = {
    ("AgentCreateRequest", "model"): "Model identifier or provider-specific model configuration used by the agent.",
    ("AgentCreateRequest", "system"): "System instructions applied to every run of this agent.",
    ("AgentCreateRequest", "tools"): "Tool definitions exposed to the agent.",
    ("AgentCreateRequest", "mcp_servers"): "MCP server connections available to the agent.",
    ("AgentCreateRequest", "skills"): "Versioned skills made available to the agent.",
    ("AgentCreateRequest", "multiagent"): "Optional coordinator and subagent configuration.",
    ("AgentCreateRequest", "runtime"): "Provider-specific runtime configuration for the agent.",
    ("AgentReference", "id"): "Identifier of the agent definition to run.",
    ("AgentReference", "version"): "Specific agent version to run; omit to use the current version.",
    ("EnvironmentCreateRequest", "config"): "Execution configuration, including environment type, resources, networking, and sandbox options.",
    ("EnvironmentCreateRequest", "scope"): "Ownership scope for the environment: `organization` or `account`.",
    ("SessionCreateRequest", "agent"): "Agent identifier or structured agent reference used by the session.",
    ("SessionCreateRequest", "environment_id"): "Identifier of the execution environment used by the session.",
    ("SessionCreateRequest", "title"): "Optional display title for the session.",
    ("SessionCreateRequest", "resources"): "Files or memory stores mounted into the session at creation time.",
    ("SessionCreateRequest", "vault_ids"): "Credential vault identifiers made available to the session.",
    ("SessionCreateRequest", "funding"): "Optional create-time funding selection; omission uses the Organization default.",
    ("SessionResponse", "status"): "Current lifecycle status of the session.",
    ("SessionResponse", "last_event_seq"): "Highest durable event sequence recorded for the session.",
    ("SendEventsRequest", "events"): "Ordered input events to append to the session.",
    ("SessionEventInput", "type"): "Event type discriminator, such as `user.message` or `user.interrupt`.",
    ("UsageEntryResponse", "organization_id"): "Identifier of the Organization that owns this usage fact.",
    ("UsageEntryResponse", "metric"): "Exact raw metric recorded by the control plane.",
    ("UsageEntryResponse", "quantity"): "Non-negative quantity recorded for this metric.",
    ("UsageEntryResponse", "unit"): "Unit associated with the recorded quantity.",
    ("UsageEntryResponse", "provider"): "Model or runtime provider that reported this usage, when available.",
    ("UsageEntryResponse", "model"): "Concrete provider model associated with this usage, when available.",
    ("UsageEntryResponse", "source_type"): "Control-plane resource type that produced this usage fact.",
    ("UsageEntryResponse", "source_id"): "Identifier of the control-plane resource that produced this usage fact.",
    ("UsageEntryResponse", "dimensions"): "Provider-reported raw usage dimensions such as input and output tokens.",
    ("UsageEntryResponse", "data"): "Control-plane accounting metadata for this raw usage fact.",
    ("UsageEntryResponse", "occurred_at"): "RFC 3339 timestamp when this usage occurred.",
}


AGENT_ID = IDENTIFIER_EXAMPLES["agent_id"]
ENVIRONMENT_ID = IDENTIFIER_EXAMPLES["environment_id"]
SESSION_ID = IDENTIFIER_EXAMPLES["session_id"]
DEPLOYMENT_ID = IDENTIFIER_EXAMPLES["deployment_id"]
EXAMPLE_TIMESTAMP = "2026-07-13T16:00:00Z"

AGENT_EXAMPLE = {
    "id": AGENT_ID,
    "type": "agent",
    "name": "Research assistant",
    "version": 1,
    "model": {"id": "gpt-5.5"},
    "system": "Research the request and cite the sources you use.",
    "description": "A research agent for account questions.",
    "tools": [],
    "mcp_servers": [],
    "skills": [],
    "multiagent": None,
    "metadata": {"team": "support"},
    "archived_at": None,
    "created_at": EXAMPLE_TIMESTAMP,
    "updated_at": EXAMPLE_TIMESTAMP,
}

ENVIRONMENT_EXAMPLE = {
    "id": ENVIRONMENT_ID,
    "type": "environment",
    "name": "production-cloud",
    "description": "Cloud sandbox for production agent sessions.",
    "config": {"type": "cloud"},
    "metadata": {"region": "us-east"},
    "scope": "organization",
    "archived_at": None,
    "deleted_at": None,
    "created_at": EXAMPLE_TIMESTAMP,
    "updated_at": EXAMPLE_TIMESTAMP,
}

SESSION_EXAMPLE = {
    "id": SESSION_ID,
    "type": "session",
    "agent": {"type": "agent", "id": AGENT_ID, "version": 1},
    "agent_id": AGENT_ID,
    "agent_version": 1,
    "environment_id": ENVIRONMENT_ID,
    "title": "Investigate an account request",
    "status": "idle",
    "status_details": {},
    "stop_reason": None,
    "run_state": None,
    "sandbox_state": None,
    "metadata": {"account_id": "acct_example"},
    "resources": [],
    "outcome_evaluations": [],
    "stats": {},
    "usage": {},
    "vault_ids": [],
    "last_event_seq": 0,
    "archived_at": None,
    "deleted_at": None,
    "deployment_id": None,
    "created_at": EXAMPLE_TIMESTAMP,
    "updated_at": EXAMPLE_TIMESTAMP,
}

DEPLOYMENT_EXAMPLE = {
    "id": DEPLOYMENT_ID,
    "type": "deployment",
    "name": "Daily account report",
    "agent": {"id": AGENT_ID, "version": 1},
    "environment_id": ENVIRONMENT_ID,
    "initial_events": [{"type": "user.message", "content": "Generate the daily report."}],
    "schedule": {
        "type": "cron",
        "cron": "0 9 * * 1-5",
        "timezone": "America/New_York",
        "enabled": True,
        "upcoming_runs_at": ["2026-07-14T13:00:00Z"],
    },
    "status": "active",
    "created_at": EXAMPLE_TIMESTAMP,
    "updated_at": EXAMPLE_TIMESTAMP,
    "archived_at": None,
}


def list_response_example(item: dict[str, Any]) -> dict[str, Any]:
    resource_id = str(item["id"])
    return {
        "data": [item],
        "has_more": False,
        "first_id": resource_id,
        "last_id": resource_id,
        "next_page": None,
    }


READ_OPERATION_EXAMPLES = {
    ("get", "/v1/agents"): list_response_example(AGENT_EXAMPLE),
    ("get", "/v1/agents/{agent_id}"): AGENT_EXAMPLE,
    ("get", "/v1/agents/{agent_id}/versions"): list_response_example(
        AGENT_EXAMPLE
    ),
    ("get", "/v1/environments"): list_response_example(ENVIRONMENT_EXAMPLE),
    ("get", "/v1/environments/{environment_id}"): ENVIRONMENT_EXAMPLE,
    ("get", "/v1/sessions"): list_response_example(SESSION_EXAMPLE),
    ("get", "/v1/sessions/{session_id}"): SESSION_EXAMPLE,
    ("get", "/v1/deployments"): list_response_example(DEPLOYMENT_EXAMPLE),
    ("get", "/v1/deployments/{deployment_id}"): DEPLOYMENT_EXAMPLE,
}

OPERATION_EXAMPLES = {
    ("post", "/v1/agents"): {
        "request": {
            "name": "Research assistant",
            "model": {"id": "gpt-5.5"},
            "system": "Research the request and cite the sources you use.",
            "description": "A research agent for account questions.",
            "metadata": {"team": "support"},
        },
        "status": "201",
        "response": AGENT_EXAMPLE,
    },
    ("patch", "/v1/agents/{agent_id}"): {
        "request": {"version": 1, "system": "Answer concisely and cite every external source."},
        "status": "200",
        "response": {**AGENT_EXAMPLE, "version": 2, "system": "Answer concisely and cite every external source."},
    },
    ("post", "/v1/agents/{agent_id}"): {
        "request": {"version": 1, "system": "Answer concisely and cite every external source."},
        "status": "200",
        "response": {**AGENT_EXAMPLE, "version": 2, "system": "Answer concisely and cite every external source."},
    },
    ("post", "/v1/environments"): {
        "request": {
            "name": "production-cloud",
            "description": "Cloud sandbox for production agent sessions.",
            "config": {"type": "cloud"},
            "metadata": {"region": "us-east"},
            "scope": "organization",
        },
        "status": "201",
        "response": ENVIRONMENT_EXAMPLE,
    },
    ("patch", "/v1/environments/{environment_id}"): {
        "request": {"description": "Cloud sandbox for support automation.", "metadata": {"region": "us-east"}},
        "status": "200",
        "response": {**ENVIRONMENT_EXAMPLE, "description": "Cloud sandbox for support automation."},
    },
    ("post", "/v1/environments/{environment_id}"): {
        "request": {"description": "Cloud sandbox for support automation.", "metadata": {"region": "us-east"}},
        "status": "200",
        "response": {**ENVIRONMENT_EXAMPLE, "description": "Cloud sandbox for support automation."},
    },
    ("post", "/v1/sessions"): {
        "request": {
            "agent": {"type": "agent", "id": AGENT_ID, "version": 1},
            "environment_id": ENVIRONMENT_ID,
            "title": "Investigate an account request",
            "metadata": {"account_id": "acct_example"},
        },
        "status": "201",
        "response": SESSION_EXAMPLE,
    },
    ("patch", "/v1/sessions/{session_id}"): {
        "request": {"title": "Investigate the Acme request", "metadata": {"priority": "high"}},
        "status": "200",
        "response": {
            **SESSION_EXAMPLE,
            "title": "Investigate the Acme request",
            "metadata": {"priority": "high"},
        },
    },
    ("post", "/v1/sessions/{session_id}"): {
        "request": {"title": "Investigate the Acme request", "metadata": {"priority": "high"}},
        "status": "200",
        "response": {
            **SESSION_EXAMPLE,
            "title": "Investigate the Acme request",
            "metadata": {"priority": "high"},
        },
    },
    ("post", "/v1/sessions/{session_id}/events"): {
        "request": {"events": [{"type": "user.message", "content": "Summarize the Acme account."}]},
        "status": "200",
        "response": {
            "data": [
                {
                    "id": "evt_1234567890abcdef1234567890abcdef",
                    "type": "user.message",
                    "session_id": SESSION_ID,
                    "seq": 1,
                    "content": "Summarize the Acme account.",
                    "created_at": EXAMPLE_TIMESTAMP,
                    "processed_at": None,
                }
            ]
        },
    },
    ("post", "/v1/deployments"): {
        "request": {
            "name": "Daily account report",
            "agent": {"id": AGENT_ID, "version": 1},
            "environment_id": ENVIRONMENT_ID,
            "initial_events": [{"type": "user.message", "content": "Generate the daily report."}],
            "schedule": {
                "type": "cron",
                "cron": "0 9 * * 1-5",
                "timezone": "America/New_York",
            },
        },
        "status": "201",
        "response": DEPLOYMENT_EXAMPLE,
    },
    ("post", "/v1/deployments/{deployment_id}"): {
        "request": {"name": "Weekday account report"},
        "status": "200",
        "response": {**DEPLOYMENT_EXAMPLE, "name": "Weekday account report"},
    },
    ("post", "/v1/deployments/{deployment_id}/run"): {
        "request": {"trigger": "manual", "title": "Run the report now"},
        "status": "200",
        "response": {
            "id": "deprun_1234567890abcdef1234567890abcdef",
            "type": "deployment_run",
            "deployment_id": DEPLOYMENT_ID,
            "agent": {"id": AGENT_ID, "version": 1},
            "status": "queued",
            "attempt": 1,
            "trigger": "manual",
            "trigger_context": {},
            "scheduled_for": None,
            "session_id": SESSION_ID,
            "created_at": EXAMPLE_TIMESTAMP,
            "updated_at": EXAMPLE_TIMESTAMP,
            "archived_at": None,
        },
    },
}


SSE_EXAMPLE = (
    "id: 42\n"
    "event: agent.message\n"
    'data: {"id":"evt_1234567890abcdef1234567890abcdef","type":"agent.message",'
    f'"session_id":"{SESSION_ID}","seq":42,"content":"The report is ready.",'
    f'"created_at":"{EXAMPLE_TIMESTAMP}"}}\n\n'
)


def documentation_tag(path: str, fallback: str) -> str:
    """Split broad runtime tags into navigation-sized documentation groups."""
    if path.startswith("/v1/environments/") and "/work" in path:
        return "environment work"
    if path.startswith("/v1/sessions/"):
        if "/threads" in path:
            return "session threads"
        if "/resources" in path:
            return "session resources"
        if "/events" in path or path.endswith("/stream"):
            return "session events"
    if path.startswith("/v1/vaults"):
        return "vaults"
    if path.startswith("/v1/memory_stores"):
        return "memory"
    if path.startswith(("/v1/deployments", "/v1/deployment_runs")):
        return "deployments"
    if path.startswith("/v1/user_profiles"):
        return "user profiles"
    return fallback


def fallback_operation_description(
    *, method: str, path: str, operation: dict[str, Any]
) -> str:
    special = SPECIAL_OPERATION_DESCRIPTIONS.get((method.lower(), path))
    if special:
        return special

    summary = str(operation.get("summary") or "").strip()
    if not summary:
        return (
            f"Performs the `{method.upper()}` operation for `{path}` within "
            "the current Organization."
        )

    action, _, remainder = summary.partition(" ")
    action = action.lower()
    subject = (remainder or summary).lower()
    if action == "health":
        return "Checks service readiness and returns the current health state."

    template = ACTION_DESCRIPTION_TEMPLATES.get(action)
    if template:
        return template.format(subject=subject)

    if method.lower() == "get":
        return (
            f"Returns {summary.lower()} for resources visible to the current Organization."
        )
    return (
        f"Performs {summary.lower()} within the current Organization and returns "
        "the resulting resource or operation state."
    )


def parameter_description(parameter: dict[str, Any]) -> str:
    name = str(parameter.get("name") or "parameter")
    location = str(parameter.get("in") or "request")
    known = PARAMETER_DESCRIPTIONS.get(name)
    if known:
        return known

    label = name.removesuffix("[]").replace("_", " ").replace("-", " ")
    if location == "path":
        if name.endswith("_id"):
            resource = name.removesuffix("_id").replace("_", " ")
            return f"Unique identifier of the {resource} addressed by this request."
        return f"Path value identifying the requested {label}."
    if location == "query":
        if name.endswith("_id"):
            resource = name.removesuffix("_id").replace("_", " ")
            return f"Only return records associated with this {resource} identifier."
        return f"Value used by the `{name}` filter or request control."
    if location == "header":
        return f"Value supplied in the `{name}` request header for this operation."
    return f"Value supplied for the `{name}` {location} parameter."


def parameter_example(parameter: dict[str, Any]) -> Any | None:
    name = str(parameter.get("name") or "")
    if name in PARAMETER_EXAMPLES:
        return PARAMETER_EXAMPLES[name]
    return IDENTIFIER_EXAMPLES.get(name)


def enrich_parameter(parameter: dict[str, Any]) -> None:
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        # Pydantic repeats the parameter name as a schema title. Fumadocs then
        # renders that title where users expect the primitive type.
        schema.pop("title", None)

    description = str(parameter.get("description") or "").strip()
    if not description:
        parameter["description"] = parameter_description(parameter)

    example = parameter_example(parameter)
    if example is not None and "example" not in parameter and "examples" not in parameter:
        parameter["example"] = example


def enrich_component_schemas(components: dict[str, Any]) -> None:
    schemas = components.setdefault("schemas", {})
    for schema_name, component_schema in schemas.items():
        if not isinstance(component_schema, dict):
            continue
        for property_name, property_schema in component_schema.get(
            "properties", {}
        ).items():
            if not isinstance(property_schema, dict):
                continue
            # The field name is already the visible label; removing the
            # generated title lets Fumadocs show `string`, `integer`, etc.
            property_schema.pop("title", None)
            if str(property_schema.get("description") or "").strip():
                continue
            description = (
                PROPERTY_DESCRIPTIONS.get((schema_name, property_name))
                or DOMAIN_PROPERTY_DESCRIPTIONS.get(property_name)
                or COMMON_PROPERTY_DESCRIPTIONS.get(property_name)
            )
            if description:
                property_schema["description"] = description


def install_error_schemas(components: dict[str, Any]) -> None:
    schemas = components.setdefault("schemas", {})
    schemas["ApiError"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "message"],
        "description": "Machine-readable error details.",
        "properties": {
            "type": {
                "type": "string",
                "description": "Stable category for programmatic error handling.",
                "example": "invalid_request_error",
            },
            "message": {
                "type": "string",
                "description": "Human-readable explanation of the failed request.",
                "example": "Request body validation failed.",
            },
        },
    }
    schemas["ApiErrorResponse"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "error"],
        "description": "Error envelope returned by the Votrix Managed Agents API.",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["error"],
                "description": "Top-level error response discriminator.",
                "example": "error",
            },
            "error": {"$ref": "#/components/schemas/ApiError"},
        },
    }


def error_response(
    *, description: str, error_type: str, message: str
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ApiErrorResponse"},
                "example": {
                    "type": "error",
                    "error": {"type": error_type, "message": message},
                },
            }
        },
    }


def enrich_error_responses(operation: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    responses.setdefault(
        "400",
        error_response(
            description="The request is malformed or is missing a required preview header.",
            error_type="invalid_request_error",
            message="The request is missing a required header or contains an invalid value.",
        ),
    )
    responses.setdefault(
        "401",
        error_response(
            description="The Organization API key is missing or invalid.",
            error_type="authentication_error",
            message="Invalid API key.",
        ),
    )
    if "422" in responses:
        responses["422"] = error_response(
            description="The request parameters or body failed validation.",
            error_type="invalid_request_error",
            message="Request body validation failed.",
        )


def enrich_sse_response(*, method: str, path: str, operation: dict[str, Any]) -> None:
    if (
        method.lower() != "get"
        or not path.startswith("/v1/sessions/")
        or not path.endswith("/stream")
    ):
        return

    operation.setdefault("responses", {})["200"] = {
        "description": (
            "A continuous server-sent event stream. Each frame contains an event "
            "sequence, event type, and JSON data payload."
        ),
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
                "example": SSE_EXAMPLE,
            }
        },
    }


def enrich_binary_and_multipart_operations(
    *, method: str, path: str, operation: dict[str, Any]
) -> None:
    method = method.lower()

    if method == "post" and path in {
        "/v1/skills",
        "/v1/skills/{skill_id}/versions",
    }:
        operation["requestBody"] = {
            "required": True,
            "description": (
                "Upload one or more files under a shared top-level directory. "
                "The directory must contain a root `SKILL.md` file with `name` "
                "and `description` frontmatter."
            ),
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "display_title": {
                                "type": "string",
                                "description": "Human-readable title for the skill.",
                            },
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "description": (
                                    "Skill files. Every path must share one top-level "
                                    "directory and include its root `SKILL.md`."
                                ),
                                "items": {
                                    "type": "string",
                                    "format": "binary",
                                },
                            },
                        },
                    }
                },
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "display_title": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["filename", "content"],
                                    "properties": {
                                        "filename": {"type": "string"},
                                        "content": {"type": "string"},
                                        "mime_type": {"type": "string"},
                                    },
                                },
                            },
                        },
                    }
                },
            },
        }

    if method == "get" and path in {
        "/v1/files/{file_id}/content",
        "/v1/skills/{skill_id}/versions/{version}/content",
    }:
        operation.setdefault("responses", {})["200"] = {
            "description": "Binary content returned as a downloadable response.",
            "headers": {
                "Cache-Control": {
                    "description": "Prevents shared or browser caches from retaining private content.",
                    "schema": {"type": "string", "example": "private, no-store"},
                },
                "Content-Disposition": {
                    "description": "Suggested filename for the downloaded content.",
                    "schema": {"type": "string"},
                },
                "X-Content-Type-Options": {
                    "description": "Disables MIME type sniffing for downloaded content.",
                    "schema": {"type": "string", "example": "nosniff"},
                },
            },
            "content": {
                "application/octet-stream": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                }
            },
        }


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def example_path(path: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(IDENTIFIER_EXAMPLES.get(name, PARAMETER_EXAMPLES.get(name, name)))

    return re.sub(r"\{([^}]+)\}", replacement, path)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_code_samples(
    *, server_url: str, method: str, path: str, request_example: Any
) -> list[dict[str, str]]:
    url = server_url.rstrip("/") + example_path(path)
    method_upper = method.upper()
    compact_json = json.dumps(request_example, ensure_ascii=False, separators=(",", ":"))
    pretty_json = json.dumps(request_example, ensure_ascii=False, indent=2)
    python_payload = pformat(request_example, sort_dicts=False, width=88)

    curl_source = "\n".join(
        [
            f"curl --request {method_upper} \\",
            f"  --url {shell_quote(url)} \\",
            '  --header "Content-Type: application/json" \\',
            '  --header "x-api-key: $VOTRIX_API_KEY" \\',
            (
                "  --header \"votrix-managed-agents-beta: "
                f"{VOTRIX_MANAGED_AGENTS_BETA}\" \\"
            ),
            f"  --data {shell_quote(compact_json)}",
        ]
    )
    python_source = (
        "import os\n\n"
        "import requests\n\n"
        f"payload = {python_payload}\n\n"
        "response = requests.request(\n"
        f"    {method_upper!r},\n"
        f"    {url!r},\n"
        "    headers={\n"
        "        'Content-Type': 'application/json',\n"
        "        'x-api-key': os.environ['VOTRIX_API_KEY'],\n"
        f"        'votrix-managed-agents-beta': {VOTRIX_MANAGED_AGENTS_BETA!r},\n"
        "    },\n"
        "    json=payload,\n"
        ")\n"
        "response.raise_for_status()\n"
        "print(response.json())"
    )
    javascript_source = (
        f"const payload = {pretty_json};\n\n"
        f"const response = await fetch({json.dumps(url)}, {{\n"
        f"  method: {json.dumps(method_upper)},\n"
        "  headers: {\n"
        '    "Content-Type": "application/json",\n'
        '    "x-api-key": process.env.VOTRIX_API_KEY,\n'
        f'    "votrix-managed-agents-beta": {json.dumps(VOTRIX_MANAGED_AGENTS_BETA)}\n'
        "  },\n"
        "  body: JSON.stringify(payload)\n"
        "});\n\n"
        "if (!response.ok) throw new Error(await response.text());\n"
        "console.log(await response.json());"
    )
    return [
        {"id": "curl", "lang": "bash", "label": "cURL", "source": curl_source},
        {
            "id": "python",
            "lang": "python",
            "label": "Python",
            "source": python_source,
        },
        {
            "id": "js",
            "lang": "javascript",
            "label": "JavaScript",
            "source": javascript_source,
        },
    ]


def enrich_operation_examples(
    *,
    server_url: str,
    method: str,
    path: str,
    operation: dict[str, Any],
) -> None:
    example = OPERATION_EXAMPLES.get((method.lower(), path))
    if not example:
        return

    request_example = example["request"]
    request_body = operation.get("requestBody", {})
    request_content = request_body.get("content", {})
    request_media = request_content.get("application/json")
    if isinstance(request_media, dict):
        request_media.setdefault("example", json_copy(request_example))

    response = operation.get("responses", {}).get(str(example["status"]))
    if isinstance(response, dict):
        response_media = response.setdefault("content", {}).setdefault(
            "application/json", {}
        )
        response_media.setdefault("example", json_copy(example["response"]))

    operation.setdefault(
        "x-codeSamples",
        build_code_samples(
            server_url=server_url,
            method=method,
            path=path,
            request_example=request_example,
        ),
    )


def enrich_read_operation_examples(
    *, method: str, path: str, operation: dict[str, Any]
) -> None:
    example = READ_OPERATION_EXAMPLES.get((method.lower(), path))
    if example is None:
        return

    response = operation.get("responses", {}).get("200")
    if not isinstance(response, dict):
        return

    response_media = response.setdefault("content", {}).setdefault(
        "application/json", {}
    )
    response_media.setdefault("example", json_copy(example))


def build_documentation_schema(*, server_url: str) -> dict[str, Any]:
    # The hosted service exposes only the public-GA route allowlist. Keep the
    # committed explorer independent from a developer's local .env so deferred
    # compatibility routes cannot leak back into published documentation.
    schema = public_ga_openapi(create_app().openapi())
    schema["servers"] = [
        {
            "url": server_url.rstrip("/"),
            "description": "Votrix Managed Agents API",
        }
    ]
    schema["tags"] = [
        {
            "name": name,
            "description": description,
            "x-displayName": display_name,
        }
        for name, (display_name, description) in TAG_DESCRIPTIONS.items()
    ]

    info = schema.setdefault("info", {})
    info["description"] = (
        "Build and operate long-running agents through a durable, "
        "Organization-scoped REST API. This reference is generated from the same "
        "FastAPI application that serves production traffic, so request fields "
        "and response models stay aligned with the code.\n\n"
        f"> **Base URL** `{server_url.rstrip('/')}`\n\n"
        "Every `/v1` request requires the current "
        f"`votrix-managed-agents-beta: {VOTRIX_MANAGED_AGENTS_BETA}` header plus "
        "either an `x-api-key` header or the same key as a Bearer token. Choose "
        "an endpoint to inspect its schema, copy a code sample, or send a request."
    )
    info.pop("license", None)

    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.update(
        {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "x-api-key",
                "description": (
                    "Organization API key. Treat this value as a server-side secret "
                    "and do not expose it in client-side application code."
                ),
            },
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Organization API key sent as a Bearer token in the "
                    "`Authorization` header."
                ),
            },
        }
    )
    install_error_schemas(components)
    enrich_component_schemas(components)
    file_upload_schema = components.get("schemas", {}).get(
        "Body_upload_file_v1_files_post"
    )
    if isinstance(file_upload_schema, dict):
        file_property = file_upload_schema.get("properties", {}).get("file")
        if isinstance(file_property, dict):
            # Fumadocs and many OpenAPI 3.0 tools detect file controls from
            # `format: binary`; keep the 3.1 contentMediaType emitted by FastAPI.
            file_property["format"] = "binary"

    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            fallback_tag = next(iter(operation.get("tags", [])), "managed resources")
            operation["tags"] = [documentation_tag(path, fallback_tag)]

            description = str(operation.get("description") or "").strip()
            summary = str(operation.get("summary") or "").strip()
            if not description or description == summary:
                operation["description"] = fallback_operation_description(
                    method=method,
                    path=path,
                    operation=operation,
                )

            parameters = operation.get("parameters", [])
            operation["parameters"] = [
                parameter
                for parameter in parameters
                if not (
                    parameter.get("in") == "header"
                    and parameter.get("name") in {"authorization", "x-api-key"}
                )
            ]
            operation["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]

            for parameter in operation["parameters"]:
                if (
                    parameter.get("in") == "header"
                    and parameter.get("name") == "votrix-managed-agents-beta"
                ):
                    parameter["required"] = True
                    parameter.setdefault("schema", {})["default"] = (
                        VOTRIX_MANAGED_AGENTS_BETA
                    )
                enrich_parameter(parameter)

            enrich_sse_response(method=method, path=path, operation=operation)
            enrich_binary_and_multipart_operations(
                method=method,
                path=path,
                operation=operation,
            )
            enrich_error_responses(operation)
            enrich_operation_examples(
                server_url=server_url,
                method=method,
                path=path,
                operation=operation,
            )
            enrich_read_operation_examples(
                method=method,
                path=path,
                operation=operation,
            )

    return schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the FastAPI OpenAPI schema used by Fumadocs."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("VMA_OPENAPI_SERVER_URL", DEFAULT_SERVER_URL),
        help="API origin used by the interactive request explorer.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    schema = build_documentation_schema(server_url=args.server_url)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(schema.get('paths', {}))} paths to {output}")


if __name__ == "__main__":
    main()
