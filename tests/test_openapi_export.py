"""The checked-in API reference must mirror the active FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_openapi import (
    DEFAULT_OUTPUT,
    DEFAULT_SERVER_URL,
    REQUEST_COMPONENT_EXAMPLES,
    RESPONSE_COMPONENT_EXAMPLES,
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

ACCOUNT_OPERATIONS = {
    ("post", "/v1/accounts"): ("idempotency_key", "uncapped", "Session"),
    ("get", "/v1/accounts"): ("oldest first", "is_default", "status"),
    ("get", "/v1/accounts/{account_id}"): (
        "provisioning",
        "suspended",
        "limit_usd",
    ),
    ("get", "/v1/accounts/{account_id}/usage"): (
        "usage_usd",
        "limit_remaining_usd",
        "suspended",
    ),
    ("get", "/v1/accounts/usage"): (
        "usage_usd",
        "every Account",
        "Suspended Accounts",
    ),
    ("post", "/v1/accounts/{account_id}/suspend"): (
        "default Account",
        "Existing Sessions",
        "usage history",
    ),
    ("post", "/v1/accounts/{account_id}/resume"): (
        "Existing Sessions",
        "usage history",
        "already active",
    ),
}

ACCOUNT_GUIDE_LINK = "[Accounts guide](/docs/accounts)"
API_REFERENCE_INDEX = Path(__file__).parents[1] / "docs" / "api" / "index.mdx"


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


def test_every_json_request_body_has_a_copyable_sample_request():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    documented: list[str] = []

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            media = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json")
            )
            if media is None:
                continue

            documented.append(f"{method.upper()} {path}")
            sample = media["examples"]["sample_request"]
            assert sample["summary"] == "Sample request"
            assert sample["value"]

            request_json = next(
                item
                for item in operation["x-codeSamples"]
                if item.get("id") == "request-json"
            )
            assert request_json["label"] == "Request JSON"
            assert request_json["lang"] == "json"
            assert json.loads(request_json["source"]) == sample["value"]

    assert documented


def _sample_leaves(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _sample_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _sample_leaves(child)
    else:
        yield value


def test_every_response_body_has_a_concrete_sample_response():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    documented: list[str] = []

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            for status_code, response in operation.get("responses", {}).items():
                for media_type, media in response.get("content", {}).items():
                    label = f"{method.upper()} {path} {status_code} {media_type}"
                    documented.append(label)
                    if "example" in media:
                        samples = [media["example"]]
                    else:
                        examples = media.get("examples")
                        assert examples, label
                        samples = [
                            item.get("value") if isinstance(item, dict) else item
                            for item in examples.values()
                        ]

                    for sample in samples:
                        assert sample not in ({}, [], "", "string"), label
                        assert "string" not in _sample_leaves(sample), label

    assert documented


def test_stream_and_download_responses_document_the_actual_transport():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)

    stream = schema["paths"][
        "/v1/sessions/{session_id}/events/stream"
    ]["get"]["responses"]["200"]["content"]["text/event-stream"]
    frame = stream["example"]
    assert frame.startswith("id: 42\nevent: agent.message\ndata: ")
    event = json.loads(
        next(
            line.removeprefix("data: ")
            for line in frame.splitlines()
            if line.startswith("data: ")
        )
    )
    assert event["type"] == "agent.message"
    assert event["seq"] == 42
    assert event["content"][0]["type"] == "text"
    assert "session_id" not in event

    file_download = schema["paths"]["/v1/files/{file_id}/content"]["get"]
    assert set(file_download["responses"]) == {"307", "422"}
    redirect = file_download["responses"]["307"]
    assert "content" not in redirect
    assert redirect["headers"]["Location"]["schema"] == {
        "type": "string",
        "format": "uri",
    }

    skill_download = schema["paths"]["/v1/skills/{skill_id}/content"]["get"]
    package = skill_download["responses"]["200"]["content"]["application/zip"]
    assert package["schema"] == {"type": "string", "format": "binary"}
    assert package["example"] == "<binary ZIP data>"


def test_user_messages_publish_the_durable_file_image_contract():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    schemas = schema["components"]["schemas"]

    content = schemas["UserMessageInput"]["properties"]["content"]
    assert content["items"]["discriminator"]["mapping"] == {
        "image": "#/components/schemas/ImageBlock",
        "text": "#/components/schemas/TextBlock",
    }
    assert schemas["ImageBlock"]["properties"]["source"] == {
        "$ref": "#/components/schemas/FileImageSource"
    }
    source = schemas["FileImageSource"]
    assert source["properties"]["type"]["const"] == "file"
    assert source["required"] == ["file_id"]


def test_required_request_parameters_have_realistic_examples():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("required") and parameter.get("in") in {
                    "header",
                    "path",
                }:
                    assert parameter.get("example"), parameter


def test_agent_examples_create_a_runnable_definition_without_empty_items():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    create = schema["paths"]["/v1/agents"]["post"]
    request = create["requestBody"]["content"]["application/json"]["examples"][
        "sample_request"
    ]["value"]

    assert request["tools"] == [{"type": "agent_toolset_20260401"}]
    for field in ("mcp_servers", "skills"):
        assert field not in request

    schemas = schema["components"]["schemas"]
    response = create["responses"]["201"]["content"]["application/json"]["example"]
    version = schema["paths"]["/v1/agents/{agent_id}/versions"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]["data"][0]
    assert response["tools"] == request["tools"]
    assert version["tools"] == request["tools"]
    for field in ("mcp_servers", "skills"):
        assert response[field] == []
        assert version[field] == []

    quickstart = (Path(__file__).parents[1] / "docs" / "quickstart.md").read_text(
        encoding="utf-8"
    )
    assert '"tools": [{"type":"agent_toolset_20260401"}]' in quickstart

    # `description` is an API field, not merely a JSON Schema annotation. The
    # presentation cleanup must preserve it so every example remains valid for
    # the schema it documents.
    for component_name in (
        "AgentCreateRequest",
        "AgentUpdateRequest",
        "AgentResponse",
        "AgentVersionResponse",
    ):
        component = schemas[component_name]
        assert "description" in component["properties"]

    assert set(response) <= set(schemas["AgentResponse"]["properties"])
    assert set(version) <= set(schemas["AgentVersionResponse"]["properties"])


def test_agent_and_environment_schemas_explain_runtime_requirements():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    components = schema["components"]["schemas"]

    tools = components["AgentCreateRequest"]["properties"]["tools"]
    assert "agent_toolset_20260401" in tools["description"]

    update_config = components["EnvironmentUpdateRequest"]["properties"]["config"]
    assert "replacement" in update_config["description"]

    build_state = components["EnvironmentResponse"]["properties"]["build_state"]
    assert all(state in build_state["description"] for state in ("building", "ready", "failed"))


def test_payload_examples_are_never_embedded_in_component_schemas():
    """Prevent Fumadocs from compiling payload `id` fields as schema URIs."""
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    schemas = schema["components"]["schemas"]

    for component_name in REQUEST_COMPONENT_EXAMPLES | RESPONSE_COMPONENT_EXAMPLES:
        component = schemas[component_name]
        assert "example" not in component
        assert "examples" not in component


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
            assert headers["x-api-key"]["required"] is True


def test_documentation_openapi_marks_the_api_key_as_required():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            api_key = next(
                parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("name") == "x-api-key"
            )
            assert api_key["required"] is True
            assert api_key["schema"] == {"type": "string"}


def test_billing_and_stripe_webhooks_are_not_vma_api_operations():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    assert not any(path.startswith("/v1/billing") for path in schema["paths"])
    assert "/webhooks/stripe" not in schema["paths"]


def test_account_reference_explains_each_public_operation_and_field():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)

    for (method, path), expected_phrases in ACCOUNT_OPERATIONS.items():
        operation = schema["paths"][path][method]
        description = operation["description"]
        assert len(description) >= 200
        assert all(phrase in description for phrase in expected_phrases)
        assert ACCOUNT_GUIDE_LINK in description

        success_status = "201" if (method, path) == ("post", "/v1/accounts") else "200"
        success = operation["responses"][success_status]
        assert success["description"] != "Successful Response"
        assert success["content"]["application/json"]["example"]

    schemas = schema["components"]["schemas"]
    for component_name in (
        "AccountCreateRequest",
        "AccountResponse",
        "AccountUsageSummary",
        "AccountUsageResponse",
        "OrganizationUsageResponse",
        "ListResponse_AccountResponse_",
    ):
        component = schemas[component_name]
        assert component["description"]
        assert all(
            property_schema.get("description")
            for property_schema in component["properties"].values()
        )

    assert "patch" not in schema["paths"]["/v1/accounts/{account_id}"]
    assert "delete" not in schema["paths"]["/v1/accounts/{account_id}"]
    suspend_example = schema["paths"]["/v1/accounts/{account_id}/suspend"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]
    assert suspend_example["status"] == "suspended"


def test_api_reference_landing_page_links_to_the_accounts_guide():
    content = API_REFERENCE_INDEX.read_text(encoding="utf-8")
    assert "[Accounts](../accounts.md)" in content


def test_documentation_openapi_does_not_publish_internal_technology_details():
    schema = build_documentation_schema(server_url=DEFAULT_SERVER_URL)
    rendered = json.dumps(schema).lower()
    forbidden = (
        "cloud tasks",
        "container",
        "credential",
        "database",
        "deep agents",
        "e2b",
        "fastapi",
        "langgraph",
        "postgres",
        "provider",
    )
    assert not [term for term in forbidden if term in rendered]
