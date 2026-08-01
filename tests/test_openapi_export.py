"""The checked-in API reference must mirror the active FastAPI application."""

from __future__ import annotations

import json

from scripts.export_openapi import (
    DEFAULT_OUTPUT,
    DEFAULT_SERVER_URL,
    build_documentation_schema,
)


MEMORY_OPERATIONS = {
    "DELETE /v1/memory_stores/{memory_store_id}",
    "DELETE /v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    "GET /v1/memory_stores",
    "GET /v1/memory_stores/{memory_store_id}",
    "GET /v1/memory_stores/{memory_store_id}/memories",
    "GET /v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    "GET /v1/memory_stores/{memory_store_id}/memory_versions",
    "GET /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}",
    "POST /v1/memory_stores",
    "POST /v1/memory_stores/{memory_store_id}",
    "POST /v1/memory_stores/{memory_store_id}/archive",
    "POST /v1/memory_stores/{memory_store_id}/memories",
    "POST /v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    "POST /v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}/redact",
}


def _operations(schema: dict) -> set[str]:
    methods = {"delete", "get", "patch", "post", "put"}
    return {
        f"{method.upper()} {path}"
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in methods
    }


def test_documentation_openapi_snapshot_matches_active_app():
    generated = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    committed = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    assert committed == generated


def test_documentation_openapi_publishes_complete_memory_surface():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    assert MEMORY_OPERATIONS <= _operations(schema)
    assert any(tag["name"] == "memory-stores" for tag in schema["tags"])

    for path, path_item in schema["paths"].items():
        if not path.startswith("/v1/memory_stores"):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "post"}:
                continue
            headers = {
                parameter["name"]: parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header"
            }
            assert headers["x-organization-id"]["required"] is True
