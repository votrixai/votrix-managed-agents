import asyncio

from tests.conftest import VOTRIX_MANAGED_AGENTS_HEADERS, TEST_HEADERS


async def _create_agent(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Coding Assistant",
            "model": {"id": "gpt-5.5"},
            "system": "You are concise.",
            "tools": [{"type": "agent_toolset_20260401"}],
            "metadata": {"team": "platform"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={
            "name": "local-dev",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_agent_update_creates_immutable_versions(client):
    agent = await _create_agent(client)

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={
            "version": agent["version"],
            "system": "You are very concise.",
            "metadata": {"team": "platform-v2", "remove_me": "x"},
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 2
    assert updated["metadata"]["team"] == "platform-v2"

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": 2, "metadata": {"remove_me": ""}},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 3
    assert "remove_me" not in updated["metadata"]

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 3

    response = await client.get(f"/v1/agents/{agent['id']}/versions", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    versions = response.json()["data"]
    assert [v["version"] for v in versions] == [3, 2, 1]


async def test_agent_update_rejects_stale_version(client):
    agent = await _create_agent(client)

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "system": "v2"},
    )
    assert response.status_code == 200, response.text

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "system": "stale write"},
    )
    assert response.status_code == 409


async def test_agent_name_and_model_contract_is_enforced(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "   ", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 422
    assert "non-empty" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Missing Model ID", "model": {"provider": "openai"}},
    )
    assert response.status_code == 422
    assert "model.id" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Bad Speed", "model": {"id": "gpt-5.5", "speed": "warp"}},
    )
    assert response.status_code == 422
    assert "model.speed" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Model Alias", "model": {"model": "mini-max/MiniMax-Text-01", "provider": "mini-max"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    assert agent["name"] == "Model Alias"
    assert agent["model"]["id"] == "mini-max/MiniMax-Text-01"
    assert "model" not in agent["model"]
    assert agent["model"]["provider"] == "mini-max"

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Conflicting Model Aliases",
            "model": {"id": "model-a", "model": "model-b"},
        },
    )
    assert response.status_code == 422
    assert "must match" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "No Inline Provider Secret",
            "model": {"id": "gpt-5.5", "api_key": "must-use-a-vault"},
        },
    )
    assert response.status_code == 422
    assert "api_key" in response.json()["error"]["message"]

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "name": ""},
    )
    assert response.status_code == 422

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "model": ""},
    )
    assert response.status_code == 422


async def test_agent_metadata_limits_are_enforced(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Too Much Metadata",
            "model": {"id": "gpt-5.5"},
            "metadata": {f"k{index}": "v" for index in range(17)},
        },
    )
    assert response.status_code == 422
    assert "at most 16 keys" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Long Metadata Key", "model": {"id": "gpt-5.5"}, "metadata": {"x" * 65: "v"}},
    )
    assert response.status_code == 422
    assert "at most 64 characters" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Long Metadata Value", "model": {"id": "gpt-5.5"}, "metadata": {"key": "v" * 513}},
    )
    assert response.status_code == 422
    assert "at most 512 characters" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Non String Metadata", "model": {"id": "gpt-5.5"}, "metadata": {"key": 1}},
    )
    assert response.status_code == 422
    assert "values must be strings" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Full Metadata",
            "model": {"id": "gpt-5.5"},
            "metadata": {f"k{index}": "v" for index in range(16)},
        },
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "metadata": {"extra": "v"}},
    )
    assert response.status_code == 422
    assert "at most 16 keys" in response.json()["error"]["message"]


