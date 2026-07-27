"""Consumer-driven contract for the SDK surface used by votrix-backend.

Keep this suite deliberately narrower than ``test_anthropic_sdk_contract.py``.
It must run unchanged with both the backend's current SDK and VMA's forward
contract SDK. Runtime parity that needs E2B, a real model, or cross-Session
memory belongs in explicit acceptance tests rather than being inferred here.
"""

from contextlib import asynccontextmanager
import asyncio
import io
import json
import socket
import zipfile

import anthropic
import httpx
import pytest
import uvicorn
from anthropic import AsyncAnthropic
from httpx import ASGITransport
from tests.conftest import TEST_API_KEY


pytestmark = pytest.mark.contract

MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
# Most backend calls rely on the generated SDK resource to attach its beta.
BETA_KWARG = {}
FILES_BETA_KWARG = {
    "betas": ["files-api-2025-04-14", MANAGED_AGENTS_BETA],
}
_BACKEND_ZIP_DATE = (2024, 1, 1, 0, 0, 0)


def _backend_skill_zip(*, description: str, body: str) -> bytes:
    """Use the exact archive and multipart shape from votrix-backend skills.py."""

    content = f"---\nname: consumer-skill\ndescription: {description}\n---\n{body}".encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(
            filename="consumer-skill/SKILL.md",
            date_time=_BACKEND_ZIP_DATE,
        )
        archive.writestr(info, content)
    return buffer.getvalue()


@asynccontextmanager
async def backend_consumer_client():
    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        sdk = AsyncAnthropic(
            api_key=TEST_API_KEY,
            base_url="http://testserver",
            default_headers={
                "anthropic-version": "2023-06-01",
            },
            http_client=http_client,
            max_retries=0,
            _strict_response_validation=True,
        )
        yield sdk


def test_backend_sdk_public_surface_is_available():
    """Fail before HTTP if an SDK release removes a backend dependency."""

    client = AsyncAnthropic(api_key=TEST_API_KEY)
    assert anthropic.__version__ in {"0.97.0", "0.116.0"}
    expected = {
        "agents": {"create", "retrieve", "update", "versions"},
        "environments": {"create"},
        "sessions": {"create", "retrieve", "delete", "events", "resources"},
        "skills": {"create", "list", "versions"},
        "files": {"upload", "retrieve_metadata", "list", "download", "delete"},
        "memory_stores": {"create", "update", "archive"},
    }

    for resource_name, methods in expected.items():
        resource = getattr(client.beta, resource_name)
        missing = methods - set(dir(resource))
        assert not missing, (
            f"anthropic=={anthropic.__version__}: {resource_name} is missing "
            f"backend methods {sorted(missing)}"
        )

    assert {"send", "stream"} <= set(dir(client.beta.sessions.events))
    assert {"add"} <= set(dir(client.beta.sessions.resources))
    assert {"create"} <= set(dir(client.beta.skills.versions))


async def test_backend_provisioning_wire_contract():
    """Exercise the Agent, Skill, and Memory calls made during provisioning."""

    async with backend_consumer_client() as client:
        skill = await client.beta.skills.create(
            display_title="Votrix Backend Consumer Skill",
            files=[
                (
                    "skill.zip",
                    _backend_skill_zip(description="Consumer contract.", body="Use it."),
                    "application/zip",
                )
            ],
            **BETA_KWARG,
        )
        await client.beta.skills.versions.create(
            skill.id,
            files=[
                (
                    "skill.zip",
                    _backend_skill_zip(description="Consumer contract v2.", body="Use it."),
                    "application/zip",
                )
            ],
            **BETA_KWARG,
        )
        listed_skills = [
            item
            async for item in client.beta.skills.list(
                limit=100,
                source="custom",
                **BETA_KWARG,
            )
        ]
        assert any(item.id == skill.id for item in listed_skills)

        agent = await client.beta.agents.create(
            name="Votrix Backend Consumer Agent",
            model="claude-sonnet-4-6",
            system="Exercise the backend consumer contract.",
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "permission_policy": {"type": "always_allow"},
                    },
                },
                {
                    "type": "custom",
                    "name": "lookup_account",
                    "description": "Look up an account.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"account_id": {"type": "string"}},
                        "required": ["account_id"],
                    },
                },
            ],
            skills=[{"type": "custom", "skill_id": skill.id, "version": "latest"}],
            **BETA_KWARG,
        )
        current = await client.beta.agents.retrieve(agent.id, **BETA_KWARG)
        updated = await client.beta.agents.update(
            agent.id,
            version=current.version,
            name="Votrix Backend Consumer Agent v2",
            model="deepseek/deepseek-v4-pro",
            **BETA_KWARG,
        )
        assert updated.version == current.version + 1
        assert updated.model.id == "deepseek/deepseek-v4-pro"
        versions = [
            item
            async for item in client.beta.agents.versions.list(
                agent.id,
                limit=20,
                **BETA_KWARG,
            )
        ]
        assert [item.version for item in versions] == [updated.version, current.version]

        memory_store = await client.beta.memory_stores.create(
            name="Votrix Backend Consumer Memory",
            description="Created through the backend consumer surface.",
            **BETA_KWARG,
        )
        updated_store = await client.beta.memory_stores.update(
            memory_store.id,
            description="Updated through the backend consumer surface.",
            **BETA_KWARG,
        )
        assert updated_store.description == "Updated through the backend consumer surface."
        archived_store = await client.beta.memory_stores.archive(memory_store.id, **BETA_KWARG)
        assert archived_store.archived_at is not None


