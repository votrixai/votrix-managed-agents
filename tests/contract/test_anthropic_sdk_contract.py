from contextlib import asynccontextmanager
import asyncio
import hashlib
import json

import httpx
import pytest
from httpx import ASGITransport

anthropic = pytest.importorskip("anthropic")

from anthropic import AsyncAnthropic  # noqa: E402
from anthropic._streaming import SSEDecoder  # noqa: E402
from anthropic.types.beta.sessions.beta_managed_agents_stream_session_events import (  # noqa: E402
    BetaManagedAgentsStreamSessionEvents,
)
from tests.conftest import TEST_API_KEY  # noqa: E402

pytestmark = pytest.mark.contract


MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
BETA_KWARG = {"betas": [MANAGED_AGENTS_BETA]}


def assert_epoch_microsecond_version(value: str | None) -> int:
    assert value is not None
    assert value.isdigit()
    assert len(value) >= 16
    return int(value)


@asynccontextmanager
async def anthropic_client():
    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        sdk = AsyncAnthropic(
            api_key=TEST_API_KEY,
            base_url="http://testserver",
            default_headers={
                "anthropic-beta": MANAGED_AGENTS_BETA,
                "anthropic-version": "2023-06-01",
            },
            http_client=http_client,
            max_retries=0,
            _strict_response_validation=True,
        )
        yield sdk, http_client


def test_anthropic_sdk_exposes_expected_managed_agents_surface():
    client = AsyncAnthropic(api_key=TEST_API_KEY)

    expected = {
        "agents": {"create", "retrieve", "update", "list", "archive", "versions"},
        "environments": {"create", "retrieve", "update", "list", "delete", "archive", "work"},
        "sessions": {"create", "retrieve", "update", "list", "delete", "archive", "events", "resources", "threads"},
        "skills": {"create", "retrieve", "list", "delete", "versions"},
        "files": {"upload", "retrieve_metadata", "list", "download", "delete"},
        "vaults": {"create", "retrieve", "update", "list", "delete", "archive", "credentials"},
        "memory_stores": {"create", "retrieve", "update", "list", "delete", "archive"},
        "deployments": {"create", "retrieve", "update", "list", "archive", "pause", "unpause", "run"},
        "deployment_runs": {"retrieve", "list"},
        "user_profiles": {"create", "retrieve", "update", "list", "create_enrollment_url"},
    }

    for resource_name, methods in expected.items():
        resource = getattr(client.beta, resource_name)
        missing = methods - set(dir(resource))
        assert not missing, f"{resource_name} missing SDK methods: {sorted(missing)}"


async def test_anthropic_sdk_agent_crud_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(
            name="SDK Contract Agent",
            model={"id": "gpt-5.5"},
            system="Be precise.",
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            **BETA_KWARG,
        )
        assert agent.type == "agent"
        assert agent.version == 1
        assert agent.model.id == "gpt-5.5"

        retrieved = await client.beta.agents.retrieve(agent.id, **BETA_KWARG)
        assert retrieved.id == agent.id

        updated = await client.beta.agents.update(
            agent.id,
            version=agent.version,
            metadata={"team": "contract"},
            **BETA_KWARG,
        )
        assert updated.version == 2
        assert updated.metadata["team"] == "contract"

        listed = [item async for item in client.beta.agents.list(limit=20, **BETA_KWARG)]
        assert any(item.id == agent.id for item in listed)

        reviewer = await client.beta.agents.create(name="SDK Reviewer Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        coordinator = await client.beta.agents.create(
            name="SDK Coordinator Agent",
            model={"id": "gpt-5.5"},
            multiagent={"type": "coordinator", "agents": [reviewer.id, {"type": "self"}]},
            **BETA_KWARG,
        )
        roster = [entry.model_dump() for entry in coordinator.multiagent.agents]
        assert roster[0]["id"] == reviewer.id
        assert roster[0]["version"] == reviewer.version
        assert roster[1]["id"] == coordinator.id
        assert roster[1]["version"] == coordinator.version

        archived = await client.beta.agents.archive(agent.id, **BETA_KWARG)
        assert archived.archived_at is not None


async def test_anthropic_sdk_agent_tool_response_defaults_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(
            name="SDK Tool Defaults Agent",
            model={"id": "gpt-5.5"},
            tools=[{"type": "agent_toolset_20260401"}],
            **BETA_KWARG,
        )
        assert agent.tools[0].type == "agent_toolset_20260401"
        assert agent.tools[0].configs == []
        assert agent.tools[0].default_config.enabled is True
        assert agent.tools[0].default_config.permission_policy.type == "always_allow"

        mcp_agent = await client.beta.agents.create(
            name="SDK MCP Tool Defaults Agent",
            model={"id": "gpt-5.5"},
            mcp_servers=[{"type": "url", "name": "github", "url": "https://mcp.example.com/github"}],
            tools=[{"type": "mcp_toolset", "mcp_server_name": "github"}],
            **BETA_KWARG,
        )
        assert mcp_agent.mcp_servers[0].name == "github"
        assert mcp_agent.tools[0].type == "mcp_toolset"
        assert mcp_agent.tools[0].configs == []
        assert mcp_agent.tools[0].default_config.enabled is True
        assert mcp_agent.tools[0].default_config.permission_policy.type == "always_ask"

        environment = await client.beta.environments.create(
            name="SDK Tool Defaults Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            **BETA_KWARG,
        )
        updated = await client.beta.sessions.update(
            session.id,
            agent={"tools": [{"type": "custom", "name": "lookup_minimal"}], "mcp_servers": []},
            **BETA_KWARG,
        )
        assert updated.agent.tools[0].type == "custom"
        assert updated.agent.tools[0].description == "Custom tool lookup_minimal."
        assert updated.agent.tools[0].input_schema.type == "object"


async def test_anthropic_sdk_files_contract():
    async with anthropic_client() as (client, _):
        uploaded = await client.beta.files.upload(
            file=("hello.txt", b"hello world", "text/plain"),
            **BETA_KWARG,
        )
        assert uploaded.type == "file"
        assert uploaded.filename == "hello.txt"
        assert uploaded.mime_type == "text/plain"
        assert uploaded.size_bytes == 11

        metadata = await client.beta.files.retrieve_metadata(uploaded.id, **BETA_KWARG)
        assert metadata.id == uploaded.id

        listed = [item async for item in client.beta.files.list(limit=20, **BETA_KWARG)]
        assert any(item.id == uploaded.id for item in listed)

        download = await client.beta.files.download(uploaded.id, **BETA_KWARG)
        assert await download.read() == b"hello world"

        deleted = await client.beta.files.delete(uploaded.id, **BETA_KWARG)
        assert deleted.id == uploaded.id
        assert deleted.type == "file_deleted"


