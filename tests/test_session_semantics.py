import asyncio

from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from tests.conftest import TEST_HEADERS


async def _create_agent(client, *, tools=None, mcp_servers=None):
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Session Semantic Agent",
            "model": {"id": "gpt-5.5"},
            "tools": tools or [],
            "mcp_servers": mcp_servers or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_environment(client, *, env_type="cloud"):
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": f"{env_type}-session-semantics", "config": {"type": env_type}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_session(client, agent, environment):
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={"agent": {"type": "agent", "id": agent["id"], "version": 1}, "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _wait_for_stop_reason(client, session_id: str, reason_type: str):
    for _ in range(30):
        response = await client.get(f"/v1/sessions/{session_id}", headers=TEST_HEADERS)
        assert response.status_code == 200, response.text
        session = response.json()
        if (session.get("stop_reason") or {}).get("type") == reason_type:
            return session
        await asyncio.sleep(0.05)
    raise AssertionError(f"session did not reach stop_reason={reason_type}; last={session}")


async def _wait_for_event_type(client, session_id: str, event_type: str):
    for _ in range(30):
        response = await client.get(f"/v1/sessions/{session_id}/events", headers=TEST_HEADERS)
        assert response.status_code == 200, response.text
        events = response.json()["data"]
        if any(event["type"] == event_type for event in events):
            return events
        await asyncio.sleep(0.05)
    raise AssertionError(f"session did not emit {event_type}; last={events}")


async def test_custom_tool_requires_action_and_resumes_from_result(client):
    agent = await _create_agent(client, tools=[{"type": "custom", "name": "lookup_customer"}])
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "lookup acme"}]},
    )
    assert response.status_code == 200, response.text

    session = await _wait_for_stop_reason(client, session["id"], "requires_action")
    custom_tool_use_id = session["stop_reason"]["event_ids"][0]
    events = await _wait_for_event_type(client, session["id"], "agent.custom_tool_use")
    assert next(event for event in events if event["id"] == custom_tool_use_id)["name"] == "lookup_customer"

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "new request before tool result"}]},
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={
            "events": [
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": custom_tool_use_id,
                    "content": [{"type": "text", "text": "ACME is enterprise."}],
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    events = await _wait_for_event_type(client, session["id"], "agent.message")
    assert any("custom tool result" in str(event.get("content")) for event in events if event["type"] == "agent.message")


async def test_client_cannot_set_input_event_processed_at(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={
            "events": [
                {
                    "type": "user.message",
                    "content": "use this context",
                },
                {
                    "type": "system.message",
                    "content": "context",
                    "processed_at": "2026-01-01T00:00:00Z",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["processed_at"] is None

    response = await client.get(f"/v1/sessions/{session['id']}/events", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    system_event = next(event for event in response.json()["data"] if event["type"] == "system.message")
    assert system_event["processed_at"] is None


async def test_system_message_batch_shape_is_validated(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    invalid_batches = [
        [{"type": "system.message", "content": "context"}],
        [
            {"type": "system.message", "content": "context"},
            {"type": "user.message", "content": "work"},
        ],
        [
            {"type": "user.message", "content": "work"},
            {"type": "system.message", "content": "context"},
            {"type": "system.message", "content": "more context"},
        ],
        [
            {"type": "user.tool_confirmation", "tool_use_id": "tool_missing", "result": "allow"},
            {"type": "system.message", "content": "context"},
        ],
    ]

    for events in invalid_batches:
        response = await client.post(
            f"/v1/sessions/{session['id']}/events",
            headers=TEST_HEADERS,
            json={"events": events},
        )

        assert response.status_code in {409, 422}, response.text


async def test_transient_runtime_failure_reschedules_then_caps_retries(client, monkeypatch):
    from app.runtime import runner

    class TransientRuntimeError(RuntimeError):
        status_code = 429
        retry_after = 1

    async def fail_transient(*args, **kwargs):
        raise TransientRuntimeError("rate limited")

    monkeypatch.setattr(runner, "_execute", fail_transient)

    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    await runner.run_session_turn(session["id"])
    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    first_retry = response.json()
    assert first_retry["status"] == "rescheduling"
    assert first_retry["stop_reason"]["type"] == "transient_error"
    assert first_retry["stop_reason"]["attempt"] == 1
    assert first_retry["stop_reason"]["retry_after_seconds"] == 1

    events = await _wait_for_event_type(client, session["id"], "session.status_rescheduled")
    assert any(event["type"] == "session.error" and event["transient"] for event in events)

    await runner.run_session_turn(session["id"])
    await runner.run_session_turn(session["id"])
    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    capped = response.json()
    assert capped["status"] == "terminated"
    assert capped["stop_reason"]["type"] == "error"
    assert capped["stop_reason"]["transient"] is True
    assert capped["stop_reason"]["attempt"] == 3


async def test_tool_confirmation_requires_action_and_resumes(client):
    agent = await _create_agent(
        client,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": {"permission_policy": {"type": "always_ask"}},
            }
        ],
    )
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "run shell"}]},
    )
    assert response.status_code == 200, response.text

    session = await _wait_for_stop_reason(client, session["id"], "requires_action")
    tool_use_id = session["stop_reason"]["event_ids"][0]
    events = await _wait_for_event_type(client, session["id"], "agent.tool_use")
    assert next(event for event in events if event["id"] == tool_use_id)["name"] == "bash"

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={
            "events": [
                {
                    "type": "user.tool_confirmation",
                    "tool_use_id": tool_use_id,
                    "result": "allow",
                    "deny_message": "not for allow",
                }
            ]
        },
    )
    assert response.status_code == 422, response.text

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.tool_confirmation", "tool_use_id": tool_use_id, "result": "allow"}]},
    )
    assert response.status_code == 200, response.text
    events = await _wait_for_event_type(client, session["id"], "agent.message")
    assert any("tool confirmation" in str(event.get("content")) for event in events if event["type"] == "agent.message")


