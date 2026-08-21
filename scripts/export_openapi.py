"""Export the active FastAPI OpenAPI document used by the docs website.

The application owns every request and response schema.  This exporter only
adds presentation metadata that is useful to Fumadocs; it must not maintain a
second route allowlist or replace generated schemas with hand-written copies.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

from app.server import create_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "website" / "public" / "openapi" / "vma.json"
DEFAULT_SERVER_URL = "https://api.vma.votrixai.com"
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}

TAG_DESCRIPTIONS = {
    "organizations": (
        "Organizations",
        "Manage Organization details and members.",
    ),
    "accounts": (
        "Accounts",
        "Create separate usage and spending boundaries for Agent work.",
    ),
    "agents": (
        "Agents",
        "Create reusable agents and manage changes to their instructions and capabilities.",
    ),
    "environments": (
        "Environments",
        "Choose the sandbox setup available to an Agent.",
    ),
    "files": (
        "Files",
        "Upload source files and download files created during a Session.",
    ),
    "memory-stores": (
        "Memory Stores",
        "Keep shared knowledge available across Sessions.",
    ),
    "models": ("Models", "See the models available to Agents."),
    "sessions": (
        "Sessions",
        "Start work, continue it later, and follow an Agent's progress.",
    ),
    "skills": ("Skills", "Give Agents reusable guidance for specific kinds of work."),
}

PARAMETER_DESCRIPTIONS = {
    "x-api-key": (
        "Your VMA API key. It identifies the Organization this request can access."
    ),
    "Last-Event-ID": (
        "Last received Session event sequence. The SSE stream resumes after it."
    ),
    "after_id": "Return records after this resource identifier.",
    "after_seq": "Return Session events with a sequence greater than this value.",
    "api_key_id": "Only return Memory Versions attributed to this API-key handle.",
    "before_id": "Return records before this resource identifier.",
    "created_at[gte]": "Return records created at or after this RFC 3339 timestamp.",
    "created_at[lte]": "Return records created at or before this RFC 3339 timestamp.",
    "depth": "Maximum descendant depth below the requested Memory path prefix.",
    "expected_content_sha256": (
        "Expected current SHA-256 digest used as the Memory delete precondition."
    ),
    "include_archived": "Include archived resources in the result.",
    "limit": "Maximum number of records returned in this page.",
    "memory_id": "Only return Versions belonging to this Memory.",
    "operation": "Only return Versions for this operation.",
    "page": "Opaque pagination cursor returned by the previous page.",
    "path_prefix": "Slash-terminated Memory path prefix used to filter descendants.",
    "scope_id": "Only return Files belonging to this scope.",
    "session_id": "Only return Memory Versions attributed to this Session.",
    "status": "Only return resources with this status.",
    "view": "Choose the basic metadata view or the full content view.",
}

ACCOUNT_ID_EXAMPLE = "acct_1234567890abcdef1234567890abcdef"
AGENT_ID_EXAMPLE = "agent_1234567890abcdef1234567890abcdef"
ENVIRONMENT_ID_EXAMPLE = "env_1234567890abcdef1234567890abcdef"
EVENT_ID_EXAMPLE = "evt_1234567890abcdef1234567890abcdef"
FILE_ID_EXAMPLE = "file_1234567890abcdef1234567890abcdef"
OUTPUT_FILE_ID_EXAMPLE = "file_2234567890abcdef1234567890abcdef"
MEMORY_STORE_ID_EXAMPLE = "memstore_1234567890abcdef1234567890abcdef"
ORGANIZATION_ID_EXAMPLE = "org_1234567890abcdef1234567890abcdef"
SESSION_ID_EXAMPLE = "sess_1234567890abcdef1234567890abcdef"
SKILL_ID_EXAMPLE = "skill_1234567890abcdef1234567890abcdef"

CREATED_AT_EXAMPLE = "2026-08-13T14:30:00Z"
UPDATED_AT_EXAMPLE = "2026-08-13T14:32:00Z"
ARCHIVED_AT_EXAMPLE = "2026-08-13T15:00:00Z"
AGENT_VERSION_ID_EXAMPLE = "av_1234567890abcdef1234567890abcdef"
SESSION_FILE_ID_EXAMPLE = "sfile_1234567890abcdef1234567890abcdef"
SESSION_MEMORY_ID_EXAMPLE = "sesrsc_1234567890abcdef1234567890abcdef"

PARAMETER_EXAMPLES = {
    "x-api-key": "vma_example_key",
    "Last-Event-ID": "42",
    "account_id": ACCOUNT_ID_EXAMPLE,
    "agent_id": AGENT_ID_EXAMPLE,
    "after_seq": 42,
    "api_key_id": "apikey_1234567890abcdef1234567890abcdef",
    "backend": "openai",
    "created_at[gte]": "2026-07-01T00:00:00Z",
    "created_at[lte]": "2026-08-01T00:00:00Z",
    "depth": 1,
    "environment_id": ENVIRONMENT_ID_EXAMPLE,
    "event_id": EVENT_ID_EXAMPLE,
    "file_id": FILE_ID_EXAMPLE,
    "limit": 20,
    "memory_id": "mem_1234567890abcdef1234567890abcdef",
    "memory_store_id": MEMORY_STORE_ID_EXAMPLE,
    "memory_version_id": "memver_1234567890abcdef1234567890abcdef",
    "operation": "modified",
    "page": "page_example_cursor",
    "path_prefix": "/projects/",
    "session_id": SESSION_ID_EXAMPLE,
    "skill_id": SKILL_ID_EXAMPLE,
    "view": "full",
}

ACCOUNT_ACTIVE_EXAMPLE = {
    "id": ACCOUNT_ID_EXAMPLE,
    "type": "account",
    "organization_id": ORGANIZATION_ID_EXAMPLE,
    "name": "Website Builder",
    "status": "active",
    "is_default": False,
    "limit_usd": "20.00",
    "funding": {"type": "platform", "backend": "openrouter"},
}
ACCOUNT_SUSPENDED_EXAMPLE = {
    **ACCOUNT_ACTIVE_EXAMPLE,
    "status": "suspended",
}
ACCOUNT_BYOK_EXAMPLE = {
    "id": ACCOUNT_ID_EXAMPLE,
    "type": "account",
    "organization_id": ORGANIZATION_ID_EXAMPLE,
    "name": "Direct Models",
    "status": "active",
    "is_default": False,
    "limit_usd": None,
    "funding": {
        "type": "byok",
        "credentials": [
            {"backend": "anthropic"},
            {"backend": "openai"},
        ],
    },
}
ACCOUNT_USAGE_EXAMPLE = {
    "account_id": ACCOUNT_ID_EXAMPLE,
    "type": "account_usage",
    "funding": {"type": "platform", "backend": "openrouter"},
    "usage_usd": "8.40",
    "usage_daily_usd": "0.75",
    "usage_weekly_usd": "3.10",
    "usage_monthly_usd": "8.40",
    "limit_usd": "20.00",
    "limit_remaining_usd": "11.60",
    "observed_usage": {
        "input_tokens": 12500,
        "output_tokens": 3100,
        "total_tokens": 15600,
    },
}
AGENT_ACTIVE_EXAMPLE = {
    "id": AGENT_ID_EXAMPLE,
    "type": "agent",
    "name": "Research Assistant",
    "version": 1,
    "model": {"id": "claude-sonnet-5"},
    "system": (
        "Research the topic carefully and save the final brief in "
        "outputs/brief.md."
    ),
    "description": "Creates concise, source-backed research briefs.",
    "tools": [],
    "mcp_servers": [],
    "skills": [],
    "multiagent": None,
    "metadata": {"team": "research"},
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": CREATED_AT_EXAMPLE,
    "archived_at": None,
}
AGENT_VERSION_EXAMPLE = {
    "id": AGENT_VERSION_ID_EXAMPLE,
    "type": "agent_version",
    "agent_id": AGENT_ID_EXAMPLE,
    "version": 1,
    "name": "Research Assistant",
    "model": {"id": "claude-sonnet-5"},
    "system": AGENT_ACTIVE_EXAMPLE["system"],
    "description": AGENT_ACTIVE_EXAMPLE["description"],
    "tools": [],
    "mcp_servers": [],
    "skills": [],
    "multiagent": None,
    "metadata": {"team": "research"},
    "runtime": {},
    "created_at": CREATED_AT_EXAMPLE,
}
AGENT_LIST_EXAMPLE = {
    "data": [AGENT_ACTIVE_EXAMPLE],
    "has_more": False,
    "first_id": AGENT_ID_EXAMPLE,
    "last_id": AGENT_ID_EXAMPLE,
}
AGENT_VERSION_LIST_EXAMPLE = {
    "data": [AGENT_VERSION_EXAMPLE],
    "has_more": False,
    "first_id": AGENT_VERSION_EXAMPLE["id"],
    "last_id": AGENT_VERSION_EXAMPLE["id"],
}

AGENT_UPDATED_EXAMPLE = {
    **AGENT_ACTIVE_EXAMPLE,
    "version": 2,
    "system": (
        "Research the topic carefully, cite sources, and save the final brief "
        "in outputs/brief.md."
    ),
    "metadata": {"team": "research", "reviewed": True},
    "updated_at": UPDATED_AT_EXAMPLE,
}
AGENT_ARCHIVED_EXAMPLE = {
    **AGENT_ACTIVE_EXAMPLE,
    "updated_at": ARCHIVED_AT_EXAMPLE,
    "archived_at": ARCHIVED_AT_EXAMPLE,
}

ENVIRONMENT_READY_EXAMPLE = {
    "id": ENVIRONMENT_ID_EXAMPLE,
    "type": "environment",
    "name": "Data Analysis Workspace",
    "description": "A sandbox with common data-analysis packages.",
    "config": {
        "packages": {
            "apt": [],
            "cargo": [],
            "gem": [],
            "go": [],
            "npm": [],
            "pip": ["pandas==2.2.3", "openpyxl==3.1.5"],
        },
        "cpu": 2,
        "memory_mb": 2048,
    },
    "build_state": "ready",
    "build_error": None,
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": UPDATED_AT_EXAMPLE,
    "archived_at": None,
}
ENVIRONMENT_BUILDING_EXAMPLE = {
    **ENVIRONMENT_READY_EXAMPLE,
    "build_state": "building",
    "updated_at": CREATED_AT_EXAMPLE,
}
ENVIRONMENT_UPDATED_EXAMPLE = {
    **ENVIRONMENT_BUILDING_EXAMPLE,
    "description": "A larger sandbox for data-analysis workloads.",
    "config": {
        **ENVIRONMENT_READY_EXAMPLE["config"],
        "cpu": 4,
        "memory_mb": 4096,
    },
    "updated_at": UPDATED_AT_EXAMPLE,
}
ENVIRONMENT_ARCHIVED_EXAMPLE = {
    **ENVIRONMENT_READY_EXAMPLE,
    "updated_at": ARCHIVED_AT_EXAMPLE,
    "archived_at": ARCHIVED_AT_EXAMPLE,
}

UPLOADED_FILE_EXAMPLE = {
    "id": FILE_ID_EXAMPLE,
    "type": "file",
    "filename": "source-material.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 184320,
    "sha256": "90b30e2de3a3d4f11f59bc4863f4d80fba129b5f650f85d9d60b24287d6fef27",
    "scope": None,
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": CREATED_AT_EXAMPLE,
}
OUTPUT_FILE_EXAMPLE = {
    **UPLOADED_FILE_EXAMPLE,
    "id": OUTPUT_FILE_ID_EXAMPLE,
    "filename": "brief.pdf",
    "size_bytes": 96214,
    "sha256": "f5c4a9b8db604b511a4b284b2405584b4cc919bb626a23deed4045b35f2f1c78",
    "scope": {"type": "session", "id": SESSION_ID_EXAMPLE},
    "updated_at": UPDATED_AT_EXAMPLE,
}

SKILL_ACTIVE_EXAMPLE = {
    "id": SKILL_ID_EXAMPLE,
    "type": "skill",
    "name": "research-brief",
    "description": "Guidance for producing concise, source-backed briefs.",
    "size_bytes": 4812,
    "sha256": "55e500545e0d269b3f775901c2df98b916972d7208936f2e6ff47f1c6cb477d6",
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": UPDATED_AT_EXAMPLE,
    "archived_at": None,
}

MEMORY_STORE_ACTIVE_EXAMPLE = {
    "id": MEMORY_STORE_ID_EXAMPLE,
    "type": "memory_store",
    "name": "Content Creator",
    "description": "Durable brand and project context.",
    "metadata": {"team": "creative", "region": "global"},
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": UPDATED_AT_EXAMPLE,
    "archived_at": None,
}
MEMORY_STORE_UPDATED_EXAMPLE = {
    **MEMORY_STORE_ACTIVE_EXAMPLE,
    "description": "Durable brand, asset, and active-project context.",
    "metadata": {"team": "content", "region": "global"},
}
MEMORY_STORE_ARCHIVED_EXAMPLE = {
    **MEMORY_STORE_ACTIVE_EXAMPLE,
    "updated_at": ARCHIVED_AT_EXAMPLE,
    "archived_at": ARCHIVED_AT_EXAMPLE,
}

SESSION_FILE_RESOURCE_EXAMPLE = {
    "id": SESSION_FILE_ID_EXAMPLE,
    "type": "file",
    "file_id": FILE_ID_EXAMPLE,
    "mount_path": "/home/user/uploads/source-material.pdf",
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": CREATED_AT_EXAMPLE,
}
SESSION_MEMORY_RESOURCE_EXAMPLE = {
    "id": SESSION_MEMORY_ID_EXAMPLE,
    "type": "memory_store",
    "memory_store_id": MEMORY_STORE_ID_EXAMPLE,
    "access": "read_write",
    "instructions": "Use the approved voice and current project context.",
    "mount_path": "/mnt/memory/content-creator",
    "name": "Content Creator",
    "description": "Durable brand and project context.",
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": CREATED_AT_EXAMPLE,
}
SESSION_NEW_EXAMPLE = {
    "id": SESSION_ID_EXAMPLE,
    "type": "session",
    "agent_id": AGENT_ID_EXAMPLE,
    "agent_version": 1,
    "environment_id": ENVIRONMENT_ID_EXAMPLE,
    "model": None,
    "account_id": ACCOUNT_ID_EXAMPLE,
    "title": "Q3 market research brief",
    "status": "idle",
    "stop_reason": None,
    "last_event_seq": 0,
    "resources": [],
    "created_at": CREATED_AT_EXAMPLE,
    "updated_at": CREATED_AT_EXAMPLE,
    "archived_at": None,
}
SESSION_ACTIVE_EXAMPLE = {
    **SESSION_NEW_EXAMPLE,
    "stop_reason": {"type": "end_turn"},
    "last_event_seq": 4,
    "resources": [SESSION_FILE_RESOURCE_EXAMPLE, SESSION_MEMORY_RESOURCE_EXAMPLE],
    "updated_at": UPDATED_AT_EXAMPLE,
}
SESSION_UPDATED_EXAMPLE = {
    **SESSION_ACTIVE_EXAMPLE,
    "title": "Q3 market research brief — revised",
}
SESSION_ARCHIVED_EXAMPLE = {
    **SESSION_ACTIVE_EXAMPLE,
    "updated_at": ARCHIVED_AT_EXAMPLE,
    "archived_at": ARCHIVED_AT_EXAMPLE,
}

USER_MESSAGE_EVENT_EXAMPLE = {
    "id": "evt_1234567890abcdef1234567890abc001",
    "seq": 1,
    "processed_at": "2026-08-13T14:31:00Z",
    "type": "user.message",
    "content": [
        {
            "type": "text",
            "text": "Write a short brief on the future of vertical AI agents.",
        }
    ],
}
SESSION_RUNNING_EVENT_EXAMPLE = {
    "id": "evt_1234567890abcdef1234567890abc002",
    "seq": 2,
    "processed_at": "2026-08-13T14:31:01Z",
    "type": "session.status_running",
}
AGENT_MESSAGE_EVENT_EXAMPLE = {
    "id": "evt_1234567890abcdef1234567890abc003",
    "seq": 3,
    "processed_at": "2026-08-13T14:31:42Z",
    "type": "agent.message",
    "content": [
        {
            "type": "text",
            "text": "The completed brief is ready in outputs/brief.md.",
        }
    ],
}
SESSION_IDLE_EVENT_EXAMPLE = {
    "id": "evt_1234567890abcdef1234567890abc004",
    "seq": 4,
    "processed_at": "2026-08-13T14:31:43Z",
    "type": "session.status_idle",
    "stop_reason": {"type": "end_turn"},
}
EVENT_PAGE_EXAMPLE = {
    "data": [
        USER_MESSAGE_EVENT_EXAMPLE,
        SESSION_RUNNING_EVENT_EXAMPLE,
        AGENT_MESSAGE_EVENT_EXAMPLE,
        SESSION_IDLE_EVENT_EXAMPLE,
    ],
    "has_more": False,
    "first_id": USER_MESSAGE_EVENT_EXAMPLE["id"],
    "last_id": SESSION_IDLE_EVENT_EXAMPLE["id"],
    "last_event_seq": 4,
}

VALIDATION_ERROR_EXAMPLE = {
    "detail": [
        {
            "loc": ["body", "name"],
            "msg": "Field required",
            "type": "missing",
        }
    ]
}

REQUEST_COMPONENT_EXAMPLES = {
    "AccountCreateRequest": {
        "name": "Website Builder",
        "limit_usd": "20.00",
        "idempotency_key": "create-website-builder-account",
        "funding": {"type": "platform", "backend": "openrouter"},
    },
    "ByokModelCredentialSetRequest": {
        "api_key": "YOUR_OPENAI_API_KEY",
    },
    "AgentCreateRequest": {
        "name": "Research Assistant",
        "model": "claude-sonnet-5",
        "system": (
            "Research the topic carefully and save the final brief in "
            "outputs/brief.md."
        ),
        "description": "Creates concise, source-backed research briefs.",
        "metadata": {"team": "research"},
    },
    "AgentUpdateRequest": {
        "system": (
            "Research the topic carefully, cite sources, and save the final "
            "brief in outputs/brief.md."
        ),
        "metadata": {"team": "research", "reviewed": True},
    },
    "EnvironmentCreateRequest": {
        "name": "Data Analysis Workspace",
        "description": "A sandbox with common data-analysis packages.",
        "config": {
            "packages": {"pip": ["pandas==2.2.3", "openpyxl==3.1.5"]},
            "cpu": 2,
            "memory_mb": 2048,
        },
    },
    "EnvironmentUpdateRequest": {
        "description": "A larger sandbox for data-analysis workloads.",
        "config": {
            "packages": {"pip": ["pandas==2.2.3", "openpyxl==3.1.5"]},
            "cpu": 4,
            "memory_mb": 4096,
        },
    },
    "LiveFileRequest": {"path": "brief.pdf"},
    "LiveUploadRequest": {
        "file_id": FILE_ID_EXAMPLE,
        "path": "source-material.pdf",
    },
    "MemoryStoreCreateRequest": {
        "name": "Content Creator",
        "description": "Durable brand and project context.",
        "metadata": {"team": "creative"},
    },
    "MemoryStoreUpdateRequest": {
        "description": "Durable brand, asset, and active-project context.",
        "metadata": {"team": "content"},
    },
    "SendEventsRequest": {
        "events": [
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Write a short brief on the future of vertical AI "
                            "agents."
                        ),
                    }
                ],
            }
        ]
    },
    "SessionCreateRequest": {
        "agent_id": AGENT_ID_EXAMPLE,
        "environment_id": ENVIRONMENT_ID_EXAMPLE,
        "account_id": ACCOUNT_ID_EXAMPLE,
        "title": "Q3 market research brief",
    },
    "SessionUpdateRequest": {"title": "Q3 market research brief — revised"},
}

# Response examples belong to the response Media Type Object, not inside a
# reusable JSON Schema. Some browser-side schema resolvers interpret an `id`
# key anywhere below a Schema Object as a schema URI. Embedding API payloads
# such as {"model": {"id": "claude-sonnet-5"}} in component schemas can then
# produce a duplicate-schema-URI crash when the example is repeated.
RESPONSE_COMPONENT_EXAMPLES = {
    "AccountResponse": ACCOUNT_ACTIVE_EXAMPLE,
    "AccountUsageResponse": ACCOUNT_USAGE_EXAMPLE,
    "ListResponse_AccountResponse_": {
        "data": [ACCOUNT_ACTIVE_EXAMPLE],
        "has_more": False,
        "first_id": ACCOUNT_ID_EXAMPLE,
        "last_id": ACCOUNT_ID_EXAMPLE,
    },
    "AgentResponse": AGENT_ACTIVE_EXAMPLE,
    "AgentVersionResponse": AGENT_VERSION_EXAMPLE,
    "ListResponse_AgentResponse_": AGENT_LIST_EXAMPLE,
    "ListResponse_AgentVersionResponse_": AGENT_VERSION_LIST_EXAMPLE,
    "EnvironmentResponse": ENVIRONMENT_READY_EXAMPLE,
    "ListResponse_EnvironmentResponse_": {
        "data": [ENVIRONMENT_READY_EXAMPLE],
        "has_more": False,
        "first_id": ENVIRONMENT_ID_EXAMPLE,
        "last_id": ENVIRONMENT_ID_EXAMPLE,
    },
    "FileResponse": UPLOADED_FILE_EXAMPLE,
    "ListResponse_FileResponse_": {
        "data": [UPLOADED_FILE_EXAMPLE, OUTPUT_FILE_EXAMPLE],
        "has_more": False,
        "first_id": FILE_ID_EXAMPLE,
        "last_id": OUTPUT_FILE_ID_EXAMPLE,
    },
    "SkillResponse": SKILL_ACTIVE_EXAMPLE,
    "ListResponse_SkillResponse_": {
        "data": [SKILL_ACTIVE_EXAMPLE],
        "has_more": False,
        "first_id": SKILL_ID_EXAMPLE,
        "last_id": SKILL_ID_EXAMPLE,
    },
    "MemoryStoreResponse": MEMORY_STORE_ACTIVE_EXAMPLE,
    "MemoryStoreListResponse": {
        "data": [MEMORY_STORE_ACTIVE_EXAMPLE],
        "next_page": None,
        "has_more": False,
        "first_id": MEMORY_STORE_ID_EXAMPLE,
        "last_id": MEMORY_STORE_ID_EXAMPLE,
    },
    "DeletedMemoryStoreResponse": {
        "id": MEMORY_STORE_ID_EXAMPLE,
        "type": "memory_store_deleted",
    },
    "SessionResponse": SESSION_ACTIVE_EXAMPLE,
    "ListResponse_SessionResponse_": {
        "data": [SESSION_ACTIVE_EXAMPLE],
        "has_more": False,
        "first_id": SESSION_ID_EXAMPLE,
        "last_id": SESSION_ID_EXAMPLE,
    },
    "SessionFileResourceResponse": SESSION_FILE_RESOURCE_EXAMPLE,
    "SessionMemoryStoreResourceResponse": SESSION_MEMORY_RESOURCE_EXAMPLE,
    "DeletedResponse": {"id": SESSION_ID_EXAMPLE, "deleted": True},
    "SendEventsResponse": {"data": [USER_MESSAGE_EVENT_EXAMPLE]},
    "ListEventsResponse": EVENT_PAGE_EXAMPLE,
    "UserMessageEvent": USER_MESSAGE_EVENT_EXAMPLE,
    "AgentMessageEvent": AGENT_MESSAGE_EVENT_EXAMPLE,
    "SessionStatusRunningEvent": SESSION_RUNNING_EVENT_EXAMPLE,
    "SessionStatusIdleEvent": SESSION_IDLE_EVENT_EXAMPLE,
    "HTTPValidationError": VALIDATION_ERROR_EXAMPLE,
}


# Router and model docstrings are written for maintainers and can discuss how
# the service is built. The documentation schema deliberately replaces them
# with this small, reviewed public-contract vocabulary.
ACCOUNT_GUIDE_REFERENCE = (
    "\n\nRead the [Accounts guide](/docs/accounts) for default Account behavior, "
    "Session assignment, usage, spending limits, BYOK key management, and "
    "suspension."
)


OPERATION_DESCRIPTIONS = {
    ("post", "/v1/accounts"): (
        "Create a separate usage and spending boundary inside your Organization. "
        "Use additional Accounts when work for different customers, teams, "
        "products, or environments should not be mixed together.\n\n"
        "An Account is uncapped unless `limit_usd` is supplied. A successful "
        "response has `status: \"active\"` and can be selected when creating a "
        "Session. Omit `funding` for a VMA-managed OpenRouter key, or choose "
        "`byok` to supply one key for each direct backend the Account should "
        "use. BYOK Accounts do not accept `limit_usd`.\n\n"
        "`idempotency_key` is optional and belongs in the JSON request body. "
        "Reuse the same value when retrying a successful create request to "
        "receive the original Account instead of creating another one."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("get", "/v1/accounts"): (
        "List the Accounts available to your API key. Accounts are returned "
        "oldest first, which normally places the Organization's default Account "
        "first.\n\n"
        "The response includes active and suspended Accounts. Use `is_default` "
        "to identify the Account selected when Session creation omits "
        "`account_id`, and use `status` to decide whether an Account can fund "
        "Agent work.\n\n"
        "Use `after_id` or `before_id` with the returned cursor fields to move "
        "through additional pages."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("get", "/v1/accounts/{account_id}"): (
        "Retrieve one Account by its public `acct_...` ID.\n\n"
        "`status` is `provisioning` while the Account is not ready, `active` "
        "when it can fund Agent work, or `suspended` when further work is "
        "blocked. `is_default` identifies the fallback used by Sessions that "
        "omit `account_id`. `limit_usd: null` means the Account has no "
        "Account-specific spending limit."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("put", "/v1/accounts/{account_id}/credentials/{backend}"): (
        "Add a direct-backend key to a BYOK Account or atomically replace the "
        "key already stored for that backend. The submitted key is validated "
        "before stored data changes, so a rejected key leaves the previous "
        "one available. Repeating the same PUT is idempotent.\n\n"
        "The Account keeps its current lifecycle state: changing a key on a "
        "suspended Account does not resume it. Platform Accounts reject this "
        "operation because their OpenRouter key is administered by VMA."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("delete", "/v1/accounts/{account_id}/credentials/{backend}"): (
        "Remove one direct-backend key from a BYOK Account. Its stored encrypted "
        "secret is deleted, while the Account and its historical observed usage "
        "remain available. At least one backend key must remain.\n\n"
        "Existing Sessions that select a model from the removed backend cannot "
        "make their next model call until a key is added again. Platform "
        "Accounts reject this operation because their OpenRouter key is "
        "administered by VMA."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("get", "/v1/accounts/{account_id}/usage"): (
        "Return a current usage snapshot for one Account. Usage for another "
        "Account is never included. `observed_usage` totals the normalized "
        "tokens recorded from completed calls.\n\n"
        "For Platform funding, `usage_usd` is cumulative and the daily, weekly, "
        "and monthly fields describe the current billing windows. For BYOK, "
        "those USD fields are `null` because VMA cannot reliably price activity "
        "outside this Account. When a spending limit is set, `limit_usd` and "
        "`limit_remaining_usd` describe it.\n\n"
        "Usage remains available while an Account is suspended. Treat this "
        "response as a snapshot at request time, not as a receipt for one "
        "individual Agent turn."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("post", "/v1/accounts/{account_id}/suspend"): (
        "Stop a non-default Account from funding further Agent work. The "
        "Account keeps the same ID, spending limit, and usage history, and its "
        "usage endpoint remains available.\n\n"
        "A suspended Account cannot be selected for a new Session. Existing "
        "Sessions remain assigned to it, but their next Agent work cannot "
        "continue until the Account is resumed. Suspending an already suspended "
        "Account returns that Account without changing it again.\n\n"
        "The Organization's default Account cannot be suspended because it is "
        "the fallback for every Session created without an explicit `account_id`."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("post", "/v1/accounts/{account_id}/resume"): (
        "Return a suspended Account to `active` so it can fund new and existing "
        "Sessions again.\n\n"
        "Resuming preserves the Account's ID, limit, and usage history. Existing "
        "Sessions stay assigned to the same Account; they do not need to be "
        "recreated. Resuming an already active Account returns it unchanged."
        + ACCOUNT_GUIDE_REFERENCE
    ),
    ("get", "/v1/agents/{agent_id}"): (
        "Retrieve the active Agent version, or a specific version selected by "
        "the `version` query parameter."
    ),
    ("patch", "/v1/agents/{agent_id}"): (
        "Change only the supplied Agent fields and return the resulting version."
    ),
    ("post", "/v1/agents/{agent_id}"): (
        "Change only the supplied Agent fields and return the resulting version."
    ),
    ("post", "/v1/sessions/{session_id}/events"): (
        "Add client events to a Session. A new message is refused with `409` "
        "while the Agent is working; an interrupt is accepted during a turn."
    ),
    ("get", "/v1/sessions/{session_id}/events"): (
        "List the Session's ordered event history. Use `after_seq` to request "
        "only events after a sequence already processed."
    ),
    ("get", "/v1/sessions/{session_id}/events/stream"): (
        "Follow Session events as server-sent events. Resume with `after_seq` "
        "or `Last-Event-ID`; without a cursor, the stream starts at the first "
        "event."
    ),
    ("post", "/v1/sessions/{session_id}/live/files"): (
        "Capture a file under `outputs/` as a File before the current turn "
        "finishes. The Session must have an available sandbox."
    ),
    ("post", "/v1/sessions/{session_id}/live/uploads"): (
        "Attach an existing File to an idle Session sandbox. `path` is relative "
        "to `uploads/` and defaults to the File's filename."
    ),
    ("post", "/v1/environments"): (
        "Create an Environment for Session sandboxes. Wait for `build_state` "
        "to become `ready` before starting a Session."
    ),
    ("post", "/v1/environments/{environment_id}"): (
        "Update an Environment. Existing Sessions keep the setup with which "
        "they started; later Sessions use the update."
    ),
    ("delete", "/v1/environments/{environment_id}"): (
        "Delete an Environment that is not referenced by a Session."
    ),
    ("post", "/v1/environments/{environment_id}/archive"): (
        "Archive an Environment so it cannot be selected by new Sessions."
    ),
    ("post", "/v1/files"): "Upload a File using `multipart/form-data`.",
    ("get", "/v1/files"): (
        "List Files, optionally limited to one Session with `scope_id`."
    ),
    ("get", "/v1/files/{file_id}"): (
        "Retrieve File metadata without downloading its content."
    ),
    ("get", "/v1/files/{file_id}/content"): (
        "Get a short-lived download URL for a File."
    ),
    ("post", "/v1/skills"): (
        "Upload a Skill package using `multipart/form-data`."
    ),
    ("post", "/v1/skills/{skill_id}"): (
        "Update Skill metadata and optionally replace its package."
    ),
}

OPERATION_SUCCESS_RESPONSES = {
    ("post", "/v1/accounts"): (
        "201",
        "The new Account, active and ready to be assigned to a Session.",
        ACCOUNT_ACTIVE_EXAMPLE,
    ),
    ("get", "/v1/accounts"): (
        "200",
        "A cursor page of Accounts ordered from oldest to newest.",
        {
            "data": [
                {
                    "id": "acct_default1234567890abcdef1234567890",
                    "type": "account",
                    "organization_id": ORGANIZATION_ID_EXAMPLE,
                    "name": "Default",
                    "status": "active",
                    "is_default": True,
                    "limit_usd": None,
                    "funding": {"type": "platform", "backend": "openrouter"},
                },
                ACCOUNT_SUSPENDED_EXAMPLE,
            ],
            "has_more": False,
            "first_id": "acct_default1234567890abcdef1234567890",
            "last_id": ACCOUNT_ID_EXAMPLE,
        },
    ),
    ("get", "/v1/accounts/{account_id}"): (
        "200",
        "The requested Account's public state and spending limit.",
        ACCOUNT_ACTIVE_EXAMPLE,
    ),
    ("put", "/v1/accounts/{account_id}/credentials/{backend}"): (
        "200",
        "The BYOK Account with the backend key added or replaced.",
        ACCOUNT_BYOK_EXAMPLE,
    ),
    ("delete", "/v1/accounts/{account_id}/credentials/{backend}"): (
        "200",
        "The BYOK Account after the selected backend key was removed.",
        {
            **ACCOUNT_BYOK_EXAMPLE,
            "funding": {
                "type": "byok",
                "credentials": [{"backend": "anthropic"}],
            },
        },
    ),
    ("get", "/v1/accounts/{account_id}/usage"): (
        "200",
        "A current USD usage snapshot for the requested Account.",
        ACCOUNT_USAGE_EXAMPLE,
    ),
    ("post", "/v1/accounts/{account_id}/suspend"): (
        "200",
        "The same Account with `status: \"suspended\"`.",
        ACCOUNT_SUSPENDED_EXAMPLE,
    ),
    ("post", "/v1/accounts/{account_id}/resume"): (
        "200",
        "The same Account with `status: \"active\"`.",
        ACCOUNT_ACTIVE_EXAMPLE,
    ),
}

# A component-level example is enough for most operations. These responses
# need a more precise lifecycle state or resource identifier than their shared
# response model can express on its own.
OPERATION_RESPONSE_EXAMPLES = {
    ("patch", "/v1/agents/{agent_id}", "200"): AGENT_UPDATED_EXAMPLE,
    ("post", "/v1/agents/{agent_id}", "200"): AGENT_UPDATED_EXAMPLE,
    ("post", "/v1/agents/{agent_id}/archive", "200"): AGENT_ARCHIVED_EXAMPLE,
    ("post", "/v1/sessions", "201"): SESSION_NEW_EXAMPLE,
    ("post", "/v1/sessions/{session_id}", "200"): SESSION_UPDATED_EXAMPLE,
    ("delete", "/v1/sessions/{session_id}", "200"): {
        "id": SESSION_ID_EXAMPLE,
        "deleted": True,
    },
    ("post", "/v1/sessions/{session_id}/archive", "200"): (
        SESSION_ARCHIVED_EXAMPLE
    ),
    (
        "get",
        "/v1/sessions/{session_id}/events/{event_id}",
        "200",
    ): AGENT_MESSAGE_EVENT_EXAMPLE,
    ("post", "/v1/sessions/{session_id}/live/files", "200"): (
        OUTPUT_FILE_EXAMPLE
    ),
    ("post", "/v1/sessions/{session_id}/live/uploads", "201"): (
        SESSION_FILE_RESOURCE_EXAMPLE
    ),
    ("post", "/v1/environments", "201"): ENVIRONMENT_BUILDING_EXAMPLE,
    ("post", "/v1/environments/{environment_id}", "200"): (
        ENVIRONMENT_UPDATED_EXAMPLE
    ),
    ("delete", "/v1/environments/{environment_id}", "200"): {
        "id": ENVIRONMENT_ID_EXAMPLE,
        "deleted": True,
    },
    ("post", "/v1/environments/{environment_id}/archive", "200"): (
        ENVIRONMENT_ARCHIVED_EXAMPLE
    ),
    ("delete", "/v1/files/{file_id}", "200"): {
        "id": FILE_ID_EXAMPLE,
        "deleted": True,
    },
    ("delete", "/v1/skills/{skill_id}", "200"): {
        "id": SKILL_ID_EXAMPLE,
        "deleted": True,
    },
    ("post", "/v1/memory_stores/{memory_store_id}", "200"): (
        MEMORY_STORE_UPDATED_EXAMPLE
    ),
    ("post", "/v1/memory_stores/{memory_store_id}/archive", "200"): (
        MEMORY_STORE_ARCHIVED_EXAMPLE
    ),
}

COMPONENT_DESCRIPTIONS = {
    "AccountCreateRequest": (
        "The public fields accepted when creating an additional Account."
    ),
    "AccountResponse": (
        "An Account's identity, readiness, default role, and optional spending limit."
    ),
    "AccountUsageResponse": (
        "A current billing and observed-token snapshot for one Account."
    ),
    "PlatformFundingRequest": "Select a VMA-managed OpenRouter key.",
    "ByokFundingRequest": "Supply your own keys for supported direct backends.",
    "ByokModelCredentialRequest": "One write-only direct-backend API key.",
    "ByokModelCredentialSetRequest": (
        "A write-only API key used to add or replace one direct backend."
    ),
    "PlatformFundingResponse": "Funding details for a VMA-managed Account.",
    "ByokFundingResponse": "Configured backends for a user-key Account.",
    "ByokModelCredentialResponse": "One configured direct backend.",
    "ObservedTokenUsage": "Normalized tokens recorded from completed model calls.",
    "ListResponse_AccountResponse_": (
        "A cursor page of Accounts ordered from oldest to newest."
    ),
    "EnvironmentConfig": "The sandbox settings shared by Sessions using this Environment.",
    "FileResource": (
        "A File attached when a Session is created. `path` is relative to "
        "`uploads/` and defaults to the File's filename."
    ),
    "ListEventsResponse": "One page of ordered Session events.",
    "LiveFileRequest": (
        "A file to capture from `outputs/` in an active Session sandbox."
    ),
    "LiveUploadRequest": (
        "An existing File to attach under `uploads/` in an idle Session sandbox."
    ),
    "MemoryStoreResource": (
        "A Memory Store attached when a Session is created."
    ),
    "ModelUsageEvent": (
        "The standardized token usage reported for one completed model call."
    ),
    "UserCustomToolResultInput": (
        "The result returned by your application for a custom tool request."
    ),
}

PROPERTY_DESCRIPTIONS = {
    ("AccountCreateRequest", "name"): "The Account name shown in API responses.",
    ("AccountCreateRequest", "limit_usd"): (
        "Optional spending limit in USD. Omit it for no Account-specific limit."
    ),
    ("AccountCreateRequest", "idempotency_key"): (
        "Optional retry key scoped to the Organization. Reuse it to receive the "
        "Account created by the first successful request."
    ),
    ("AccountCreateRequest", "funding"): (
        "Funding selection. Omit it for a VMA-managed OpenRouter key."
    ),
    ("AccountResponse", "id"): "Public Account identifier with the `acct_` prefix.",
    ("AccountResponse", "type"): "Resource type. Always `account`.",
    ("AccountResponse", "organization_id"): (
        "The Organization that owns this Account."
    ),
    ("AccountResponse", "name"): "The Account's display name.",
    ("AccountResponse", "status"): (
        "Whether the Account is `provisioning`, `active`, or `suspended`. Only "
        "an active Account can fund Agent work."
    ),
    ("AccountResponse", "is_default"): (
        "Whether Sessions use this Account when `account_id` is omitted."
    ),
    ("AccountResponse", "limit_usd"): (
        "The Account-specific spending limit in USD, or `null` when uncapped."
    ),
    ("AccountResponse", "funding"): (
        "Whether VMA or the Account owner supplies keys, and their backends."
    ),
    ("AccountUsageResponse", "account_id"): (
        "The Account whose usage is represented by this response."
    ),
    ("AccountUsageResponse", "type"): (
        "Resource type. Always `account_usage`."
    ),
    ("AccountUsageResponse", "funding"): (
        "Funding mode and configured backends used to interpret the usage fields."
    ),
    ("AccountUsageResponse", "usage_usd"): (
        "Cumulative usage in USD for Platform funding, or `null` for BYOK."
    ),
    ("AccountUsageResponse", "usage_daily_usd"): (
        "Current daily USD usage for Platform funding, or `null` for BYOK."
    ),
    ("AccountUsageResponse", "usage_weekly_usd"): (
        "Current weekly USD usage for Platform funding, or `null` for BYOK."
    ),
    ("AccountUsageResponse", "usage_monthly_usd"): (
        "Current monthly USD usage for Platform funding, or `null` for BYOK."
    ),
    ("AccountUsageResponse", "limit_usd"): (
        "The Account's spending limit in USD, or `null` when uncapped."
    ),
    ("AccountUsageResponse", "limit_remaining_usd"): (
        "The amount remaining under the Account's limit, or `null` when uncapped."
    ),
    ("AccountUsageResponse", "observed_usage"): (
        "Normalized token totals from completed calls recorded for this Account."
    ),
    ("PlatformFundingRequest", "type"): "Funding mode. Always `platform`.",
    ("PlatformFundingRequest", "backend"): "Inference backend. Always `openrouter`.",
    ("ByokFundingRequest", "type"): "Funding mode. Always `byok`.",
    ("ByokFundingRequest", "credentials"): (
        "One write-only API key per direct backend configured on this Account."
    ),
    ("ByokModelCredentialRequest", "backend"): (
        "Direct model backend that accepts this key."
    ),
    ("ByokModelCredentialRequest", "api_key"): (
        "Write-only API key for this backend. It is never returned."
    ),
    ("ByokModelCredentialSetRequest", "api_key"): (
        "Write-only API key for the backend in the URL. It is never returned."
    ),
    ("PlatformFundingResponse", "type"): "Funding mode. Always `platform`.",
    ("PlatformFundingResponse", "backend"): "Inference backend. Always `openrouter`.",
    ("ByokFundingResponse", "type"): "Funding mode. Always `byok`.",
    ("ByokFundingResponse", "credentials"): (
        "Direct backends configured on this Account; secret values are omitted."
    ),
    ("ByokModelCredentialResponse", "backend"): (
        "A direct model backend configured on this Account."
    ),
    ("ObservedTokenUsage", "input_tokens"): "Recorded input tokens.",
    ("ObservedTokenUsage", "output_tokens"): "Recorded output tokens.",
    ("ObservedTokenUsage", "total_tokens"): "Recorded total tokens.",
    ("ListResponse_AccountResponse_", "data"): (
        "Accounts in this page, ordered from oldest to newest."
    ),
    ("ListResponse_AccountResponse_", "has_more"): (
        "Whether another page is available in the requested direction."
    ),
    ("ListResponse_AccountResponse_", "first_id"): (
        "ID of the first Account in this page, or `null` for an empty page."
    ),
    ("ListResponse_AccountResponse_", "last_id"): (
        "ID of the last Account in this page, or `null` for an empty page."
    ),
    ("ModelUsageEvent", "model"): (
        "The public VMA model ID used for this call."
    ),
    ("ModelUsageEvent", "backend"): (
        "The inference backend that executed this call."
    ),
    ("ModelUsageEvent", "source"): (
        "Whether the Agent or read-image tool made this call; `legacy` marks "
        "an older event whose source was not recorded."
    ),
    ("ModelUsageEvent", "usage"): (
        "Token counts standardized by the active chat integration and preserved "
        "without repricing, estimation, or reconstructed totals."
    ),
    ("SessionCreateRequest", "account_id"): (
        "The Account assigned to this Session. Omit it to use the Organization's "
        "default Account."
    ),
    ("SessionCreateRequest", "model"): (
        "Optional model override for this Session. Omit it to use the selected "
        "Agent version's model."
    ),
    ("SessionResponse", "model"): (
        "The Session model override, or `null` when the Agent version supplies it."
    ),
}


def _parameter_description(parameter: dict[str, Any]) -> str:
    name = str(parameter.get("name") or "parameter")
    if parameter.get("in") == "path" and name.endswith("_id"):
        resource = name.removesuffix("_id").replace("_", " ")
        return f"Unique identifier of the {resource} addressed by this request."
    if name in PARAMETER_DESCRIPTIONS:
        return PARAMETER_DESCRIPTIONS[name]
    label = name.replace("_", " ").replace("-", " ")
    return f"Value supplied for the {label} {parameter.get('in', 'request')} parameter."


def _enrich_parameter(parameter: dict[str, Any]) -> None:
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        schema.pop("title", None)
    if not str(parameter.get("description") or "").strip():
        parameter["description"] = _parameter_description(parameter)
    name = str(parameter.get("name") or "")
    if name == "x-api-key":
        # Authentication rejects a missing key. The dependency accepts None
        # only so it can return the intended 401 response instead of a generic
        # validation error, so the public contract is still required/string.
        parameter["required"] = True
        parameter["schema"] = {"type": "string"}
    if name in PARAMETER_EXAMPLES and "example" not in parameter:
        parameter["example"] = PARAMETER_EXAMPLES[name]


def _remove_descriptions(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("description", None)
        for key, child in value.items():
            if key == "properties" and isinstance(child, dict):
                # `description` may be a real API field name. Only remove the
                # JSON Schema annotation from each property's schema; never
                # remove an entry from the properties map itself.
                for property_schema in child.values():
                    _remove_descriptions(property_schema)
            else:
                _remove_descriptions(child)
    elif isinstance(value, list):
        for child in value:
            _remove_descriptions(child)


def _enrich_component_schemas(schema: dict[str, Any]) -> None:
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, component in schemas.items():
        if not isinstance(component, dict):
            continue
        _remove_descriptions(component)
        if name in COMPONENT_DESCRIPTIONS:
            component["description"] = COMPONENT_DESCRIPTIONS[name]
        for property_name, property_schema in component.get("properties", {}).items():
            if isinstance(property_schema, dict):
                # The field name is already visible in Fumadocs. Repeating the
                # generated title hides the useful primitive/union type.
                property_schema.pop("title", None)
                description = PROPERTY_DESCRIPTIONS.get((name, property_name))
                if description is not None:
                    property_schema["description"] = description


def _component_example_for_schema(
    schema: dict[str, Any], examples: dict[str, Any]
) -> Any | None:
    reference = schema.get("$ref")
    prefix = "#/components/schemas/"
    if isinstance(reference, str) and reference.startswith(prefix):
        return examples.get(reference.removeprefix(prefix))

    # FastAPI uses a union for polymorphic event responses. Pick the first
    # reviewed example represented by one of the referenced alternatives.
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            example = _component_example_for_schema(variant, examples)
            if example is not None:
                return example
    return None


def _component_example_for_media(
    media: dict[str, Any], examples: dict[str, Any]
) -> Any | None:
    media_schema = media.get("schema")
    if not isinstance(media_schema, dict):
        return None
    return _component_example_for_schema(media_schema, examples)


def _enrich_request_examples(operation: dict[str, Any]) -> None:
    """Publish one copyable example for every documented JSON request body."""
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return
    media = request_body.get("content", {}).get("application/json")
    if not isinstance(media, dict):
        return
    example = _component_example_for_media(media, REQUEST_COMPONENT_EXAMPLES)
    if example is None:
        return

    if "example" not in media and "examples" not in media:
        media["examples"] = {
            "sample_request": {
                "summary": "Sample request",
                "description": (
                    "A representative JSON request body. Replace any example "
                    "resource IDs with IDs from your Organization."
                ),
                "value": deepcopy(example),
            }
        }

    code_samples = operation.setdefault("x-codeSamples", [])
    if not any(
        isinstance(sample, dict) and sample.get("id") == "request-json"
        for sample in code_samples
    ):
        code_samples.append(
            {
                "id": "request-json",
                "lang": "json",
                "label": "Request JSON",
                "source": json.dumps(example, indent=2, ensure_ascii=False),
            }
        )


def _enrich_response_examples(
    *, method: str, path: str, operation: dict[str, Any]
) -> None:
    """Attach payload examples at the OpenAPI media layer, outside schemas."""
    for status_code, response in operation.get("responses", {}).items():
        if not isinstance(response, dict):
            continue
        for media_type, media in response.get("content", {}).items():
            if (
                not isinstance(media, dict)
                or "example" in media
                or "examples" in media
            ):
                continue
            example = None
            if media_type == "application/json":
                example = OPERATION_RESPONSE_EXAMPLES.get(
                    (method, path, str(status_code))
                )
            if example is None:
                example = _component_example_for_media(
                    media, RESPONSE_COMPONENT_EXAMPLES
                )
            if example is not None:
                media["example"] = deepcopy(example)


def _operation_description(operation: dict[str, Any]) -> str:
    summary = str(operation.get("summary") or "this operation").strip()
    return f"{summary.rstrip('.')} for your Organization."


def _enrich_success_response(
    *, method: str, path: str, operation: dict[str, Any]
) -> None:
    configured = OPERATION_SUCCESS_RESPONSES.get((method, path))
    if configured is None:
        return
    status, description, example = configured
    response = operation.setdefault("responses", {}).get(status)
    if not isinstance(response, dict):
        return
    response["description"] = description
    media = response.get("content", {}).get("application/json")
    if isinstance(media, dict):
        media["example"] = example


def _enrich_sse_response(*, method: str, path: str, operation: dict[str, Any]) -> None:
    if method != "get" or path != "/v1/sessions/{session_id}/events/stream":
        return
    operation.setdefault("responses", {})["200"] = {
        "description": (
            "A server-sent event stream. Each frame carries the durable event "
            "sequence as its SSE id, the event type, and a JSON data payload."
        ),
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
                "example": "id: 42\n"
                "event: agent.message\n"
                "data: "
                + json.dumps(
                    {
                        **AGENT_MESSAGE_EVENT_EXAMPLE,
                        "id": EVENT_ID_EXAMPLE,
                        "seq": 42,
                    },
                    separators=(",", ":"),
                )
                + "\n\n",
            }
        },
    }


def _active_tags(schema: dict[str, Any]) -> list[str]:
    used: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS and isinstance(operation, dict):
                used.update(str(tag) for tag in operation.get("tags", []))
    known = [tag for tag in TAG_DESCRIPTIONS if tag in used]
    return [*known, *sorted(used.difference(known))]


def build_documentation_schema(*, server_url: str) -> dict[str, Any]:
    """Return a deterministic docs schema derived from the active application."""
    schema = create_app().openapi()
    schema["servers"] = [
        {
            "url": server_url.rstrip("/"),
            "description": "Votrix Managed Agents (VMA) API",
        }
    ]

    info = schema.setdefault("info", {})
    info["description"] = (
        "VMA is an API-first agent harness framework that gives every Agent a "
        "sandbox for tools, files, and ongoing work.\n\n"
        f"> **Base URL** `{server_url.rstrip('/')}`\n\n"
        "Send your VMA API key in the `x-api-key` header with each request. The key "
        "automatically connects the request to the right Organization."
    )

    _enrich_component_schemas(schema)
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            normalized_method = method.lower()
            if normalized_method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation["description"] = OPERATION_DESCRIPTIONS.get(
                (normalized_method, path),
                _operation_description(operation),
            )
            _enrich_success_response(
                method=normalized_method,
                path=path,
                operation=operation,
            )
            _enrich_response_examples(
                method=normalized_method,
                path=path,
                operation=operation,
            )
            _enrich_request_examples(operation)
            for parameter in operation.get("parameters", []):
                if isinstance(parameter, dict):
                    _enrich_parameter(parameter)
            _enrich_sse_response(
                method=normalized_method,
                path=path,
                operation=operation,
            )

    schema["tags"] = [
        {
            "name": tag,
            "description": TAG_DESCRIPTIONS.get(tag, (tag, f"{tag} API operations."))[1],
            "x-displayName": TAG_DESCRIPTIONS.get(tag, (tag, ""))[0],
        }
        for tag in _active_tags(schema)
    ]
    return schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the active FastAPI OpenAPI schema used by Fumadocs."
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