async def test_agent_tool_limits_and_custom_tool_names_are_enforced(client):
    def custom_tool(index: int, *, name: str | None = None, description: str = "Lookup a record.") -> dict:
        return {
            "type": "custom",
            "name": name or f"lookup_{index}",
            "description": description,
            "input_schema": {"type": "object", "properties": {}},
        }

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Too Many Tools",
            "model": {"id": "gpt-5.5"},
            "tools": [custom_tool(index) for index in range(129)],
        },
    )
    assert response.status_code == 422
    assert "at most 128" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Too Many Toolset Configs",
            "model": {"id": "gpt-5.5"},
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "configs": [{"name": f"tool_{index}"} for index in range(129)],
                }
            ],
        },
    )
    assert response.status_code == 422
    assert "at most 128" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Duplicate Tools",
            "model": {"id": "gpt-5.5"},
            "tools": [custom_tool(1, name="lookup"), custom_tool(2, name="lookup")],
        },
    )
    assert response.status_code == 422
    assert "Duplicate custom tool name" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Invalid Tool Name",
            "model": {"id": "gpt-5.5"},
            "tools": [custom_tool(1, name="lookup account")],
        },
    )
    assert response.status_code == 422
    assert "letters, digits" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Long Tool Description",
            "model": {"id": "gpt-5.5"},
            "tools": [custom_tool(1, description="x" * 1025)],
        },
    )
    assert response.status_code == 422
    assert "at most 1024" in response.json()["error"]["message"]

    agent = await _create_agent(client)
    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "tools": [custom_tool(index) for index in range(129)]},
    )
    assert response.status_code == 422


async def test_multiagent_roster_pins_referenced_agent_versions(client):
    reviewer = await _create_agent(client)

    response = await client.patch(
        f"/v1/agents/{reviewer['id']}",
        headers=TEST_HEADERS,
        json={"version": reviewer["version"], "system": "Reviewer v2"},
    )
    assert response.status_code == 200, response.text
    reviewer_v2 = response.json()

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Coordinator",
            "model": {"id": "gpt-5.5"},
            "tools": [{"type": "agent_toolset_20260401"}],
            "multiagent": {
                "type": "coordinator",
                "agents": [
                    reviewer["id"],
                    {"type": "self"},
                ],
            },
        },
    )
    assert response.status_code == 201, response.text
    coordinator = response.json()
    assert coordinator["multiagent"]["agents"][0]["version"] == reviewer_v2["version"]
    assert coordinator["multiagent"]["agents"][1]["id"] == coordinator["id"]
    assert coordinator["multiagent"]["agents"][1]["version"] == coordinator["version"]

    response = await client.patch(
        f"/v1/agents/{reviewer['id']}",
        headers=TEST_HEADERS,
        json={"version": reviewer_v2["version"], "system": "Reviewer v3"},
    )
    assert response.status_code == 200, response.text
    reviewer_v3 = response.json()

    response = await client.get(f"/v1/agents/{coordinator['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["multiagent"]["agents"][0]["version"] == reviewer_v2["version"]

    response = await client.patch(
        f"/v1/agents/{coordinator['id']}",
        headers=TEST_HEADERS,
        json={
            "version": coordinator["version"],
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "agent", "id": reviewer["id"]}],
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["multiagent"]["agents"][0]["version"] == reviewer_v3["version"]


async def test_multiagent_roster_contract_is_enforced(client):
    reviewer = await _create_agent(client)

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Empty Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": []},
        },
    )
    assert response.status_code == 422
    assert "at least one" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Duplicate Self Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": [{"type": "self"}, {"type": "self"}]},
        },
    )
    assert response.status_code == 422
    assert "at most one self" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Duplicate Agent Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": [reviewer["id"], {"type": "agent", "id": reviewer["id"]}]},
        },
    )
    assert response.status_code == 422
    assert "distinct agents" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Bad Version Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": [{"type": "agent", "id": reviewer["id"], "version": 0}]},
        },
    )
    assert response.status_code == 422
    assert "at least 1" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Nested Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": [{"type": "self"}]},
        },
    )
    assert response.status_code == 201, response.text
    nested = response.json()

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Too Deep Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {"type": "coordinator", "agents": [nested["id"]]},
        },
    )
    assert response.status_code == 422
    assert "multiagent agent" in response.json()["error"]["message"]

    response = await client.patch(
        f"/v1/agents/{reviewer['id']}",
        headers=TEST_HEADERS,
        json={
            "version": reviewer["version"],
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "self"}, {"type": "agent", "id": reviewer["id"]}],
            },
        },
    )
    assert response.status_code == 422
    assert "distinct agents" in response.json()["error"]["message"]