async def test_active_session_states_block_mutations(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    sessions = {
        "running": await _create_session(client, agent, environment),
        "rescheduling": await _create_session(client, agent, environment),
    }

    for status, session in sessions.items():
        async with session_scope() as db:
            db_session = await sessions_q.get_session(db, session["id"])
            assert db_session is not None
            await sessions_q.update_session(db, db_session, status=status, stop_reason={"type": "in_progress"})
            await db.commit()

        response = await client.patch(
            f"/v1/sessions/{session['id']}",
            headers=TEST_HEADERS,
            json={"title": "blocked"},
        )
        assert response.status_code == 409

        response = await client.post(f"/v1/sessions/{session['id']}/archive", headers=TEST_HEADERS)
        assert response.status_code == 409

        response = await client.delete(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
        assert response.status_code == 409


async def test_user_interrupt_is_allowed_as_single_running_event(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    async with session_scope() as db:
        db_session = await sessions_q.get_session(db, session["id"])
        assert db_session is not None
        await sessions_q.update_session(db, db_session, status="running", stop_reason={"type": "in_progress"})
        await db.commit()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.interrupt"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["type"] == "user.interrupt"
    assert response.json()["data"][0]["processed_at"] is None

    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    interrupted = response.json()
    assert interrupted["status"] == "idle"
    assert interrupted["stop_reason"] == {"type": "interrupted"}

    events = await _wait_for_event_type(client, session["id"], "session.status_idle")
    idle_event = [event for event in events if event["type"] == "session.status_idle"][-1]
    assert idle_event["stop_reason"] == {"type": "interrupted"}


async def test_user_interrupt_must_be_single_event_while_running(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    async with session_scope() as db:
        db_session = await sessions_q.get_session(db, session["id"])
        assert db_session is not None
        await sessions_q.update_session(db, db_session, status="running", stop_reason={"type": "in_progress"})
        await db.commit()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.interrupt"}, {"type": "user.message", "content": "new work"}]},
    )
    assert response.status_code == 409
    assert "Cannot send events while session is running" in response.json()["error"]["message"]


async def test_session_local_agent_update_does_not_mutate_agent_version(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.patch(
        f"/v1/sessions/{session['id']}",
        headers=TEST_HEADERS,
        json={"agent": {"tools": [{"type": "custom", "name": "session_lookup"}]}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status_details"]["agent"]["tools"][0]["name"] == "session_lookup"

    response = await client.get(f"/v1/agents/{agent['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["tools"] == []
    assert response.json()["version"] == 1

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "use session tool"}]},
    )
    assert response.status_code == 200, response.text
    session = await _wait_for_stop_reason(client, session["id"], "requires_action")
    events = await _wait_for_event_type(client, session["id"], "agent.custom_tool_use")
    assert next(event for event in events if event["id"] == session["stop_reason"]["event_ids"][0])["name"] == "session_lookup"


async def test_file_session_resource_creates_session_scoped_copy(client):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("notes.txt", b"session notes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()

    agent = await _create_agent(client)
    environment = await _create_environment(client)
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "resources": [{"type": "file", "file_id": file["id"], "mount_path": "/workspace/notes.txt"}],
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    file_resource = next(resource for resource in session["resources"] if resource["type"] == "file")
    assert file_resource["file_id"] != file["id"]
    assert file_resource == {
        "id": file_resource["id"],
        "type": "file",
        "file_id": file_resource["file_id"],
        "mount_path": "/workspace/notes.txt",
        "created_at": file_resource["created_at"],
        "updated_at": file_resource["updated_at"],
    }

    async with session_scope() as db:
        resources = await res_q.list_resources(
            db,
            resource_type="session_resource",
            parent_id=session["id"],
            limit=10,
        )

    stored = resources[0].data["session_file"]
    assert stored["source_file_id"] == file["id"]
    assert stored["filename"] == "notes.txt"
    assert stored["storage"]["key"].startswith(f"workspaces/wrkspc_default/sessions_{session['id']}/resources/")

    response = await client.delete(f"/v1/files/{file['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/files/{file_resource['file_id']}/content", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.content == b"session notes"

    response = await client.delete(
        f"/v1/sessions/{session['id']}/resources/{file_resource['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text

    response = await client.get(f"/v1/files/{file_resource['file_id']}/content", headers=TEST_HEADERS)
    assert response.status_code == 404


async def test_session_file_resource_limit_is_enforced(client):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("limit.txt", b"limit", "text/plain")},
    )
    assert response.status_code == 201, response.text
    file = response.json()

    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    async with session_scope() as db:
        for index in range(100):
            await res_q.create_resource(
                db,
                resource_type="session_resource",
                parent_id=session["id"],
                name=file["id"],
                data={
                    "type": "file",
                    "file_id": file["id"],
                    "mount_path": f"/workspace/{index}.txt",
                    "read_only": True,
                },
            )
        await db.commit()

    response = await client.post(
        f"/v1/sessions/{session['id']}/resources",
        headers=TEST_HEADERS,
        json={"type": "file", "file_id": file["id"], "mount_path": "/workspace/overflow.txt"},
    )

    assert response.status_code == 422
    assert "at most 100 file resources" in response.json()["error"]["message"]


async def test_memory_store_session_resource_is_added_to_runtime_context(client):
    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "Customer context"},
    )
    assert response.status_code == 201, response.text
    memory_store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{memory_store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/customers/acme", "content": "ACME prefers email."},
    )
    assert response.status_code == 201, response.text

    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "resources": [
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store["id"],
                    "access": "read_only",
                    "instructions": "Use customer preferences.",
                }
            ],
        },
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "summarize customer context"}]},
    )
    assert response.status_code == 200, response.text

    events = await _wait_for_event_type(client, session["id"], "agent.message")
    agent_message = next(event for event in events if event["type"] == "agent.message")
    assert "ACME prefers email." in str(agent_message["content"])

    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    run_state = response.json()["run_state"]
    memory_context = run_state["memory_context"]["memory_stores"][0]
    assert memory_context["memory_store_id"] == memory_store["id"]
    assert memory_context["instructions"] == "Use customer preferences."
    assert memory_context["memories"][0]["content"] == "ACME prefers email."


