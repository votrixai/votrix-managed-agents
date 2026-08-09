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
        "Organization records and role-based membership placeholders.",
    ),
    "agents": ("Agents", "Agent definitions and immutable Agent versions."),
    "environments": (
        "Environments",
        "Execution environment configuration and lifecycle.",
    ),
    "files": ("Files", "Private uploaded files and captured Session outputs."),
    "memory-stores": (
        "Memory Stores",
        "Persistent stores, path-addressed Memories, immutable Versions, and redaction.",
    ),
    "models": ("Models", "Model catalogue entries available to Agent definitions."),
    "sessions": (
        "Sessions",
        "Long-running Session lifecycle, resources, durable events, and streaming.",
    ),
    "skills": ("Skills", "Reusable Skill archives available to Agents."),
}

PARAMETER_DESCRIPTIONS = {
    "x-organization-id": (
        "Required Organization scope for this request. The current rewrite trusts "
        "this value and does not yet authenticate the caller."
    ),
    "x-api-key": (
        "Optional attribution value. It is accepted but not validated by the current "
        "rewrite and must not be treated as an authentication boundary."
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
    "page": "Opaque CMA pagination cursor returned by the previous page.",
    "path_prefix": "Slash-terminated Memory path prefix used to filter descendants.",
    "scope_id": "Only return Files belonging to this scope.",
    "session_id": "Only return Memory Versions attributed to this Session.",
    "status": "Only return resources with this status.",
    "view": "Choose the basic metadata view or the full content view.",
}

PARAMETER_EXAMPLES = {
    "x-organization-id": "org_1234567890abcdef1234567890abcdef",
    "x-api-key": "vma_example_key",
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
    if name in PARAMETER_EXAMPLES and "example" not in parameter:
        parameter["example"] = PARAMETER_EXAMPLES[name]


def _enrich_component_schemas(schema: dict[str, Any]) -> None:
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, component in schemas.items():
        if not isinstance(component, dict):
            continue
        if name in COMPONENT_EXAMPLES:
            component.setdefault("example", COMPONENT_EXAMPLES[name])
        for property_schema in component.get("properties", {}).values():
            if isinstance(property_schema, dict):
                # The field name is already visible in Fumadocs. Repeating the
                # generated title hides the useful primitive/union type.
                property_schema.pop("title", None)


def _operation_description(operation: dict[str, Any]) -> str:
    summary = str(operation.get("summary") or "this operation").strip()
    return (
        f"{summary.rstrip('.')} within the Organization selected by the "
        "`x-organization-id` header."
    )


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
        "The HTTP API implemented by the active Votrix Managed Agents rewrite. "
        "This document is generated from the same FastAPI application that serves "
        "requests, so route and model changes are checked into the documentation "
        "with the code.\n\n"
        f"> **Base URL** `{server_url.rstrip('/')}`\n\n"
        "Organization-scoped routes require `x-organization-id`. The current "
        "rewrite trusts that header and accepts, but does not authenticate, the "
        "optional `x-api-key`; do not expose it directly to untrusted clients."
    )

    _enrich_component_schemas(schema)
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            normalized_method = method.lower()
            if normalized_method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            if not str(operation.get("description") or "").strip():
                operation["description"] = _operation_description(operation)
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
