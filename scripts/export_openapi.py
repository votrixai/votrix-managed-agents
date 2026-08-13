"""Export the active FastAPI OpenAPI document used by the docs website.

The application owns every request and response schema.  This exporter only
adds presentation metadata that is useful to Fumadocs; it must not maintain a
second route allowlist or replace generated schemas with hand-written copies.
"""

from __future__ import annotations

import argparse
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

PARAMETER_EXAMPLES = {
    "x-api-key": "vma_live_example_key",
    "Last-Event-ID": "42",
    "after_seq": 42,
    "api_key_id": "apikey_1234567890abcdef1234567890abcdef",
    "created_at[gte]": "2026-07-01T00:00:00Z",
    "created_at[lte]": "2026-08-01T00:00:00Z",
    "depth": 1,
    "limit": 20,
    "memory_id": "mem_1234567890abcdef1234567890abcdef",
    "memory_store_id": "memstore_1234567890abcdef1234567890abcdef",
    "memory_version_id": "memver_1234567890abcdef1234567890abcdef",
    "operation": "modified",
    "page": "page_example_cursor",
    "path_prefix": "/projects/",
    "session_id": "sess_1234567890abcdef1234567890abcdef",
    "view": "full",
}

ACCOUNT_ID_EXAMPLE = "acct_1234567890abcdef1234567890abcdef"
ORGANIZATION_ID_EXAMPLE = "org_1234567890abcdef1234567890abcdef"
ACCOUNT_ACTIVE_EXAMPLE = {
    "id": ACCOUNT_ID_EXAMPLE,
    "type": "account",
    "organization_id": ORGANIZATION_ID_EXAMPLE,
    "name": "Website Builder",
    "status": "active",
    "is_default": False,
    "limit_usd": "20.00",
}
ACCOUNT_SUSPENDED_EXAMPLE = {
    **ACCOUNT_ACTIVE_EXAMPLE,
    "status": "suspended",
}
ACCOUNT_USAGE_EXAMPLE = {
    "account_id": ACCOUNT_ID_EXAMPLE,
    "type": "account_usage",
    "usage_usd": "8.40",
    "usage_daily_usd": "0.75",
    "usage_weekly_usd": "3.10",
    "usage_monthly_usd": "8.40",
    "limit_usd": "20.00",
    "limit_remaining_usd": "11.60",
}

COMPONENT_EXAMPLES = {
    "AccountCreateRequest": {
        "name": "Website Builder",
        "limit_usd": "20.00",
        "idempotency_key": "create-website-builder-account",
    },
    "AccountResponse": ACCOUNT_ACTIVE_EXAMPLE,
    "AccountUsageResponse": ACCOUNT_USAGE_EXAMPLE,
    "MemoryStoreCreateRequest": {
        "name": "Content Creator",
        "description": "Durable brand and project context.",
        "metadata": {"team": "creative"},
    },
    "MemoryStoreUpdateRequest": {
        "description": "Durable brand, asset, and active-project context.",
        "metadata": {"team": "content"},
    },
    "MemoryCreateRequest": {
        "path": "/context.md",
        "content": "Brand voice: warm, concise, and practical.",
    },
    "MemoryUpdateRequest": {
        "content": "Brand voice: warm, direct, concise, and practical.",
        "precondition": {
            "type": "content_sha256",
            "content_sha256": "0" * 64,
        },
    },
}


# Router and model docstrings are written for maintainers and can discuss how
# the service is built. The documentation schema deliberately replaces them
# with this small, reviewed public-contract vocabulary.
ACCOUNT_GUIDE_REFERENCE = (
    "\n\nRead the [Accounts guide](/docs/accounts) for default Account behavior, "
    "Session assignment, usage, spending limits, and suspension."
)


OPERATION_DESCRIPTIONS = {
    ("post", "/v1/accounts"): (
        "Create a separate usage and spending boundary inside your Organization. "
        "Use additional Accounts when work for different customers, teams, "
        "products, or environments should not be mixed together.\n\n"
        "An Account is uncapped unless `limit_usd` is supplied. A successful "
        "response has `status: \"active\"` and can be selected when creating a "
        "Session.\n\n"
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
    ("get", "/v1/accounts/{account_id}/usage"): (
        "Return a current USD usage snapshot for one Account. Usage for another "
        "Account is never included.\n\n"
        "`usage_usd` is cumulative for the Account. The daily, weekly, and "
        "monthly fields describe the current period windows. When a spending "
        "limit is set, `limit_usd` reports the limit and "
        "`limit_remaining_usd` reports the remaining amount. Both limit fields "
        "are `null` for an uncapped Account.\n\n"
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

COMPONENT_DESCRIPTIONS = {
    "AccountCreateRequest": (
        "The public fields accepted when creating an additional Account."
    ),
    "AccountResponse": (
        "An Account's identity, readiness, default role, and optional spending limit."
    ),
    "AccountUsageResponse": (
        "A current USD usage snapshot for one Account."
    ),
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
    ("AccountUsageResponse", "account_id"): (
        "The Account whose usage is represented by this response."
    ),
    ("AccountUsageResponse", "type"): (
        "Resource type. Always `account_usage`."
    ),
    ("AccountUsageResponse", "usage_usd"): (
        "Cumulative usage for the Account in USD."
    ),
    ("AccountUsageResponse", "usage_daily_usd"): (
        "Usage in the current daily window, in USD."
    ),
    ("AccountUsageResponse", "usage_weekly_usd"): (
        "Usage in the current weekly window, in USD."
    ),
    ("AccountUsageResponse", "usage_monthly_usd"): (
        "Usage in the current monthly window, in USD."
    ),
    ("AccountUsageResponse", "limit_usd"): (
        "The Account's spending limit in USD, or `null` when uncapped."
    ),
    ("AccountUsageResponse", "limit_remaining_usd"): (
        "The amount remaining under the Account's limit, or `null` when uncapped."
    ),
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
    if name in PARAMETER_DESCRIPTIONS:
        return PARAMETER_DESCRIPTIONS[name]
    if parameter.get("in") == "path" and name.endswith("_id"):
        resource = name.removesuffix("_id").replace("_", " ")
        return f"Unique identifier of the {resource} addressed by this request."
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
        for child in value.values():
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
        if name in COMPONENT_EXAMPLES:
            component.setdefault("example", COMPONENT_EXAMPLES[name])
        for property_name, property_schema in component.get("properties", {}).items():
            if isinstance(property_schema, dict):
                # The field name is already visible in Fumadocs. Repeating the
                # generated title hides the useful primitive/union type.
                property_schema.pop("title", None)
                description = PROPERTY_DESCRIPTIONS.get((name, property_name))
                if description is not None:
                    property_schema["description"] = description


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
                "example": (
                    "id: 42\n"
                    "event: agent.message\n"
                    'data: {"id":"evt_example","type":"agent.message",'
                    '"session_id":"sess_example","seq":42,'
                    '"content":"Done."}\n\n'
                ),
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