async def test_memory_store_session_resource_limit_and_delete_guard(client):
    stores = []
    for index in range(9):
        response = await client.post(
            "/v1/memory_stores",
            headers=TEST_HEADERS,
            json={"name": f"Memory Store {index}"},
        )
        assert response.status_code == 201, response.text
        stores.append(response.json())

    agent = await _create_agent(client)
    environment = await _create_environment(client)

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "resources": [
                {"type": "memory_store", "memory_store_id": store["id"]}
                for store in stores[:8]
            ],
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    memory_resource = next(resource for resource in session["resources"] if resource["type"] == "memory_store")

    response = await client.delete(
        f"/v1/sessions/{session['id']}/resources/{memory_resource['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 422
    assert "cannot be removed after creation" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "resources": [
                {"type": "memory_store", "memory_store_id": store["id"]}
                for store in stores
            ],
        },
    )
    assert response.status_code == 422
    assert "at most 8 memory store resources" in response.json()["error"]["message"]


async def test_mcp_credentials_are_matched_from_session_vaults(client):
    mcp_server = {"type": "url", "name": "github", "url": "https://mcp.example.com/github"}
    agent = await _create_agent(
        client,
        tools=[{"type": "mcp_toolset", "mcp_server_name": "github"}],
        mcp_servers=[mcp_server],
    )
    environment = await _create_environment(client)

    response = await client.post("/v1/vaults", headers=TEST_HEADERS, json={"display_name": "MCP Vault"})
    assert response.status_code == 201, response.text
    vault = response.json()
    response = await client.post(
        f"/v1/vaults/{vault['id']}/credentials",
        headers=TEST_HEADERS,
        json={
            "display_name": "GitHub MCP",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/github/",
                "token": "secret-token",
            },
        },
    )
    assert response.status_code == 201, response.text
    credential = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": 1},
            "environment_id": environment["id"],
            "vault_ids": [vault["id"]],
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "use github mcp"}]},
    )
    assert response.status_code == 200, response.text

    await _wait_for_event_type(client, session["id"], "agent.mcp_tool_use")
    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    mcp_auth = response.json()["run_state"]["mcp_auth"]
    assert mcp_auth["errors"] == []
    assert mcp_auth["servers"][0]["status"] == "matched"
    assert mcp_auth["servers"][0]["credential_id"] == credential["id"]
    assert "token" not in str(mcp_auth)