async def test_agent_mcp_servers_must_match_mcp_toolsets(client):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Dangling MCP Server",
            "model": {"id": "gpt-5.5"},
            "mcp_servers": [{"type": "url", "name": "github", "url": "https://mcp.example.com/github"}],
            "tools": [{"type": "agent_toolset_20260401"}],
        },
    )
    assert response.status_code == 422

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Dangling MCP Toolset",
            "model": {"id": "gpt-5.5"},
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "github"}],
        },
    )
    assert response.status_code == 422

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Bound MCP",
            "model": {"id": "gpt-5.5"},
            "mcp_servers": [{"type": "url", "name": "github", "url": "https://mcp.example.com/github"}],
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "github"}],
        },
    )
    assert response.status_code == 201, response.text


async def test_environment_name_and_null_config_contract(client):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "   ", "config": {"type": "cloud"}},
    )
    assert response.status_code == 422
    assert "non-empty" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": " Nullable Config ", "config": None},
    )
    assert response.status_code == 201, response.text
    environment = response.json()
    assert environment["name"] == "Nullable Config"
    assert environment["config"]["type"] == "cloud"

    response = await client.patch(
        f"/v1/environments/{environment['id']}",
        headers=TEST_HEADERS,
        json={"config": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config"]["type"] == "cloud"

    response = await client.patch(
        f"/v1/environments/{environment['id']}",
        headers=TEST_HEADERS,
        json={"name": ""},
    )
    assert response.status_code == 422


async def test_session_pins_agent_version_and_processes_user_event(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "title": "First task",
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["agent_version"] == 1

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={
            "events": [
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": "Say hello"}],
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    sent_event = response.json()["data"][0]
    assert sent_event["type"] == "user.message"
    assert sent_event["processed_at"] is None

    for _ in range(20):
        response = await client.get(
            f"/v1/sessions/{session['id']}/events",
            headers=TEST_HEADERS,
        )
        assert response.status_code == 200, response.text
        types = [event["type"] for event in response.json()["data"]]
        if "agent.message" in types and "session.status_idle" in types:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"runtime did not emit completion events; saw {types}")

    assert types[0] == "session.status_idle"
    assert "user.message" in types
    assert "session.status_running" in types
    assert "agent.message" in types
    by_type = {event["type"]: event for event in response.json()["data"]}
    assert by_type["user.message"]["processed_at"] is None
    assert by_type["session.status_idle"]["processed_at"] is not None
    assert by_type["session.status_running"]["processed_at"] is not None
    assert by_type["agent.message"]["processed_at"] is not None


async def test_missing_beta_header_is_rejected(client):
    response = await client.post(
        "/v1/agents",
        headers={"anthropic-version": "2023-06-01"},
        json={"name": "No Beta", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_openapi_presents_votrix_headers_without_compatibility_headers(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200, response.text

    spec = response.json()
    operation = spec["paths"]["/v1/agents"]["post"]
    header_names = {
        parameter["name"]
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    }

    assert "votrix-managed-agents-beta" in header_names
    assert "anthropic-beta" not in header_names
    assert "anthropic-version" not in header_names
    assert "open-managed-agents" not in str(spec).lower()


async def test_votrix_managed_agents_beta_alias_is_accepted(client):
    response = await client.post(
        "/v1/agents",
        headers=VOTRIX_MANAGED_AGENTS_HEADERS,
        json={"name": "Votrix Beta", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text

    response = await client.post(
        "/v1/agents",
        headers={
            **TEST_HEADERS,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "votrix-managed-agents-2026-04-01",
        },
        json={"name": "Votrix Beta Compatibility Header", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text


async def test_anthropic_agent_memory_beta_is_accepted(client):
    response = await client.post(
        "/v1/memory_stores",
        headers={
            **TEST_HEADERS,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "agent-memory-2026-07-22",
        },
        json={"name": "Agent Memory Beta"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["type"] == "memory_store"


async def test_missing_anthropic_version_header_is_rejected(client):
    headers = {"anthropic-beta": "managed-agents-2026-04-01"}
    response = await client.post(
        "/v1/agents",
        headers=headers,
        json={"name": "No Version", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 400
    assert "Anthropic API version" in response.json()["error"]["message"]