async def test_anthropic_sdk_agent_versions_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(
            name="SDK Versions Agent",
            model={"id": "gpt-5.5"},
            system="v1",
            **BETA_KWARG,
        )
        await client.beta.agents.update(agent.id, version=agent.version, system="v2", **BETA_KWARG)
        versions = [item async for item in client.beta.agents.versions.list(agent.id, limit=20, **BETA_KWARG)]

        assert [item.version for item in versions] == [2, 1]
        assert all(item.type == "agent" for item in versions)
        assert all(item.id == agent.id for item in versions)

        retrieved_v1 = await client.beta.agents.retrieve(agent.id, version=1, **BETA_KWARG)
        retrieved_latest = await client.beta.agents.retrieve(agent.id, **BETA_KWARG)
        assert retrieved_v1.version == 1
        assert retrieved_v1.system == "v1"
        assert retrieved_latest.version == 2
        assert retrieved_latest.system == "v2"


async def test_anthropic_sdk_environment_contract():
    async with anthropic_client() as (client, _):
        environment = await client.beta.environments.create(
            name="SDK Contract Environment",
            description="Contract environment.",
            metadata={"keep": "yes", "drop": "soon"},
            config={"type": "cloud"},
            **BETA_KWARG,
        )

        assert environment.type == "environment"
        assert environment.description == "Contract environment."
        assert environment.config.type == "cloud"
        assert environment.config.networking.type == "unrestricted"
        assert environment.config.packages.pip == []

        retrieved = await client.beta.environments.retrieve(environment.id, **BETA_KWARG)
        assert retrieved.id == environment.id

        updated = await client.beta.environments.update(
            environment.id,
            name="SDK Contract Environment Updated",
            description="Updated environment.",
            metadata={"drop": None, "added": "yes"},
            **BETA_KWARG,
        )
        assert updated.name == "SDK Contract Environment Updated"
        assert updated.description == "Updated environment."
        assert updated.metadata["keep"] == "yes"
        assert updated.metadata["added"] == "yes"
        assert "drop" not in updated.metadata

        listed = [item async for item in client.beta.environments.list(limit=20, **BETA_KWARG)]
        assert any(item.id == environment.id for item in listed)

        archived = await client.beta.environments.archive(environment.id, **BETA_KWARG)
        assert archived.archived_at is not None

        deletable = await client.beta.environments.create(
            name="SDK Contract Environment Delete",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        deleted = await client.beta.environments.delete(deletable.id, **BETA_KWARG)
        assert deleted.id == deletable.id
        assert deleted.type == "environment_deleted"

        scoped_environment = await client.beta.environments.create(
            name="SDK Scoped Environment",
            config={"type": "self_hosted"},
            scope="account",
            **BETA_KWARG,
        )
        assert scoped_environment.scope == "account"
        assert "_scope" not in scoped_environment.config.model_dump()

        updated_scoped_environment = await client.beta.environments.update(
            scoped_environment.id,
            scope="organization",
            **BETA_KWARG,
        )
        assert updated_scoped_environment.scope == "organization"


async def test_anthropic_sdk_environment_work_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Work Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Work Environment",
            config={"type": "self_hosted"},
            **BETA_KWARG,
        )
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            **BETA_KWARG,
        )
        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": "Queue work."}]}],
            **BETA_KWARG,
        )

        work = await client.beta.environments.work.poll(
            environment.id,
            anthropic_worker_id="sdk-worker-1",
            block_ms=1,
            reclaim_older_than_ms=5000,
            **BETA_KWARG,
        )
        assert work is not None
        assert work.type == "work"
        assert work.state == "starting"
        assert work.environment_id == environment.id
        assert work.data.type == "session"
        assert work.data.id == session.id

        listed_work = [item async for item in client.beta.environments.work.list(environment.id, limit=20, **BETA_KWARG)]
        assert any(item.id == work.id for item in listed_work)

        retrieved_work = await client.beta.environments.work.retrieve(work.id, environment_id=environment.id, **BETA_KWARG)
        assert retrieved_work.id == work.id

        updated_work = await client.beta.environments.work.update(
            work.id,
            environment_id=environment.id,
            metadata={"phase": "sdk"},
            **BETA_KWARG,
        )
        assert updated_work.metadata["phase"] == "sdk"

        acked_work = await client.beta.environments.work.ack(work.id, environment_id=environment.id, **BETA_KWARG)
        assert acked_work.state == "active"

        heartbeat = await client.beta.environments.work.heartbeat(
            work.id,
            environment_id=environment.id,
            desired_ttl_seconds=30,
            expected_last_heartbeat="NO_HEARTBEAT",
            extra_query={
                "worker_id": "sdk-worker-1",
                "lease_id": work.model_extra["lease"]["lease_id"],
            },
            **BETA_KWARG,
        )
        assert heartbeat.type == "work_heartbeat"
        assert heartbeat.state == "active"
        assert heartbeat.lease_extended is True
        assert heartbeat.ttl_seconds == 30
        assert heartbeat.last_heartbeat is not None

        stats = await client.beta.environments.work.stats(environment.id, **BETA_KWARG)
        assert stats.type == "work_queue_stats"
        assert stats.pending >= 1

        stopped_work = await client.beta.environments.work.stop(
            work.id,
            environment_id=environment.id,
            force=True,
            **BETA_KWARG,
        )
        assert stopped_work.state == "stopped"