async def test_missing_mcp_credentials_emit_session_error_without_blocking_session_create(client):
    mcp_server = {"type": "url", "name": "github", "url": "https://mcp.example.com/github"}
    agent = await _create_agent(
        client,
        tools=[{"type": "mcp_toolset", "mcp_server_name": "github"}],
        mcp_servers=[mcp_server],
    )
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "use github mcp"}]},
    )
    assert response.status_code == 200, response.text

    events = await _wait_for_event_type(client, session["id"], "agent.mcp_tool_use")
    errors = [event for event in events if event["type"] == "session.error" and event.get("error_type") == "mcp_auth_missing"]
    assert errors
    assert errors[0]["mcp_server_name"] == "github"

    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["status"] == "idle"
    assert session["stop_reason"]["type"] == "requires_action"
    assert session["run_state"]["mcp_auth"]["errors"][0]["type"] == "mcp_auth_missing"


async def test_define_outcome_emits_evaluation_span_and_session_summary(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={
            "events": [
                {
                    "type": "user.define_outcome",
                    "description": "Produce a customer-ready summary.",
                    "rubric": {"type": "text", "content": "Must include the customer."},
                    "max_iterations": 2,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text

    events = await _wait_for_event_type(client, session["id"], "span.outcome_evaluation_end")
    outcome_event = next(event for event in events if event["type"] == "span.outcome_evaluation_end")
    assert outcome_event["outcome"]["objective"] == "Produce a customer-ready summary."
    assert outcome_event["outcome"]["max_iterations"] == 2
    assert outcome_event["result"]["type"] == "deterministic_local_grader"
    assert outcome_event["result"]["passed"] is True

    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["outcome_evaluations"][0]["event_id"] == outcome_event["id"]
    assert session["outcome_evaluations"][0]["grader_context"]["max_iterations"] == 2


async def test_define_outcome_shape_is_validated(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    invalid_events = [
        {"type": "user.define_outcome", "rubric": {"type": "text", "content": "Pass."}},
        {"type": "user.define_outcome", "description": "Do work.", "rubric": {"must_include": ["work"]}},
        {
            "type": "user.define_outcome",
            "description": "Do work.",
            "rubric": {"type": "text", "content": "Pass."},
            "max_iterations": 21,
        },
    ]
    for event in invalid_events:
        response = await client.post(
            f"/v1/sessions/{session['id']}/events",
            headers=TEST_HEADERS,
            json={"events": [event]},
        )
        assert response.status_code == 422, response.text


async def test_primary_session_thread_archive_is_persisted(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.get(f"/v1/sessions/{session['id']}/threads", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    primary_thread = response.json()["data"][0]
    assert primary_thread["archived_at"] is None

    response = await client.post(
        f"/v1/sessions/{session['id']}/threads/{primary_thread['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    archived_at = response.json()["archived_at"]
    assert archived_at is not None

    response = await client.get(
        f"/v1/sessions/{session['id']}/threads/{primary_thread['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] == archived_at

    response = await client.get(f"/v1/sessions/{session['id']}/threads", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["archived_at"] == archived_at


async def test_primary_thread_events_include_unassigned_session_events(client):
    agent = await _create_agent(client)
    environment = await _create_environment(client)
    session = await _create_session(client, agent, environment)

    response = await client.get(f"/v1/sessions/{session['id']}/threads", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    primary_thread = response.json()["data"][0]

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "hello primary"}]},
    )
    assert response.status_code == 200, response.text
    await _wait_for_event_type(client, session["id"], "agent.message")

    response = await client.get(
        f"/v1/sessions/{session['id']}/threads/{primary_thread['id']}/events",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    types = [event["type"] for event in response.json()["data"]]
    assert "user.message" in types
    assert "agent.message" in types
    assert "session.status_running" in types


async def test_multiagent_session_creates_delegated_agent_threads(client):
    reviewer = await _create_agent(client)
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "self"}, {"type": "agent", "id": reviewer["id"]}],
            },
        },
    )
    assert response.status_code == 201, response.text
    coordinator = response.json()
    environment = await _create_environment(client)

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": coordinator["id"], "version": coordinator["version"]},
            "environment_id": environment["id"],
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()

    response = await client.get(f"/v1/sessions/{session['id']}/threads", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    threads = response.json()["data"]
    assert len(threads) == 3
    primary = threads[0]
    delegated_by_agent_id = {thread["agent"]["id"]: thread for thread in threads[1:]}
    delegated = delegated_by_agent_id[reviewer["id"]]
    self_delegated = delegated_by_agent_id[coordinator["id"]]
    assert primary["agent"]["id"] == coordinator["id"]
    assert delegated["parent_thread_id"] == primary["id"]
    assert delegated["agent"]["id"] == reviewer["id"]
    assert delegated["agent"]["version"] == reviewer["version"]
    assert self_delegated["parent_thread_id"] == primary["id"]
    assert self_delegated["agent"]["id"] == coordinator["id"]
    assert self_delegated["agent"]["version"] == coordinator["version"]

    response = await client.post(
        f"/v1/sessions/{session['id']}/threads/{delegated['id']}/archive",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is not None


async def test_delegated_thread_events_only_include_explicit_thread_events(client):
    reviewer = await _create_agent(client)
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Coordinator",
            "model": {"id": "gpt-5.5"},
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "self"}, {"type": "agent", "id": reviewer["id"]}],
            },
        },
    )
    assert response.status_code == 201, response.text
    coordinator = response.json()
    environment = await _create_environment(client)
    session = await _create_session(client, coordinator, environment)

    response = await client.get(f"/v1/sessions/{session['id']}/threads", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    threads = response.json()["data"]
    primary = threads[0]
    delegated = next(thread for thread in threads[1:] if thread["agent"]["id"] == reviewer["id"])

    response = await client.post(
        f"/v1/sessions/{session['id']}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "hello coordinator"}]},
    )
    assert response.status_code == 200, response.text
    await _wait_for_event_type(client, session["id"], "agent.message")

    response = await client.get(
        f"/v1/sessions/{session['id']}/threads/{delegated['id']}/events",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []

    response = await client.get(
        f"/v1/sessions/{session['id']}/threads/{primary['id']}/events",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert any(event["type"] == "user.message" for event in response.json()["data"])
