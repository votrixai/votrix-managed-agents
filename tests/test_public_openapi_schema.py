from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.export_openapi import build_documentation_schema


HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
ROOT = Path(__file__).resolve().parents[1]


def _json_schemas(operation: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    request_schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if isinstance(request_schema, dict):
        found.append(("request", request_schema))
    for status, response in operation.get("responses", {}).items():
        if not str(status).startswith("2"):
            continue
        response_schema = (
            response.get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if isinstance(response_schema, dict):
            found.append((f"response {status}", response_schema))
    return found


def test_public_operations_never_publish_empty_success_or_request_json_schemas() -> None:
    schema = build_documentation_schema(server_url="https://api.example.test")
    checked: list[str] = []

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            for location, json_schema in _json_schemas(operation):
                label = f"{method.upper()} {path} {location}"
                checked.append(label)
                assert json_schema, f"{label} exposes an empty JSON schema"
                assert json_schema.get("$ref") != "#/components/schemas/GenericBody", (
                    f"{label} exposes the unconstrained GenericBody compatibility model"
                )

    assert checked


def test_public_vault_operations_use_strict_native_schemas_only() -> None:
    schema = build_documentation_schema(server_url="https://api.example.test")
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    assert paths["/v1/vaults"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VaultCreateRequest"}
    assert paths["/v1/vaults/{vault_id}"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VaultUpdateRequest"}
    assert paths["/v1/vaults"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VaultResponse"}
    assert paths["/v1/vaults/{vault_id}"]["delete"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/VaultDeletedResponse"
    }

    for model_name in (
        "VaultCreateRequest",
        "VaultUpdateRequest",
        "VaultResponse",
        "VaultDeletedResponse",
    ):
        assert components[model_name]["additionalProperties"] is False

    assert not any(
        "/credentials" in path and "/model_credentials" not in path
        for path in paths
    )


def test_docs_remain_blocked_from_search_indexing() -> None:
    layout = (ROOT / "website/app/layout.tsx").read_text(encoding="utf-8")
    robots = (ROOT / "website/public/robots.txt").read_text(encoding="utf-8")

    assert "robots:" in layout
    assert layout.count("index: false") >= 2
    assert layout.count("follow: false") >= 2
    assert "noarchive: true" in layout
    assert "nosnippet: true" in layout
    assert robots.splitlines() == ["User-agent: *", "Disallow: /"]