async def test_backend_session_file_event_and_sse_parser_wire_contract():
    """Mirror the backend's stateful IDs, resource calls, and SSE parser path."""

    async with backend_consumer_client() as client:
        agent = await client.beta.agents.create(
            name="Votrix Backend Session Agent",
            model="deepseek/deepseek-v4-pro",
            tools=[
                {
                    "type": "custom",
                    "name": "lookup_account",
                    "description": "Look up an account.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"account_id": {"type": "string"}},
                        "required": ["account_id"],
                    },
                }
            ],
            **BETA_KWARG,
        )
        environment = await client.beta.environments.create(
            name="Votrix Backend Session Environment",
            config={"type": "cloud"},
            **BETA_KWARG,
        )
        memory_store = await client.beta.memory_stores.create(
            name="Votrix Backend Session Memory",
            **BETA_KWARG,
        )
        session = await client.beta.sessions.create(
            agent=agent.id,
            environment_id=environment.id,
            resources=[
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store.id,
                    "access": "read_write",
                    "instructions": "Retain account preferences.",
                }
            ],
            **BETA_KWARG,
        )
        assert session.agent.id == agent.id
        assert session.resources[0].memory_store_id == memory_store.id

        uploaded = await client.beta.files.upload(
            file=("consumer-input.txt", b"consumer input", "text/plain"),
            **FILES_BETA_KWARG,
        )
        mounted = await client.beta.sessions.resources.add(
            session.id,
            file_id=uploaded.id,
            type="file",
            mount_path="/mnt/session/uploads/consumer-input.txt",
            **BETA_KWARG,
        )
        assert mounted.type == "file"
        uploaded_image = await client.beta.files.upload(
            file=("consumer-chart.png", b"\x89PNG\r\n\x1a\nconsumer-chart", "image/png"),
            **FILES_BETA_KWARG,
        )
        mounted_image = await client.beta.sessions.resources.add(
            session.id,
            file_id=uploaded_image.id,
            type="file",
            mount_path="/mnt/session/uploads/consumer-chart.png",
            **BETA_KWARG,
        )
        assert mounted_image.type == "file"

        scoped_files = [
            item
            async for item in client.beta.files.list(
                scope_id=session.id,
                limit=20,
                **FILES_BETA_KWARG,
            )
        ]
        assert any(item.id == mounted.file_id for item in scoped_files)
        metadata = await client.beta.files.retrieve_metadata(
            mounted.file_id,
            **FILES_BETA_KWARG,
        )
        assert metadata.id == mounted.file_id
        download = await client.beta.files.download(
            mounted.file_id,
            **FILES_BETA_KWARG,
        )
        assert await download.read() == b"consumer input"

        turn_content = [
            {"type": "text", "text": "Use the mounted input."},
            {
                "type": "document",
                "source": {"type": "file", "file_id": uploaded.id},
            },
            {
                "type": "image",
                "source": {"type": "file", "file_id": uploaded_image.id},
            },
        ]
        sent = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": turn_content,
                }
            ],
            extra_headers={"Idempotency-Key": "consumer-contract-turn-1"},
            **BETA_KWARG,
        )
        assert sent.data[0].type == "user.message"
        replayed = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": turn_content,
                }
            ],
            extra_headers={"Idempotency-Key": "consumer-contract-turn-1"},
            **BETA_KWARG,
        )
        assert replayed.data[0].id == sent.data[0].id

        stop_reason = None
        for _ in range(50):
            current = await client.beta.sessions.retrieve(session.id, **BETA_KWARG)
            stop_reason = (
                current.stop_reason.model_dump(mode="json")
                if hasattr(current.stop_reason, "model_dump")
                else current.stop_reason
            )
            if stop_reason and stop_reason.get("type") == "requires_action":
                break
            await asyncio.sleep(0.02)
        assert stop_reason is not None
        assert stop_reason["type"] == "requires_action"
        custom_tool_use_id = stop_reason["event_ids"][0]

        tool_result = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": custom_tool_use_id,
                    "content": [{"type": "text", "text": "account found"}],
                }
            ],
            extra_headers={"Idempotency-Key": "consumer-contract-tool-result-1"},
            **BETA_KWARG,
        )
        assert tool_result.data[0].type == "user.custom_tool_result"

        retrieved_session = await client.beta.sessions.retrieve(session.id, **BETA_KWARG)
        assert retrieved_session.id == session.id

        durable_events = [
            item
            async for item in client.beta.sessions.events.list(
                session.id,
                limit=20,
                **BETA_KWARG,
            )
        ]
        idle_event = next(item for item in durable_events if item.type == "session.status_idle")
        await _assert_backend_sse_parser_accepts_event(idle_event.model_dump(mode="json"))

        # The backend meters model tokens exclusively from the model-request
        # span pair, so a turn that emits no span records no usage at all.
        starts = [item for item in durable_events if item.type == "span.model_request_start"]
        ends = [item for item in durable_events if item.type == "span.model_request_end"]
        assert starts and ends
        start_ids = {item.id for item in starts}
        for end in ends:
            payload = end.model_dump(mode="json")
            assert payload["model_request_start_id"] in start_ids
            assert set(payload["model_usage"]) >= {
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            }
            await _assert_backend_sse_parser_accepts_event(payload)

        deleted = await client.beta.sessions.delete(session.id, **BETA_KWARG)
        assert deleted.id == session.id
        deleted_file = await client.beta.files.delete(uploaded.id, **FILES_BETA_KWARG)
        assert deleted_file.id == uploaded.id
        deleted_image = await client.beta.files.delete(uploaded_image.id, **FILES_BETA_KWARG)
        assert deleted_image.id == uploaded_image.id


