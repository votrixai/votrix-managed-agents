"""The checked-in API reference must mirror the active FastAPI application."""

from __future__ import annotations

import json

from scripts.export_openapi import (
    DEFAULT_OUTPUT,
    DEFAULT_SERVER_URL,
    build_documentation_schema,
)


# The store's lifecycle, and nothing about what is inside one: a Memory Store
# is a Volume the Agent mounts, so its contents are reached through the
# filesystem in a Session rather than over HTTP.
MEMORY_OPERATIONS = {
    "DELETE /v1/memory_stores/{memory_store_id}",
    "GET /v1/memory_stores",
    "GET /v1/memory_stores/{memory_store_id}",
    "POST /v1/memory_stores",
    "POST /v1/memory_stores/{memory_store_id}",
    "POST /v1/memory_stores/{memory_store_id}/archive",
}


def test_no_document_api_is_published_over_a_memory_store():
    """Deliberately absent. Documents used to have their own CRUD and version
    history, kept in step by hashing the whole mount after every turn, and
    nothing ever read it."""

    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    assert not [path for path in schema["paths"] if "/memories" in path]
    assert not [path for path in schema["paths"] if "memory_versions" in path]


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
