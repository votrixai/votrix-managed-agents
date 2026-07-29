import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from deepagents.backends import StateBackend
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests, session_scope
from app.db.models import (
    Base,
    ManagedResource,
    Organization,
    SessionEvent,
    SessionEventIdempotency,
)
from app.db.queries import api_keys as api_keys_q
from app.db.queries import events as events_q
from app.db.queries import sessions as sessions_q
from app.organization import (
    CurrentOrganization,
    reset_current_organization,
    set_current_organization,
)
from app.runtime.checkpoints import close_checkpoint_saver
from app.runtime.providers import RuntimeProviderCapabilities, RuntimeProviderConfig
from app.runtime.sandbox import BackendHandle, SandboxRuntimePlan
from tests.conftest import TEST_API_KEY, TEST_HEADERS, TEST_ORGANIZATION_ID
from tests.test_deepagents_engine import _ScriptedModel, _event, _version


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the suite's SQLite fixture only for this guarded integration test."""

    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")

    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "state")
    monkeypatch.setenv("VMA_DEFAULT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"fake":{"adapter":"fake","default_model":"test-model"}}',
    )
    monkeypatch.setenv("VMA_REQUIRE_BETA_HEADER", "true")
    monkeypatch.setenv("VMA_REQUIRE_ANTHROPIC_VERSION_HEADER", "true")
    organization_token = set_current_organization(
        CurrentOrganization(
            id=TEST_ORGANIZATION_ID,
            slug="test",
            source="postgres_test",
        )
    )
    get_settings.cache_clear()
    await close_checkpoint_saver()
    await reset_engine_for_tests()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as db:
        db.add(
            Organization(
                id=TEST_ORGANIZATION_ID,
                slug="test",
                name="PostgreSQL integration test",
                metadata_={"provisioned_by": "test_fixture"},
            )
        )
        await api_keys_q.create_api_key(
            db,
            organization_id=TEST_ORGANIZATION_ID,
            name="PostgreSQL integration key",
            token=TEST_API_KEY,
            scopes=(api_keys_q.API_SCOPE,),
            created_by="test_fixture",
        )
        await db.commit()
    yield
    await close_checkpoint_saver()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await reset_engine_for_tests()
    get_settings.cache_clear()
    reset_current_organization(organization_token)


@pytest.fixture
async def postgres_client():
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def _create_session(client: AsyncClient) -> dict:
    agent = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Postgres Idempotency Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent.status_code == 201, agent.text
    environment = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "postgres-idempotency-worker", "config": {"type": "self_hosted"}},
    )
    assert environment.status_code == 201, environment.text
    session = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent.json()["id"], "version": 1},
            "environment_id": environment.json()["id"],
        },
    )
    assert session.status_code == 201, session.text
    return session.json()


async def test_postgres_concurrent_retries_commit_one_event_and_one_work(postgres_client):
    session = await _create_session(postgres_client)
    url = f"/v1/sessions/{session['id']}/events"
    headers = {**TEST_HEADERS, "Idempotency-Key": "postgres-concurrent-turn"}
    payload = {"events": [{"type": "user.message", "content": "exactly once"}]}

    first, second = await asyncio.gather(
        postgres_client.post(url, headers=headers, json=payload),
        postgres_client.post(url, headers=headers, json=payload),
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    async with session_scope() as db:
        event_count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(SessionEvent.session_id == session["id"], SessionEvent.type == "user.message")
        )
        work_count = await db.scalar(
            select(func.count())
            .select_from(ManagedResource)
            .where(
                ManagedResource.resource_type == "environment_work",
                ManagedResource.name == f"session:{session['id']}",
            )
        )
        submission_count = await db.scalar(
            select(func.count())
            .select_from(SessionEventIdempotency)
            .where(SessionEventIdempotency.session_id == session["id"])
        )
    assert (event_count, work_count, submission_count) == (1, 1, 1)


async def test_postgres_concurrent_duplicate_event_id_returns_one_row(postgres_client):
    session_data = await _create_session(postgres_client)
    event_id = f"evt_pg_duplicate_{uuid4().hex}"

    async def append_once():
        async with session_scope() as db:
            session = await sessions_q.get_session(
                db,
                session_data["id"],
                organization_id=TEST_ORGANIZATION_ID,
            )
            assert session is not None
            event = await events_q.append_event(
                db,
                session,
                event_type="agent.message",
                payload={
                    "type": "agent.message",
                    "content": [{"type": "text", "text": "one durable response"}],
                },
                event_id=event_id,
            )
            await db.commit()
            return event.id, event.seq

    first, second = await asyncio.gather(append_once(), append_once())

    assert first == second
    async with session_scope() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(
                SessionEvent.session_id == session_data["id"],
                SessionEvent.id == event_id,
            )
        )
    assert count == 1


async def test_postgres_completed_checkpoint_recovers_without_second_model_call(monkeypatch):
    import app.runtime.deepagents_engine as runtime_engine

    model = _ScriptedModel(responses=[AIMessage(content="postgres checkpoint answer")])
    provider = RuntimeProviderConfig(
        provider="fake",
        model_id="fake-model",
        adapter="fake",
        api_key=None,
        base_url=None,
        capabilities=RuntimeProviderCapabilities(tool_calls=True),
    )
    monkeypatch.setattr(runtime_engine, "resolve_runtime_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(runtime_engine, "build_chat_model", lambda _provider: model)

    @asynccontextmanager
    async def fake_backend(**_kwargs):
        plan = SandboxRuntimePlan(
            enabled=True,
            backend="langgraph_state",
            supports_execute=False,
            policy_enforced=False,
            summary={"enabled": True, "backend": "langgraph_state"},
        )
        yield BackendHandle(backend=StateBackend(), plan=plan)

    monkeypatch.setattr(runtime_engine, "open_backend", fake_backend)
    admissions = 0
    recoveries = 0

    async def admit_execution():
        nonlocal admissions
        admissions += 1
        return admissions

    async def begin_recovery():
        nonlocal recoveries
        recoveries += 1

    async def crash_before_journal(payload):
        if payload["type"] == "agent.message":
            raise RuntimeError("simulated completed-checkpoint crash")
        return payload["_event_id"]

    recovery_id = uuid4().hex
    context = {
        "organization_id": TEST_ORGANIZATION_ID,
        "session_id": f"sess_postgres_checkpoint_recovery_{recovery_id}",
        "checkpoint_thread_id": f"thread_postgres_checkpoint_recovery_{recovery_id}",
        "work_id": f"work_postgres_checkpoint_recovery_{recovery_id}",
    }
    with pytest.raises(RuntimeError, match="completed-checkpoint crash"):
        await runtime_engine.execute_deep_agent(
            _version(),
            [_event(1, "user.message", content="hello")],
            {"type": "cloud"},
            runtime_context=context,
            emit_event=crash_before_journal,
            admit_execution=admit_execution,
            begin_recovery=begin_recovery,
        )

    durable = []

    async def emit_event(payload):
        durable.append(dict(payload))
        return payload["_event_id"]

    recovered = await runtime_engine.execute_deep_agent(
        _version(),
        [_event(1, "user.message", content="hello")],
        {"type": "cloud"},
        runtime_context=context,
        emit_event=emit_event,
        admit_execution=admit_execution,
        begin_recovery=begin_recovery,
    )

    assert model._index == 1
    assert admissions == 1
    assert recoveries == 1
    assert recovered.final_text == "postgres checkpoint answer"
    assert [event["type"] for event in durable] == ["agent.message"]