async def test_backend_opens_stream_before_send_and_only_receives_new_turns():
    """Exercise the real infinite SSE route over loopback, not a buffered ASGI transport."""

    async with _loopback_server() as base_url:
        client = AsyncAnthropic(
            api_key=TEST_API_KEY,
            base_url=base_url,
            max_retries=0,
            _strict_response_validation=True,
        )
        try:
            agent = await client.beta.agents.create(
                name="Votrix Backend Live Stream Agent",
                model="deepseek/deepseek-v4-pro",
            )
            environment = await client.beta.environments.create(
                name="Votrix Backend Live Stream Environment",
                config={"type": "cloud"},
            )
            session = await client.beta.sessions.create(
                agent=agent.id,
                environment_id=environment.id,
            )

            first_turn = await _stream_one_backend_turn(client, session.id, "first turn")
            second_turn = await _stream_one_backend_turn(client, session.id, "second turn")
        finally:
            await client.close()

    assert first_turn[0].type == "user.message"
    assert second_turn[0].type == "user.message"
    assert all(event.seq > 1 for event in first_turn)
    assert min(event.seq for event in second_turn) > max(event.seq for event in first_turn)
    assert any(event.type == "agent.message" for event in first_turn)
    assert any(event.type == "agent.message" for event in second_turn)


async def _stream_one_backend_turn(client: AsyncAnthropic, session_id: str, text: str):
    observed = []
    async with await client.beta.sessions.events.stream(session_id) as stream:
        await client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )
        async for event in stream:
            observed.append(event)
            if event.type != "session.status_idle":
                continue
            stop_reason = (
                event.stop_reason.model_dump(mode="json")
                if hasattr(event.stop_reason, "model_dump")
                else event.stop_reason
            )
            if stop_reason and stop_reason.get("type") == "end_turn":
                break

    assert observed, "the SDK stream closed without delivering this turn"
    return observed


@asynccontextmanager
async def _loopback_server():
    from app.main import app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    host, port = sock.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("loopback VMA server did not start")
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


async def _assert_backend_sse_parser_accepts_event(event_payload: dict):
    """Use only the public stream iterator, exactly as backend protocol.py does."""

    sse_frame = (
        f"id: {event_payload['seq']}\n"
        f"event: {event_payload['type']}\n"
        f"data: {json.dumps(event_payload, separators=(',', ':'))}\n\n"
    ).encode()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_frame,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://stream.test",
    ) as http_client:
        client = AsyncAnthropic(
            api_key=TEST_API_KEY,
            base_url="http://stream.test",
            default_headers={
                "anthropic-version": "2023-06-01",
            },
            http_client=http_client,
            max_retries=0,
            _strict_response_validation=True,
        )
        async with await client.beta.sessions.events.stream(
            event_payload["session_id"],
            **BETA_KWARG,
        ) as stream:
            parsed = await anext(stream)

    assert parsed.type == event_payload["type"]
    assert parsed.session_id == event_payload["session_id"]
    assert parsed.seq == event_payload["seq"]