async def test_anthropic_sdk_session_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Session Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Session Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        uploaded = await client.beta.files.upload(
            file=("session-resource.txt", b"resource", "text/plain"),
            **BETA_KWARG,
        )
        memory_store = await client.beta.memory_stores.create(
            name="SDK Session Memory",
            description="Session memory.",
            **BETA_KWARG,
        )
        vault = await client.beta.vaults.create(display_name="SDK Session Vault", **BETA_KWARG)

        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            vault_ids=[vault.id, vault.id],
            resources=[
                {
                    "type": "file",
                    "file_id": uploaded.id,
                    "mount_path": "/workspace/session-resource.txt",
                },
                {
                    "type": "github_repository",
                    "url": "https://github.com/example/repo",
                    "mount_path": "/workspace/repo",
                    "authorization_token": "ghp_secret",
                    "checkout": {"type": "branch", "name": "main"},
                },
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store.id,
                    "access": "read_only",
                    "instructions": "Use this store as long-term project context.",
                },
            ],
            **BETA_KWARG,
        )

        assert session.type == "session"
        assert session.agent.id == agent.id
        assert session.agent.version == agent.version
        assert {resource.type for resource in session.resources} == {"file", "github_repository", "memory_store"}
        initial_resources_by_type = {resource.type: resource for resource in session.resources}
        assert initial_resources_by_type["file"].file_id != uploaded.id
        assert initial_resources_by_type["github_repository"].url == "https://github.com/example/repo"
        assert initial_resources_by_type["github_repository"].checkout.name == "main"
        assert "authorization_token" not in initial_resources_by_type["github_repository"].model_dump()
        assert initial_resources_by_type["memory_store"].memory_store_id == memory_store.id
        assert initial_resources_by_type["memory_store"].access == "read_only"
        assert session.outcome_evaluations == []
        assert session.vault_ids == [vault.id]
        assert session.stats is not None
        assert session.usage is not None

        sent = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": "Use the contract context."}],
                },
                {
                    "type": "system.message",
                    "content": [{"type": "text", "text": "Contract context."}],
                }
            ],
            **BETA_KWARG,
        )
        assert [event.type for event in sent.data] == ["user.message", "system.message"]

        events = [item async for item in client.beta.sessions.events.list(session.id, limit=20, **BETA_KWARG)]
        assert any(item.type == "session.status_idle" for item in events)
        assert any(item.type == "system.message" for item in events)

        system_events = [
            item
            async for item in client.beta.sessions.events.list(
                session.id,
                types=["system.message"],
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert system_events
        assert {item.type for item in system_events} == {"system.message"}

        uploaded = await client.beta.files.upload(
            file=("session-resource.txt", b"resource", "text/plain"),
            **BETA_KWARG,
        )
        resource = await client.beta.sessions.resources.add(
            session.id,
            file_id=uploaded.id,
            type="file",
            mount_path="/mnt/session/uploads/session-resource.txt",
            **BETA_KWARG,
        )
        assert resource.type == "file"
        assert resource.file_id != uploaded.id

        retrieved_resource = await client.beta.sessions.resources.retrieve(
            resource.id,
            session_id=session.id,
            **BETA_KWARG,
        )
        assert retrieved_resource.id == resource.id

        resources = [item async for item in client.beta.sessions.resources.list(session.id, limit=20, **BETA_KWARG)]
        assert any(item.id == resource.id for item in resources)
        assert any(item.type == "github_repository" for item in resources)
        assert any(item.type == "memory_store" and item.memory_store_id == memory_store.id for item in resources)

        scoped_files = [item async for item in client.beta.files.list(scope_id=session.id, limit=20, **BETA_KWARG)]
        scoped_file = next(item for item in scoped_files if item.id == resource.file_id)
        assert scoped_file.scope is not None
        assert scoped_file.scope.type == "session"
        assert scoped_file.scope.id == session.id

        updated_resource = await client.beta.sessions.resources.update(
            initial_resources_by_type["github_repository"].id,
            session_id=session.id,
            authorization_token="ghp_rotated_secret",
            **BETA_KWARG,
        )
        assert updated_resource.id == initial_resources_by_type["github_repository"].id
        assert updated_resource.type == "github_repository"
        assert "authorization_token" not in updated_resource.model_dump()

        deleted_resource = await client.beta.sessions.resources.delete(
            resource.id,
            session_id=session.id,
            **BETA_KWARG,
        )
        assert deleted_resource.id == resource.id
        assert deleted_resource.type == "session_resource_deleted"

        threads = [item async for item in client.beta.sessions.threads.list(session.id, limit=20, **BETA_KWARG)]
        assert len(threads) >= 1
        primary_thread = threads[0]
        assert primary_thread.type == "session_thread"
        assert primary_thread.session_id == session.id
        assert primary_thread.agent.id == agent.id

        retrieved_thread = await client.beta.sessions.threads.retrieve(
            primary_thread.id,
            session_id=session.id,
            **BETA_KWARG,
        )
        assert retrieved_thread.id == primary_thread.id

        thread_events = [
            item
            async for item in client.beta.sessions.threads.events.list(
                primary_thread.id,
                session_id=session.id,
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert any(item.type == "session.status_idle" for item in thread_events)
        assert any(item.type == "system.message" for item in thread_events)

        archived_thread = await client.beta.sessions.threads.archive(
            primary_thread.id,
            session_id=session.id,
            **BETA_KWARG,
        )
        assert archived_thread.archived_at is not None

        retrieved = await client.beta.sessions.retrieve(session.id, **BETA_KWARG)
        assert retrieved.id == session.id

        updated = await client.beta.sessions.update(
            session.id,
            title="Updated SDK Session",
            metadata={"phase": "contract"},
            **BETA_KWARG,
        )
        assert updated.title == "Updated SDK Session"
        assert updated.metadata["phase"] == "contract"

        session_agent_updated = await client.beta.sessions.update(
            session.id,
            agent={
                "tools": [
                    {
                        "type": "custom",
                        "name": "lookup_contract_case",
                        "description": "Look up a contract case by ID.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"case_id": {"type": "string"}},
                            "required": ["case_id"],
                        },
                    }
                ],
                "mcp_servers": [],
            },
            **BETA_KWARG,
        )
        assert session_agent_updated.agent.tools[0].type == "custom"
        assert session_agent_updated.agent.tools[0].name == "lookup_contract_case"
        assert session_agent_updated.agent.mcp_servers == []
        assert session_agent_updated.agent.version == agent.version

        retrieved_after_agent_update = await client.beta.sessions.retrieve(session.id, **BETA_KWARG)
        assert retrieved_after_agent_update.agent.tools[0].name == "lookup_contract_case"

        original_agent = await client.beta.agents.retrieve(agent.id, **BETA_KWARG)
        assert original_agent.tools == []

        session_agent_cleared = await client.beta.sessions.update(
            session.id,
            agent={"tools": [], "mcp_servers": []},
            **BETA_KWARG,
        )
        assert session_agent_cleared.agent.tools == []
        assert session_agent_cleared.agent.mcp_servers == []

        listed = [item async for item in client.beta.sessions.list(limit=20, **BETA_KWARG)]
        assert any(item.id == session.id for item in listed)

        archived = await client.beta.sessions.archive(session.id, **BETA_KWARG)
        assert archived.archived_at is not None

        deletable = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            **BETA_KWARG,
        )
        deleted = await client.beta.sessions.delete(deletable.id, **BETA_KWARG)
        assert deleted.id == deletable.id
        assert deleted.type == "session_deleted"


async def test_anthropic_sdk_session_agent_with_overrides_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(
            name="SDK Override Base Agent",
            model={"id": "base-model"},
            system="base system",
            **BETA_KWARG,
        )
        environment = await client.beta.environments.create(
            name="SDK Override Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )

        session = await client.beta.sessions.create(
            agent={
                "type": "agent_with_overrides",
                "id": agent.id,
                "version": agent.version,
                "model": {"id": "override-model"},
                "system": None,
                "tools": [{"type": "custom", "name": "override_lookup"}],
                "mcp_servers": [],
                "skills": [],
            },
            environment_id=environment.id,
            **BETA_KWARG,
        )

        assert session.agent.id == agent.id
        assert session.agent.version == agent.version
        assert session.agent.model.id == "override-model"
        assert session.agent.system is None
        assert session.agent.tools[0].name == "override_lookup"
        assert session.agent.mcp_servers == []
        assert session.agent.skills == []

        threads = [item async for item in client.beta.sessions.threads.list(session.id, **BETA_KWARG)]
        assert threads[0].agent.model.id == "override-model"
        assert threads[0].agent.system is None

        unchanged = await client.beta.agents.retrieve(agent.id, **BETA_KWARG)
        assert unchanged.version == agent.version
        assert unchanged.model.id == "base-model"
        assert unchanged.system == "base system"


async def test_anthropic_sdk_session_agent_string_pins_latest_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Session Short Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        agent_v2 = await client.beta.agents.update(agent.id, version=agent.version, system="v2", **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Session Short Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )

        session = await client.beta.sessions.create(
            agent=agent.id,
            environment_id=environment.id,
            **BETA_KWARG,
        )
        assert session.agent_version == agent_v2.version
        assert session.agent.version == agent_v2.version


async def test_anthropic_sdk_session_event_stream_parser_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Stream Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Stream Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            **BETA_KWARG,
        )

        first_event = [item async for item in client.beta.sessions.events.list(session.id, limit=1, **BETA_KWARG)][0]
        event_payload = first_event.model_dump(mode="json")
        sse_frame = (
            f"id: {event_payload['seq']}\n"
            f"event: {event_payload['type']}\n"
            f"data: {json.dumps(event_payload, separators=(',', ':'))}\n\n"
        )
        [raw_sse] = list(SSEDecoder().iter_bytes(iter([sse_frame.encode("utf-8")])))
        data = raw_sse.json()
        if "type" not in data:
            data["type"] = raw_sse.event
        parsed_event = client._process_response_data(
            data=data,
            cast_to=BetaManagedAgentsStreamSessionEvents,
            response=httpx.Response(200, request=httpx.Request("GET", "http://testserver")),
        )

        assert parsed_event.type == "session.status_idle"
        assert parsed_event.session_id == session.id


async def test_anthropic_sdk_user_tool_result_event_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(
            name="SDK Tool Result Agent",
            model={"id": "gpt-5.5"},
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_ask"},
                    },
                }
            ],
            **BETA_KWARG,
        )
        environment = await client.beta.environments.create(
            name="SDK Tool Result Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent.id, "version": agent.version},
            environment_id=environment.id,
            **BETA_KWARG,
        )

        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": "Run a tool."}]}],
            **BETA_KWARG,
        )

        stop_reason = None
        for _ in range(30):
            current = await client.beta.sessions.retrieve(session.id, **BETA_KWARG)
            if hasattr(current.stop_reason, "model_dump"):
                payload = current.stop_reason.model_dump(mode="json")
            else:
                payload = current.stop_reason
            if payload and payload.get("type") == "requires_action":
                stop_reason = payload
                break
            await asyncio.sleep(0.05)
        assert stop_reason is not None
        tool_use_id = stop_reason["event_ids"][0]

        sent = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": "Tool result from SDK."}],
                }
            ],
            **BETA_KWARG,
        )
        assert sent.data[0].type == "user.tool_result"


