import json
from typing import Any

from scripts.export_openapi import (
    DEFAULT_OUTPUT,
    DEFAULT_SERVER_URL,
    build_documentation_schema,
)


HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}


def public_operations(
    schema: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, path_item in schema["paths"].items()
        if path.startswith("/v1/")
        for method, operation in path_item.items()
        if method in HTTP_METHODS and isinstance(operation, dict)
    ]


async def test_openapi_schema_remains_available_without_embedded_docs_ui(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200, response.text
    assert response.json()["info"]["title"] == "Votrix Managed Agents (VMA)"

    for path in ("/docs", "/redoc"):
        response = await client.get(path)
        assert response.status_code == 404, response.text


def test_committed_documentation_schema_is_current():
    committed = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    generated = build_documentation_schema(server_url=DEFAULT_SERVER_URL)

    assert committed == generated, (
        "website/public/openapi/vma.json is stale; run "
        "`cd website && npm run openapi:sync`"
    )


def test_documentation_uses_the_current_votrixai_domain():
    committed = DEFAULT_OUTPUT.read_text(encoding="utf-8")
    api_page = (
        DEFAULT_OUTPUT.parents[2] / "components" / "api-page.tsx"
    ).read_text(encoding="utf-8")

    assert DEFAULT_SERVER_URL == "https://api.vma.votrixai.com"
    assert "votrix" + ".ai" not in committed
    assert "votrix-managed-agents-openapi-v2-" in api_page


def test_documentation_schema_describes_every_public_operation_and_parameter():
    schema = build_documentation_schema(server_url="https://api.example.test")
    operations = public_operations(schema)

    assert operations
    for path, method, operation in operations:
        description = operation.get("description", "").strip()
        assert description, f"missing description for {method.upper()} {path}"
        assert description != operation.get("summary", "").strip()

        for parameter in operation.get("parameters", []):
            assert parameter.get("description", "").strip(), (
                f"missing description for {method.upper()} {path} "
                f"parameter {parameter.get('name')}"
            )
            assert "title" not in parameter.get("schema", {}), (
                f"redundant schema title for {method.upper()} {path} "
                f"parameter {parameter.get('name')}"
            )


def test_documentation_schema_adds_examples_and_code_samples_to_core_writes():
    server_url = "https://api.example.test"
    schema = build_documentation_schema(server_url=server_url)
    core_operations = (
        ("post", "/v1/agents", "201"),
        ("post", "/v1/environments", "201"),
        ("post", "/v1/sessions", "201"),
        ("post", "/v1/sessions/{session_id}/events", "200"),
    )

    for method, path, status in core_operations:
        operation = schema["paths"][path][method]
        request_media = operation["requestBody"]["content"]["application/json"]
        response_media = operation["responses"][status]["content"]["application/json"]
        assert request_media["example"]
        assert response_media["example"]

        samples = operation["x-codeSamples"]
        assert [sample["id"] for sample in samples] == ["curl", "python", "js"]
        assert {sample["lang"] for sample in samples} == {
            "bash",
            "python",
            "javascript",
        }
        assert {sample["label"] for sample in samples} == {
            "cURL",
            "Python",
            "JavaScript",
        }
        for sample in samples:
            assert server_url in sample["source"]
            assert "votrix-managed-agents-beta" in sample["source"]
            assert "VMA_API_KEY" in sample["source"]
            if sample["id"] == "python":
                compile(sample["source"], "<openapi-python-sample>", "exec")


def test_documentation_schema_adds_real_examples_to_core_reads():
    schema = build_documentation_schema(server_url="https://api.example.test")
    core_reads = (
        "/v1/agents",
        "/v1/agents/{agent_id}",
        "/v1/environments",
        "/v1/environments/{environment_id}",
        "/v1/sessions",
        "/v1/sessions/{session_id}",
    )

    for path in core_reads:
        media = schema["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]
        assert media["example"], f"missing response example for GET {path}"


def test_documentation_schema_matches_sse_and_error_response_contracts():
    schema = build_documentation_schema(server_url="https://api.example.test")
    error_ref = "#/components/schemas/ApiErrorResponse"
    assert error_ref.rsplit("/", 1)[-1] in schema["components"]["schemas"]

    for path, method, operation in public_operations(schema):
        for status in ("400", "401"):
            response_schema = operation["responses"][status]["content"][
                "application/json"
            ]["schema"]
            assert response_schema == {"$ref": error_ref}, (
                f"incorrect {status} response for {method.upper()} {path}"
            )
        if "422" in operation["responses"]:
            response_schema = operation["responses"]["422"]["content"][
                "application/json"
            ]["schema"]
            assert response_schema == {"$ref": error_ref}

    stream_paths = (
        "/v1/sessions/{session_id}/events/stream",
        "/v1/sessions/{session_id}/stream",
    )
    for path in stream_paths:
        content = schema["paths"][path]["get"]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}
        assert "event: agent.message" in content["text/event-stream"]["example"]


def test_documentation_schema_describes_every_component_property():
    schema = build_documentation_schema(server_url="https://api.example.test")
    schemas = schema["components"]["schemas"]

    for schema_name, component_schema in schemas.items():
        for property_name, property_schema in component_schema.get(
            "properties", {}
        ).items():
            assert property_schema.get("description", "").strip(), (
                f"missing description for {schema_name}.{property_name}"
            )
            assert "title" not in property_schema, (
                f"redundant schema title for {schema_name}.{property_name}"
            )


def test_documentation_schema_supports_interactive_binary_and_skill_uploads():
    schema = build_documentation_schema(server_url="https://api.example.test")

    file_upload = schema["components"]["schemas"][
        "Body_upload_file_v1_files_post"
    ]["properties"]["file"]
    assert file_upload["contentMediaType"] == "application/octet-stream"
    assert file_upload["format"] == "binary"

    for path in ("/v1/skills", "/v1/skills/{skill_id}/versions"):
        content = schema["paths"][path]["post"]["requestBody"]["content"]
        assert set(content) == {"application/json", "multipart/form-data"}
        files = content["multipart/form-data"]["schema"]["properties"]["files"]
        assert files["items"]["format"] == "binary"

    for path in (
        "/v1/files/{file_id}/content",
        "/v1/skills/{skill_id}/versions/{version}/content",
    ):
        response = schema["paths"][path]["get"]["responses"]["200"]
        assert set(response["content"]) == {"application/octet-stream"}
        assert response["content"]["application/octet-stream"]["schema"] == {
            "type": "string",
            "format": "binary",
        }
        assert set(response["headers"]) == {
            "Cache-Control",
            "Content-Disposition",
            "X-Content-Type-Options",
        }
