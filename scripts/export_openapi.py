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
        "Track and control usage for Agent work.",
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

COMPONENT_EXAMPLES = {
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
OPERATION_DESCRIPTIONS = {
    ("post", "/v1/accounts"): (
        "Create an Account for tracking and limiting Agent usage separately."
    ),
    ("get", "/v1/accounts/{account_id}/usage"): (
        "Return the Account's total, daily, weekly, and monthly usage in USD, "
        "together with its limit and remaining amount when a limit is set."
    ),
    ("post", "/v1/accounts/{account_id}/suspend"): (
        "Prevent an Account from funding further Agent work while preserving "
        "its identity, limit, and usage history. The default Account cannot be "
        "suspended."
    ),
    ("post", "/v1/accounts/{account_id}/resume"): (
        "Allow a suspended Account to fund Agent work again."
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

COMPONENT_DESCRIPTIONS = {
    "AccountUsageResponse": (
        "Current Account usage in USD, including total and period values."
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
        "Reuse this value when retrying creation to receive the same Account."
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