async def test_anthropic_sdk_skills_contract():
    async with anthropic_client() as (client, _):
        skill = await client.beta.skills.create(
            display_title="Contract Skill",
            files=[
                (
                    "skill/SKILL.md",
                    b"---\nname: contract\ndescription: Contract skill.\n---\nUse the contract.",
                    "text/markdown",
                )
            ],
            **BETA_KWARG,
        )

        assert skill.type == "skill"
        assert skill.source == "custom"
        first_version = assert_epoch_microsecond_version(skill.latest_version)

        retrieved = await client.beta.skills.retrieve(skill.id, **BETA_KWARG)
        assert retrieved.id == skill.id
        assert retrieved.latest_version == skill.latest_version

        listed = [item async for item in client.beta.skills.list(limit=20, **BETA_KWARG)]
        assert any(item.id == skill.id for item in listed)

        skilled_agent = await client.beta.agents.create(
            name="SDK Skilled Agent",
            model={"id": "gpt-5.5"},
            skills=[
                {"type": "custom", "skill_id": skill.id, "version": skill.latest_version},
                {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
            ],
            **BETA_KWARG,
        )
        assert [(item.type, item.skill_id, item.version) for item in skilled_agent.skills] == [
            ("custom", skill.id, skill.latest_version),
            ("anthropic", "xlsx", "latest"),
        ]

        version = await client.beta.skills.versions.create(
            skill.id,
            files=[
                (
                    "skill/SKILL.md",
                    b"---\nname: contract\ndescription: Contract skill v2.\n---\nUse the contract.",
                    "text/markdown",
                )
            ],
            **BETA_KWARG,
        )
        assert version.type == "skill_version"
        assert version.skill_id == skill.id
        assert assert_epoch_microsecond_version(version.version) > first_version
        assert version.directory == "skill"

        retrieved_version = await client.beta.skills.versions.retrieve(
            version.version,
            skill_id=skill.id,
            **BETA_KWARG,
        )
        assert retrieved_version.id == version.id

        versions = [item async for item in client.beta.skills.versions.list(skill.id, limit=20, **BETA_KWARG)]
        assert any(item.version == version.version for item in versions)

        download = await client.beta.skills.versions.download(version.version, skill_id=skill.id, **BETA_KWARG)
        assert b"Contract skill v2." in await download.read()

        deleted_version = await client.beta.skills.versions.delete(version.version, skill_id=skill.id, **BETA_KWARG)
        assert deleted_version.id == version.version
        assert deleted_version.type == "skill_version_deleted"

        deleted_skill = await client.beta.skills.delete(skill.id, **BETA_KWARG)
        assert deleted_skill.id == skill.id
        assert deleted_skill.type == "skill_deleted"


async def test_anthropic_sdk_vaults_and_credentials_contract():
    async with anthropic_client() as (client, _):
        vault = await client.beta.vaults.create(
            display_name="SDK Contract Vault",
            metadata={"keep": "yes", "drop": "soon"},
            **BETA_KWARG,
        )
        assert vault.type == "vault"
        assert vault.display_name == "SDK Contract Vault"
        assert vault.metadata["keep"] == "yes"

        updated = await client.beta.vaults.update(
            vault.id,
            display_name="SDK Contract Vault Updated",
            metadata={"drop": None, "added": "yes"},
            **BETA_KWARG,
        )
        assert updated.display_name == "SDK Contract Vault Updated"
        assert updated.metadata["added"] == "yes"
        assert "drop" not in updated.metadata

        credential = await client.beta.vaults.credentials.create(
            vault.id,
            display_name="Linear MCP",
            metadata={"kind": "mcp"},
            auth={
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.invalid",
                "access_token": "secret-token",
                "refresh": {
                    "client_id": "sdk-client",
                    "refresh_token": "sdk-refresh-secret",
                    "token_endpoint": "https://auth.example.invalid/token",
                    "token_endpoint_auth": {
                        "type": "client_secret_post",
                        "client_secret": "sdk-client-secret",
                    },
                    "scope": "read",
                },
            },
            **BETA_KWARG,
        )
        assert credential.type == "vault_credential"
        assert credential.vault_id == vault.id
        assert credential.auth.type == "mcp_oauth"
        assert credential.auth.mcp_server_url == "https://mcp.example.invalid"
        assert credential.auth.refresh is not None
        assert credential.auth.refresh.client_id == "sdk-client"
        assert credential.auth.refresh.token_endpoint_auth.type == "client_secret_post"
        auth_dump = str(credential.auth.model_dump())
        assert "secret-token" not in auth_dump
        assert "sdk-refresh-secret" not in auth_dump
        assert "sdk-client-secret" not in auth_dump

        retrieved = await client.beta.vaults.credentials.retrieve(credential.id, vault_id=vault.id, **BETA_KWARG)
        assert retrieved.id == credential.id

        credential_updated = await client.beta.vaults.credentials.update(
            credential.id,
            vault_id=vault.id,
            display_name="Linear MCP Updated",
            metadata={"kind": None, "team": "sdk"},
            **BETA_KWARG,
        )
        assert credential_updated.display_name == "Linear MCP Updated"
        assert credential_updated.metadata["team"] == "sdk"
        assert "kind" not in credential_updated.metadata

        validation = await client.beta.vaults.credentials.mcp_oauth_validate(
            credential.id,
            vault_id=vault.id,
            **BETA_KWARG,
        )
        assert validation.type == "vault_credential_validation"
        assert validation.status == "unknown"
        assert validation.vault_id == vault.id

        env_credential = await client.beta.vaults.credentials.create(
            vault.id,
            display_name="SDK Env Token",
            auth={
                "type": "environment_variable",
                "secret_name": "SDK_TOKEN",
                "secret_value": "secret-token",
                "networking": {"type": "limited", "allowed_hosts": ["api.example.invalid"]},
            },
            **BETA_KWARG,
        )
        assert env_credential.auth.type == "environment_variable"
        assert env_credential.auth.secret_name == "SDK_TOKEN"
        assert env_credential.auth.networking.type == "limited"
        assert env_credential.auth.injection_location.body is True
        assert env_credential.auth.injection_location.header is True
        assert "secret_value" not in env_credential.auth.model_dump()

        env_credential_updated = await client.beta.vaults.credentials.update(
            env_credential.id,
            vault_id=vault.id,
            auth={
                "type": "environment_variable",
                "secret_value": "rotated-secret",
                "networking": {"type": "unrestricted"},
            },
            **BETA_KWARG,
        )
        assert env_credential_updated.auth.type == "environment_variable"
        assert env_credential_updated.auth.secret_name == "SDK_TOKEN"
        assert env_credential_updated.auth.networking.type == "unrestricted"

        credentials = [item async for item in client.beta.vaults.credentials.list(vault.id, limit=20, **BETA_KWARG)]
        assert any(item.id == credential.id for item in credentials)
        assert any(item.id == env_credential.id for item in credentials)

        archived_credential = await client.beta.vaults.credentials.archive(
            credential.id,
            vault_id=vault.id,
            **BETA_KWARG,
        )
        assert archived_credential.archived_at is not None

        deletable_credential = await client.beta.vaults.credentials.create(
            vault.id,
            display_name="Delete me",
            auth={
                "type": "static_bearer",
                "mcp_server_url": "https://delete.example.invalid",
                "token": "secret-token",
            },
            **BETA_KWARG,
        )
        deleted_credential = await client.beta.vaults.credentials.delete(
            deletable_credential.id,
            vault_id=vault.id,
            **BETA_KWARG,
        )
        assert deleted_credential.id == deletable_credential.id
        assert deleted_credential.type == "vault_credential_deleted"

        vaults = [item async for item in client.beta.vaults.list(limit=20, **BETA_KWARG)]
        assert any(item.id == vault.id for item in vaults)

        archived_vault = await client.beta.vaults.archive(vault.id, **BETA_KWARG)
        assert archived_vault.archived_at is not None

        deletable_vault = await client.beta.vaults.create(display_name="Delete Vault", **BETA_KWARG)
        deleted_vault = await client.beta.vaults.delete(deletable_vault.id, **BETA_KWARG)
        assert deleted_vault.id == deletable_vault.id
        assert deleted_vault.type == "vault_deleted"


async def test_anthropic_sdk_memory_stores_contract():
    async with anthropic_client() as (client, _):
        store = await client.beta.memory_stores.create(
            name="SDK Contract Memory",
            description="Contract memory store.",
            metadata={"keep": "yes", "drop": "soon"},
            **BETA_KWARG,
        )
        assert store.type == "memory_store"
        assert store.description == "Contract memory store."

        updated_store = await client.beta.memory_stores.update(
            store.id,
            description="Updated memory store.",
            metadata={"drop": None, "added": "yes"},
            **BETA_KWARG,
        )
        assert updated_store.description == "Updated memory store."
        assert updated_store.metadata["added"] == "yes"
        assert "drop" not in updated_store.metadata

        content = "ACME prefers email."
        memory = await client.beta.memory_stores.memories.create(
            store.id,
            path="/accounts/acme.md",
            content=content,
            view="full",
            **BETA_KWARG,
        )
        assert memory.type == "memory"
        assert memory.memory_store_id == store.id
        assert memory.path == "/accounts/acme.md"
        assert memory.content == content
        assert memory.content_sha256 == hashlib.sha256(content.encode()).hexdigest()
        assert memory.memory_version_id.startswith("memver_")

        retrieved = await client.beta.memory_stores.memories.retrieve(
            memory.id,
            memory_store_id=store.id,
            view="full",
            **BETA_KWARG,
        )
        assert retrieved.id == memory.id

        updated_content = "ACME prefers email and quarterly reviews."
        updated_memory = await client.beta.memory_stores.memories.update(
            memory.id,
            memory_store_id=store.id,
            content=updated_content,
            precondition={"type": "content_sha256", "content_sha256": memory.content_sha256},
            view="full",
            **BETA_KWARG,
        )
        assert updated_memory.content == updated_content
        assert updated_memory.content_sha256 == hashlib.sha256(updated_content.encode()).hexdigest()

        noop_memory = await client.beta.memory_stores.memories.update(
            memory.id,
            memory_store_id=store.id,
            content=updated_content,
            precondition={"type": "content_sha256", "content_sha256": memory.content_sha256},
            view="full",
            **BETA_KWARG,
        )
        assert noop_memory.model_dump().get("version") == updated_memory.model_dump().get("version")
        assert noop_memory.content == updated_content

        memories = [
            item
            async for item in client.beta.memory_stores.memories.list(
                store.id,
                path_prefix="/accounts/",
                view="full",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert any(item.id == memory.id for item in memories)

        versions = [
            item
            async for item in client.beta.memory_stores.memory_versions.list(
                store.id,
                memory_id=memory.id,
                view="full",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert [item.operation for item in versions] == ["modified", "created"]
        assert versions[0].content == updated_content

        modified_versions = [
            item
            async for item in client.beta.memory_stores.memory_versions.list(
                store.id,
                operation="modified",
                view="full",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert [item.id for item in modified_versions] == [versions[0].id]

        filter_memory = await client.beta.memory_stores.memories.create(
            store.id,
            path="/filters/sdk.md",
            content="filterable memory",
            view="full",
            extra_body={"actor": "sdk-filter-key", "session_id": "sess_sdk_filter"},
            **BETA_KWARG,
        )
        filtered_versions = [
            item
            async for item in client.beta.memory_stores.memory_versions.list(
                store.id,
                api_key_id="sdk-filter-key",
                session_id="sess_sdk_filter",
                view="basic",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert [item.memory_id for item in filtered_versions] == [filter_memory.id]
        assert filtered_versions[0].content is None

        retrieved_version = await client.beta.memory_stores.memory_versions.retrieve(
            versions[0].id,
            memory_store_id=store.id,
            view="full",
            **BETA_KWARG,
        )
        assert retrieved_version.id == versions[0].id

        redacted_version = await client.beta.memory_stores.memory_versions.redact(
            versions[1].id,
            memory_store_id=store.id,
            **BETA_KWARG,
        )
        assert redacted_version.redacted_at is not None
        assert redacted_version.content is None

        deleted_memory = await client.beta.memory_stores.memories.delete(
            memory.id,
            memory_store_id=store.id,
            expected_content_sha256=updated_memory.content_sha256,
            **BETA_KWARG,
        )
        assert deleted_memory.id == memory.id
        assert deleted_memory.type == "memory_deleted"

        post_delete_versions = [
            item
            async for item in client.beta.memory_stores.memory_versions.list(
                store.id,
                memory_id=memory.id,
                view="full",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert [item.operation for item in post_delete_versions] == ["deleted", "modified", "created"]
        assert post_delete_versions[0].content is None
        assert post_delete_versions[0].content_sha256 is None
        assert post_delete_versions[0].content_size_bytes is None
        assert post_delete_versions[0].path == "/accounts/acme.md"
        retrieved_deleted_version = await client.beta.memory_stores.memory_versions.retrieve(
            post_delete_versions[0].id,
            memory_store_id=store.id,
            view="full",
            **BETA_KWARG,
        )
        assert retrieved_deleted_version.operation == "deleted"

        stores = [item async for item in client.beta.memory_stores.list(limit=20, **BETA_KWARG)]
        assert any(item.id == store.id for item in stores)

        archived_store = await client.beta.memory_stores.archive(store.id, **BETA_KWARG)
        assert archived_store.archived_at is not None

        deletable_store = await client.beta.memory_stores.create(name="Delete Memory Store", **BETA_KWARG)
        deleted_store = await client.beta.memory_stores.delete(deletable_store.id, **BETA_KWARG)
        assert deleted_store.id == deletable_store.id
        assert deleted_store.type == "memory_store_deleted"


async def test_anthropic_sdk_deployments_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Deployment Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Deployment Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        uploaded = await client.beta.files.upload(
            file=("deployment-resource.txt", b"deployment resource", "text/plain"),
            **BETA_KWARG,
        )
        memory_store = await client.beta.memory_stores.create(
            name="SDK Deployment Memory",
            description="Deployment memory.",
            **BETA_KWARG,
        )

        deployment = await client.beta.deployments.create(
            name="SDK Contract Deployment",
            agent={"id": agent.id, "version": agent.version},
            environment_id=environment.id,
            initial_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Run report."}]},
                {"type": "system.message", "content": [{"type": "text", "text": "Contract deployment context."}]},
            ],
            metadata={"keep": "yes", "drop": "soon"},
            resources=[
                {
                    "type": "file",
                    "file_id": uploaded.id,
                    "mount_path": "/workspace/deployment-resource.txt",
                },
                {
                    "type": "github_repository",
                    "url": "https://github.com/example/deployment-repo",
                    "mount_path": "/workspace/deployment-repo",
                    "authorization_token": "ghp_secret",
                    "checkout": {"type": "branch", "name": "main"},
                },
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store.id,
                    "access": "read_only",
                    "instructions": "Use deployment memory.",
                },
            ],
            schedule={"type": "cron", "expression": "0 9 * * *", "timezone": "UTC"},
            vault_ids=[],
            **BETA_KWARG,
        )
        assert deployment.type == "deployment"
        assert deployment.agent.id == agent.id
        assert deployment.environment_id == environment.id
        assert [event.type for event in deployment.initial_events] == ["user.message", "system.message"]
        assert deployment.schedule.expression == "0 9 * * *"
        assert {resource.type for resource in deployment.resources} == {"file", "github_repository", "memory_store"}
        deployment_resources_by_type = {resource.type: resource for resource in deployment.resources}
        assert "authorization_token" not in deployment_resources_by_type["github_repository"].model_dump()

        updated = await client.beta.deployments.update(
            deployment.id,
            description="Updated deployment.",
            metadata={"drop": None, "added": "yes"},
            **BETA_KWARG,
        )
        assert updated.description == "Updated deployment."
        assert updated.metadata["added"] == "yes"
        assert "drop" not in updated.metadata

        paused = await client.beta.deployments.pause(deployment.id, **BETA_KWARG)
        assert paused.status == "paused"
        assert paused.model_dump().get("paused_reason") == {"type": "manual"}

        paused_run = await client.beta.deployments.run(deployment.id, **BETA_KWARG)
        assert paused_run.type == "deployment_run"
        assert paused_run.trigger_context.type == "manual"
        assert paused_run.session_id is not None

        unpaused = await client.beta.deployments.unpause(deployment.id, **BETA_KWARG)
        assert unpaused.status == "active"
        assert unpaused.model_dump().get("paused_reason") is None

        run = await client.beta.deployments.run(deployment.id, **BETA_KWARG)
        assert run.type == "deployment_run"
        assert run.deployment_id == deployment.id
        assert run.agent.id == agent.id
        assert run.trigger_context.type == "manual"
        assert run.session_id is not None

        run_session = await client.beta.sessions.retrieve(run.session_id, **BETA_KWARG)
        run_resources_by_type = {resource.type: resource for resource in run_session.resources}
        assert run_resources_by_type["file"].file_id != uploaded.id
        assert run_resources_by_type["github_repository"].url == "https://github.com/example/deployment-repo"
        assert "authorization_token" not in run_resources_by_type["github_repository"].model_dump()
        assert run_resources_by_type["memory_store"].memory_store_id == memory_store.id
        run_events = [item async for item in client.beta.sessions.events.list(run.session_id, limit=20, **BETA_KWARG)]
        assert [event.type for event in run_events][:2] == ["session.status_idle", "user.message"]

        deployment_sessions = [
            item async for item in client.beta.sessions.list(deployment_id=deployment.id, limit=20, **BETA_KWARG)
        ]
        assert any(item.id == run.session_id for item in deployment_sessions)

        retrieved_run = await client.beta.deployment_runs.retrieve(run.id, **BETA_KWARG)
        assert retrieved_run.id == run.id

        runs = [item async for item in client.beta.deployment_runs.list(deployment_id=deployment.id, **BETA_KWARG)]
        assert any(item.id == run.id for item in runs)

        listed = [item async for item in client.beta.deployments.list(limit=20, **BETA_KWARG)]
        assert any(item.id == deployment.id for item in listed)

        filtered_deployments = [
            item
            async for item in client.beta.deployments.list(
                agent_id=agent.id,
                status="active",
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert any(item.id == deployment.id for item in filtered_deployments)
        assert all(item.agent.id == agent.id for item in filtered_deployments)
        assert all(item.status == "active" for item in filtered_deployments)

        with pytest.raises(anthropic.UnprocessableEntityError):
            await client.beta.deployments.list(
                status="active",
                include_archived=True,
                limit=20,
                **BETA_KWARG,
            )

        archived = await client.beta.deployments.archive(deployment.id, **BETA_KWARG)
        assert archived.archived_at is not None


async def test_anthropic_sdk_deployment_agent_string_pins_latest_contract():
    async with anthropic_client() as (client, _):
        agent = await client.beta.agents.create(name="SDK Deployment Short Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        agent_v2 = await client.beta.agents.update(agent.id, version=agent.version, system="v2", **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Deployment Short Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )

        deployment = await client.beta.deployments.create(
            name="SDK Short Agent Deployment",
            agent=agent.id,
            environment_id=environment.id,
            initial_events=[{"type": "user.message", "content": [{"type": "text", "text": "Run."}]}],
            **BETA_KWARG,
        )
        assert deployment.agent.version == agent_v2.version


async def test_anthropic_sdk_user_profiles_contract():
    async with anthropic_client() as (client, _):
        profile = await client.beta.user_profiles.create(
            relationship="external",
            external_id="user-123",
            name="SDK Contract User",
            metadata={"keep": "yes", "drop": "soon"},
        )
        assert profile.type == "user_profile"
        assert profile.relationship == "external"
        assert profile.trust_grants == {}

        resold_profile = await client.beta.user_profiles.create(
            relationship="resold",
            external_id="company-123",
            name="SDK Resold Company",
        )
        assert resold_profile.relationship == "resold"
        assert resold_profile.name == "SDK Resold Company"

        retrieved = await client.beta.user_profiles.retrieve(profile.id)
        assert retrieved.id == profile.id

        updated = await client.beta.user_profiles.update(
            profile.id,
            metadata={"drop": None, "added": "yes"},
            name="SDK Contract User Updated",
        )
        assert updated.name == "SDK Contract User Updated"
        assert updated.metadata["added"] == "yes"
        assert "drop" not in updated.metadata

        profiles = [item async for item in client.beta.user_profiles.list(limit=20)]
        assert any(item.id == profile.id for item in profiles)

async def test_anthropic_sdk_page_cursor_pagination_and_filters_contract():
    async with anthropic_client() as (client, _):
        agents = []
        for index in range(3):
            agents.append(
                await client.beta.agents.create(
                    name=f"SDK Page Agent {index}",
                    model={"id": "gpt-5.5"},
                    **BETA_KWARG,
                )
            )
        await client.beta.agents.archive(agents[0].id, **BETA_KWARG)

        first_page = await client.beta.agents.list(limit=1, **BETA_KWARG)
        assert first_page.has_more is True
        assert first_page.next_page is not None
        second_page = await client.beta.agents.list(limit=1, page=first_page.next_page, **BETA_KWARG)
        assert second_page.data
        assert first_page.data[0].id != second_page.data[0].id

        default_agent_ids = [item.id async for item in client.beta.agents.list(limit=20, **BETA_KWARG)]
        archived_agent_ids = [
            item.id
            async for item in client.beta.agents.list(limit=20, include_archived=True, **BETA_KWARG)
        ]
        assert agents[0].id not in default_agent_ids
        assert agents[0].id in archived_agent_ids

        environment = await client.beta.environments.create(
            name="SDK Pagination Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        created_sessions = []
        for index in range(3):
            created_sessions.append(
                await client.beta.sessions.create(
                    agent={"type": "agent", "id": agents[1].id, "version": agents[1].version},
                    environment_id=environment.id,
                    title=f"SDK Page Session {index}",
                    **BETA_KWARG,
                )
            )

        session_page = await client.beta.sessions.list(limit=1, order="asc", **BETA_KWARG)
        assert session_page.has_more is True
        assert session_page.next_page is not None
        session_page_2 = await client.beta.sessions.list(limit=1, page=session_page.next_page, order="asc", **BETA_KWARG)
        assert session_page.data[0].created_at <= session_page_2.data[0].created_at
        assert {session.id for session in created_sessions}.issubset(
            {item.id async for item in client.beta.sessions.list(limit=20, include_archived=True, **BETA_KWARG)}
        )
        filtered_sessions = [
            item
            async for item in client.beta.sessions.list(
                agent_id=agents[1].id,
                agent_version=agents[1].version,
                statuses=["idle"],
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert {session.id for session in created_sessions}.issubset({item.id for item in filtered_sessions})
        assert all(item.agent_id == agents[1].id for item in filtered_sessions)
        assert all(item.agent_version == agents[1].version for item in filtered_sessions)
        assert all(item.status == "idle" for item in filtered_sessions)

        user_profiles = []
        for index in range(3):
            user_profiles.append(
                await client.beta.user_profiles.create(
                    relationship="external",
                    external_id=f"page-user-{index}",
                    name=f"SDK Page User {index}",
                )
            )
        user_profile_page = await client.beta.user_profiles.list(limit=2, order="asc")
        assert user_profile_page.has_more is True
        assert user_profile_page.next_page is not None
        user_profile_page_2 = await client.beta.user_profiles.list(limit=2, page=user_profile_page.next_page, order="asc")
        assert user_profile_page_2.data
        assert user_profile_page.data[-1].id != user_profile_page_2.data[0].id


async def test_anthropic_sdk_resource_specific_pagination_and_filter_contract():
    async with anthropic_client() as (client, _):
        skill_ids = []
        for index in range(3):
            skill = await client.beta.skills.create(
                display_title=f"Page Skill {index}",
                files=[
                    (
                        "skill/SKILL.md",
                        f"---\nname: page-{index}\ndescription: Page skill.\n---\nUse page {index}.".encode(),
                        "text/markdown",
                    )
                ],
                **BETA_KWARG,
            )
            skill_ids.append(skill.id)
        skill_page = await client.beta.skills.list(limit=1, source="custom", **BETA_KWARG)
        assert skill_page.has_more is True
        assert skill_page.next_page is not None
        skill_page_2 = await client.beta.skills.list(limit=1, source="custom", page=skill_page.next_page, **BETA_KWARG)
        assert skill_page_2.data
        assert skill_page.data[0].id != skill_page_2.data[0].id

        vault = await client.beta.vaults.create(display_name="Page Vault", **BETA_KWARG)
        credentials = []
        for index in range(3):
            credentials.append(
                await client.beta.vaults.credentials.create(
                    vault.id,
                    display_name=f"Credential {index}",
                    auth={
                        "type": "static_bearer",
                        "mcp_server_url": f"https://credential-{index}.example.invalid",
                        "token": "secret-token",
                    },
                    **BETA_KWARG,
                )
            )
        credential_page = await client.beta.vaults.credentials.list(vault.id, limit=2, **BETA_KWARG)
        assert credential_page.has_more is True
        assert credential_page.next_page is not None
        credential_page_2 = await client.beta.vaults.credentials.list(
            vault.id,
            limit=2,
            page=credential_page.next_page,
            **BETA_KWARG,
        )
        assert credential_page_2.data
        assert credential_page.data[-1].id != credential_page_2.data[0].id

        store = await client.beta.memory_stores.create(name="SDK Page Memory", **BETA_KWARG)
        for index in range(3):
            await client.beta.memory_stores.memories.create(
                store.id,
                path=f"/projects/{index}.md",
                content=f"content {index}",
                **BETA_KWARG,
            )
        memory_page = await client.beta.memory_stores.memories.list(
            store.id,
            path_prefix="/projects/",
            limit=2,
            extra_query={"order": "asc", "order_by": "path"},
            **BETA_KWARG,
        )
        assert memory_page.has_more is True
        assert memory_page.next_page is not None
        assert [item.path for item in memory_page.data] == ["/projects/0.md", "/projects/1.md"]

        memory_page_2 = await client.beta.memory_stores.memories.list(
            store.id,
            path_prefix="/projects/",
            limit=2,
            page=memory_page.next_page,
            extra_query={"order": "asc", "order_by": "path"},
            **BETA_KWARG,
        )
        assert [item.path for item in memory_page_2.data] == ["/projects/2.md"]

        await client.beta.memory_stores.memories.create(
            store.id,
            path="/projects/nested/notes.md",
            content="nested content",
            **BETA_KWARG,
        )
        depth_page = await client.beta.memory_stores.memories.list(
            store.id,
            path_prefix="/projects/",
            depth=1,
            limit=20,
            extra_query={"order": "asc", "order_by": "path"},
            **BETA_KWARG,
        )
        assert any(item.type == "memory_prefix" and item.path == "/projects/nested/" for item in depth_page.data)

        agent = await client.beta.agents.create(name="SDK Run Filter Agent", model={"id": "gpt-5.5"}, **BETA_KWARG)
        environment = await client.beta.environments.create(
            name="SDK Run Filter Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        deployment = await client.beta.deployments.create(
            name="SDK Run Filter Deployment",
            agent={"id": agent.id, "version": agent.version},
            environment_id=environment.id,
            initial_events=[{"type": "user.message", "content": [{"type": "text", "text": "Run."}]}],
            **BETA_KWARG,
        )
        run = await client.beta.deployments.run(deployment.id, **BETA_KWARG)
        filtered_runs = [
            item
            async for item in client.beta.deployment_runs.list(
                deployment_id=deployment.id,
                trigger_type="manual",
                has_error=False,
                **BETA_KWARG,
            )
        ]
        assert any(item.id == run.id for item in filtered_runs)

        files = []
        for index in range(3):
            files.append(
                await client.beta.files.upload(
                    file=(f"page-{index}.txt", f"file {index}".encode(), "text/plain"),
                    **BETA_KWARG,
                )
            )
        file_page = await client.beta.files.list(limit=2, **BETA_KWARG)
        assert file_page.has_more is True
        file_page_2 = await client.beta.files.list(limit=2, after_id=file_page.last_id, **BETA_KWARG)
        assert file_page_2.data
        assert file_page.data[-1].id != file_page_2.data[0].id
        file_before_page = await client.beta.files.list(limit=2, before_id=file_page_2.data[0].id, **BETA_KWARG)
        assert [item.id for item in file_before_page.data] == [item.id for item in file_page.data]
